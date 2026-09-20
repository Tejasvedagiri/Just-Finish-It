import copy
import gzip
import json
import os
import pickle
import re
from pathlib import Path
from typing import Dict, Optional

from JFI.llm.base_llm_stream import phase_env
from JFI.manager.abstract_manager import AbstractManager, phase_display_name
from JFI.session.abstract_session_manager import SessionManager
from JFI.tool.context_tools import render_facts_for_auto_load

# flock is Unix-only (Linux/macOS) — the ./JFI launcher is already a POSIX
# shell script, so this project has never targeted Windows directly.
# Session locking degrades to a no-op there rather than failing to import.
try:
    import fcntl
except ImportError:
    fcntl = None


class SessionInUseError(Exception):
    """Raised when another process already holds this session's lock (see
    SimpleSessionManager._acquire_session_lock) — two processes racing on
    the same session's history.jsonl.gz/plan.md/context.json could
    otherwise corrupt them."""
    pass


# Per-process registry of held session locks: {resolved lock path: [refcount,
# file_handle]} — see SimpleSessionManager._acquire_session_lock for why a
# second SimpleSessionManager for the same session_id within this same
# process must not trip the cross-process guard.
_SESSION_LOCKS: dict = {}

# --- append-only history persistence ----------------------------------------
# history.pkl used to be rewritten *in full* on every single message, so a
# long session's every turn cost an O(total history) disk write — on a
# multi-hundred-MB session that turned into a multi-second stall per turn.
# history.jsonl.gz instead gets one message appended per save, as its own
# independent gzip member; concatenated gzip members decompress transparently
# as a single stream on read (that's part of the gzip spec, not a hack), so a
# plain `gzip.open(path, "rb")` reads the whole history back in one go. It
# also means a crash mid-write can corrupt at most the one trailing message
# being flushed — everything in earlier (already-closed) members is
# unaffected and still loads.


def _parse_jsonl(raw: bytes) -> list:
    messages = []
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # skip one corrupt line rather than lose the rest
    return messages


def _read_jsonl_gz(path: Path) -> list:
    """Reads an append-only, multi-member gzip JSONL history file.

    Tolerant of a truncated/corrupt final member (the one being written when
    a crash interrupted a save). The naive fix — read in chunks and stop at
    the first error — doesn't actually work: ``GzipFile.read(n)`` decodes
    however many members it needs to satisfy the *whole* request, and if that
    walk reaches the corrupt member it raises without returning ANY bytes for
    that call, including ones already decoded from perfectly clean earlier
    members. So the fast path below (one big read) is what normally runs;
    only if it fails do we fall back to a byte-at-a-time read, which forces
    each call to return only what's already sitting in the decompressor's
    internal buffer — that isolates the failure to the single call that
    finally has to touch the corrupt member, keeping everything before it.
    """
    if not path.exists() or path.stat().st_size == 0:
        return []

    try:
        with gzip.open(path, "rb") as f:
            return _parse_jsonl(f.read())
    except (OSError, EOFError):
        pass

    buffer = bytearray()
    try:
        with gzip.open(path, "rb") as f:
            while True:
                chunk = f.read(1)
                if not chunk:
                    break
                buffer += chunk
    except (OSError, EOFError):
        pass  # keep whatever decoded before the corrupt member was reached
    return _parse_jsonl(bytes(buffer))


def _append_jsonl_gz(path: Path, messages: list) -> None:
    """Appends `messages` to `path` as one new gzip member. O(len(messages)),
    never O(total history already on disk)."""
    if not messages:
        return
    payload = "".join(json.dumps(m) + "\n" for m in messages).encode("utf-8")
    with gzip.open(path, "ab") as f:
        f.write(payload)

# --- context budget ---------------------------------------------------------
# Read lazily (not at import time) so load_dotenv() ordering doesn't matter.
DEFAULT_CONTEXT_SIZE = 32768
DEFAULT_CONTEXT_RATIO = 0.7  # headroom left for the model's own reply

# History structure kept verbatim no matter how tight the budget gets.
# Raised from 6 -- a tight CONTEXT_SIZE was pushing real recent turns (not
# just the oldest history) into Tier 4's digest/Tier 5's trimming far more
# often than felt right for a live session to lose detail on its OWN most
# recent work, not just aged-out history.
KEEP_RECENT_BLOCKS = 10
TOOL_RESULT_HEAD = 600
TOOL_RESULT_TAIL = 400

# Tier 4 (the digest) only pays for an extra LLM summarization call once at
# least this many characters of NEW (not-yet-condensed) history have aged
# out of the recent window — a tight CONTEXT_SIZE can trigger compression on
# nearly every turn, and summarizing 1-2 newly-aged blocks at a time would
# mean one extra LLM round-trip per turn. Below this threshold, new blocks
# are appended to the digest as the old cheap tool/file/command note instead
# (never silently dropped) until enough piles up to justify condensing them
# for real. The very first digest of a session always summarizes regardless
# of size, so the model gets a real summary (not just a metadata note) as
# soon as anything ages out.
DIGEST_SUMMARY_MIN_CHARS = 6000
# Hard cap on how much rendered new-block text goes into one summarization
# request -- keeps a single Tier 4 pass bounded even if an unusually large
# batch of history aged out between compressions. Raised alongside
# _render_blocks_for_summary's per-message caps (see compress_history's
# pretrim_blocks) so the summarizer actually sees the fuller tool-result text
# now reaching it instead of clipping right back down to the old, tighter
# total.
DIGEST_INPUT_CHAR_CAP = 20000

PLAN_FORMAT_RULES = """
    PLAN FILE FORMAT (mandatory, no exceptions):
    - The plan file is exactly: {plan_path}
    - Exactly two "##" headers, spelled exactly "## Implementation" and
      "## Testing" — JFI's own tooling scans for a header starting with
      those words to build each phase's work queue; a typo or different
      name means an EMPTY queue with no error shown. Nest every logical
      group (scaffolding, API, UI, ...) as bullets under those two headers
      only — never a separate "##"/"###" subsection. ANY markdown header,
      any level, anywhere in the file ends the current section right there,
      silently dropping everything below it from the work queue even
      though it's still in the file. Architecture notes/data model go
      under "## Context and Prerequisites" instead, same reason.
    - {plan_path}'s own directory is bookkeeping only — deliverables
      (source, tests, docs) go in the normal project layout at the working
      directory root, never inside it. A scaffolder needing an empty
      target dir (`create-next-app`, `npm create vite`,
      `django-admin startproject`) will refuse to run there because that
      folder already exists — expected, not a real error: scaffold into a
      throwaway subdir, then move everything up
      (`mv temp-app/* temp-app/.[!.]* . 2>/dev/null; rmdir temp-app`).
    - The plan is a TREE. Break every task down until each leaf is one
      small, directly-doable action (touch one file/function, run one
      command, write one test) — recurse as many levels as it takes (a
      parent commonly bottoms out 3-4 levels down, e.g. 1 -> 1.1 -> 1.1.1
      -> 1.1.1.1), never stopping just because something fits on one line.
      A leaf naming more than one distinct deliverable isn't a leaf yet:
      "+", "and", "/", or a semicolon joining separate nouns/verb-phrases
      means split it, one leaf per piece — same rule whether that's
      several files, several edits to ONE file, or several
      Testing/verification steps (start+curl+curl+check-log+stop is five
      leaves, not one "verify the server" leaf). Rule of thumb: if writing
      the leaf's own tool calls would take more than one call, it's still
      a parent. A task with only one already-small action underneath it
      can stay a single leaf — don't split for the sake of splitting.
    - Only LEAF items get a checkbox: "- [ ] 1.1.1 Description" (not
      started), "- [x] ..." (finished), "- [○] ..." (user-skipped via
      Ctrl+K — never write this yourself; treat it like finished, never
      redo/revert/flag it). No other marker, ever. Parent tasks (anything
      you broke into subtasks) are plain bullets with NO checkbox — every
      phase's work queue is every checkbox line in file order, so a
      checkbox on a parent hands the implementer a fake duplicate task
      alongside its real subtasks.
    - Numbering: sections are 1, 2, 3 ...; each level of breakdown appends
      one more ".N" (1.1, then 1.1.1, ...), so a leaf's number is its full
      path from the section root. Example — one file, three leaves,
      because the edit itself names three actions:
          - 1. Rewrite index.html for the new bundler
            - [ ] 1.1 Remove the inline <style> block
            - [ ] 1.2 Add the module <script src="/src/main.js"> tag
            - [ ] 1.3 Copy the body markup over unchanged
          - [ ] 2. A second task, already small enough as one leaf
    - Item text must stay byte-identical when you tick it: change only the
      space inside the brackets to an x, so a targeted replace can find it.
"""

CONTEXT_CACHE_RULES = """
    CONTEXT CACHE (optional, persists across turns and phases):
    - A small fact store backing {context_cache_path} holds things worth
      remembering that would otherwise be lost once older turns are
      compressed out of your context: key decisions, discovered schema/API/
      config details, gotchas — anything a later step or phase would
      otherwise have to re-derive.
    - Use the context_save and context_lookup tools for it — NOT read_file/
      write_file. context_save(key, value) merges one fact in with a single
      call; context_lookup(keyword) searches instead of dumping everything —
      call it with no keyword first to see what's already saved (a key plus
      a short preview of each), then again with a keyword to get one fact's
      full text. Never read_file or write_file this path directly: a
      write_file that doesn't perfectly round-trip every existing key
      silently deletes the ones you didn't retype.
    - Keep it small — a handful of high-value facts, not a transcript — and
      never overwrite another entry's key just to remove it from view; if a
      fact is genuinely obsolete, save it with an updated value instead.
    - The FIRST time you work out the correct way to actually run/build/test
      THIS project (which interpreter — plain `python3`, `.venv/bin/python`,
      `uv run ...`; which package-manager script — `npm run dev`/`build`;
      required env vars; the right working directory), immediately
      context_save it under the key "run_commands", one line per command
      with a short label, e.g. "server: cd app && .venv/bin/python main.py |
      build: npm run build (from frontend/) | tests: uv run pytest". Observed
      failure this prevents: discovering the interpreter the hard way (plain
      `python3` fails with ModuleNotFoundError, THEN trying `.venv/bin/
      python` or `uv run`), then re-discovering it the same way again later
      because the turn that figured it out already aged out of context.
      Update the same key (never a second key) if you learn the command was
      wrong or incomplete.
"""

VERIFICATION_RULES = """
    VERIFICATION STANDARD (applies whenever you judge whether something works):
    - If there is ANY mechanical way to check a piece of work — running it,
      compiling/building it, starting it and hitting it, running its test
      suite, executing it against sample input — that check MUST actually be
      run with execute_command. Reading the source and reasoning about what
      it "should" do is not verification and is not a substitute for running
      it, even when the code looks obviously correct.
    - This applies beyond languages with an obvious test runner: a frontend
      app with no test suite still has `npm run build` (or the equivalent
      compile/bundle step); a server still has "start it, curl it, stop it";
      a script still has "run it with representative input." Plan for and
      perform that kind of check even when nobody asked for automated tests.
    - Only fall back to code-reading-only verification when no mechanical
      check is possible at all (e.g. prose content, a static design decision).
    - Testing the small pure/helper functions in isolation is not enough by
      itself, in any language or stack. Also exercise the actual entry point
      a real user or caller would go through end-to-end: a CLI's main loop
      (including its exit and error-handling paths, not just the functions
      it calls), an HTTP route handler (a real request in, a real response
      checked out), a GUI's event loop, an exported public API called the
      way a consumer would call it. A suite that only covers internal
      building blocks while leaving the thing the goal actually described
      untested has NOT verified the goal, no matter how many of those
      building-block tests pass.
    - Starting anything long-running you'll need to stop later (a dev/test
      server, a GUI app that opens a window and never returns on its own):
      use start_background_process/stop_background_process, not a shell `&`
      plus a hand-tracked PID. Those two tools give you a HANDLE, and
      stop_background_process only ever accepts that handle — never a raw
      pid or a name pattern — so it is structurally immune to the failure
      that makes manual PID-tracking risky: searching for a process by name
      (`pgrep`/`pkill`) can match and kill the wrong thing, because the
      shell running THIS very execute_command often has that same name in
      its own command line, and the failure shows no useful STDERR, just an
      unexplained kill. (If you still capture a PID by hand for some
      reason — `python app.py & PID=$!; sleep 2; kill "$PID"` — the same
      "never search by name" rule applies; this is the fallback, not the
      default.) For a GUI app specifically: after starting it, run
      capture_screenshot followed by view_image as SEPARATE tool calls, not
      chained into the shell line — they are tools, not shell commands, and
      cannot appear inside an execute_command string.
    - Once a background process you started (and already stopped, or that
      exited on its own) is confirmed no longer needed, call
      clear_finished_processes to prune its bookkeeping entry — a long
      session that starts many one-off verification servers otherwise
      leaves list_processes/the dashboard cluttered with dead entries with
      no way to tell "still relevant" from "leftover noise" at a glance.
      Never clears a still-running process; nothing to double-check there.
    - Order Testing leaves cheapest/most-certain first, most expensive/most
      fragile last. A full build/compile/typecheck step (`npm run build`,
      `tsc --noEmit`, `go build ./...`, `cargo build`, ...) is fast,
      deterministic, and needs nothing beyond what's already installed — it
      belongs near the FRONT of the Testing section, well before anything
      slower or less certain (a live end-to-end browser render, installing
      new system tooling, anything needing network access). Two files
      written in separate steps can drift apart — a function renamed, moved,
      or never added on one side, still imported or called from the other —
      without either side's own local check ever catching it; only a
      whole-project build surfaces that kind of cross-file mismatch, so
      confirm it FIRST rather than discovering it last, after everything
      more elaborate already assumed it worked.
    - A verification step that needs installing new system-level tooling (a
      headless browser, a package needing sudo, anything not already
      available) is not a blocker. Try it once; if it needs permissions or
      downloads that fail in this environment, say so in the report/plan
      note and move on to whatever mechanical checks ARE available (the
      build, curl-ing real routes, a DOM/string check of the rendered HTML)
      instead of spending the rest of the run retrying the same fragile
      step. A goal is unverified only when NO mechanical check ran at all —
      not merely because the single most elaborate possible check couldn't
      run in this environment.
"""


# Default plan location: inside the JFI session folder. The manager overrides this
# with its own resolved path (see SimpleSessionManager.plan_path); it is only used as a
# fallback when callers do not pass an explicit plan_path.
DEFAULT_PLAN_PATH = "JFI/plan.md"
DEFAULT_CONTEXT_CACHE_PATH = "JFI/context.json"

# The only two phases with a per-item checklist to work through (and so the
# only two Ctrl+K/Ctrl+Q skip requests apply to) — planner writes the plan in
# one continuous pass, reviewer judges the whole thing at once.
PHASE_SECTION = {"imp": "Implementation", "testing": "Testing"}


def get_system_message(phase: str, plan_path: str = DEFAULT_PLAN_PATH,
                       context_cache_path: str = DEFAULT_CONTEXT_CACHE_PATH,
                       plan_format_rules: str = PLAN_FORMAT_RULES,
                       unlocked_tools=(), planner_stage: Optional[str] = None) -> str:
    """`plan_format_rules` defaults to the generic, task-agnostic
    PLAN_FORMAT_RULES block; a caller that already knows something about
    the goal (see AdaptiveSessionManager) can pass a smaller/more targeted
    rules block instead -- everything else about the message is unchanged
    either way.

    `unlocked_tools` (this session's own, from SessionManager.unlocked_tools())
    trims the TOOL ACCESS listing down to whatever's still locked -- see
    tool/schemas.deferred_tools_rules -- so it shrinks turn by turn instead
    of repeating the full catalog forever once everything's unlocked.

    `planner_stage` (only meaningful when `phase == "planner"`) selects one
    of four narrower role-prompts -- "architect" (top-level shape only, no
    checkboxes), "team_lead" (level-1 -> level-2/3 feature breakdown, still
    mostly no checkboxes), "journeyman" (walk every branch down to
    genuinely atomic checkbox leaves), or "function_breakdown" (for every
    atomic leaf that writes code, break IT down further into one child leaf
    per function/method it implements) -- run in sequence by
    runner.run_phase instead of one combined pass, because a single pass
    was observed letting compound leaves through (see task_rules.py-style
    module docstrings elsewhere in this file for the "observed in practice"
    convention: two real cases from one session, a bundled 4-scenario test
    leaf and a bundled kill/start/request/inspect-DB leaf, are named
    directly in the journeyman prompt below as the standard this pass
    exists to catch). Left as ``None`` (the default), this returns EXACTLY
    today's original single combined planner prompt unchanged -- the
    PLANNER_SINGLE_PASS=1 escape hatch (see runner._planner_single_pass)
    and any caller that predates tiering both get this unmodified path.
    """
    from JFI.tool.schemas import deferred_tools_rules

    rules = (
        plan_format_rules.format(plan_path=plan_path)
        + CONTEXT_CACHE_RULES.format(context_cache_path=context_cache_path)
        + VERIFICATION_RULES
        + deferred_tools_rules(unlocked_tools)
    )

    if phase == "planner" and planner_stage == "architect":
        return f"""
            You are acting as the ARCHITECT for this plan — the first of four passes that
            build it (Architect → Team Lead → Journeyman → Function Breakdown). Your ONLY job right now is the
            plan's TOP-LEVEL SHAPE. Do NOT write leaf checkboxes yet — that is a later pass's
            job, not yours, and doing it now just duplicates work the later passes are about
            to do anyway.
            {rules}

            Your job:
            1. If {plan_path} does not exist yet, create it with write_file using this skeleton:
                   # <Project Title>
                   ## Context and Prerequisites
                   ## Implementation
                   - 1. First high-level piece of work
                   - 2. Second high-level piece of work
                   ## Testing
                   - 1. First high-level verification concern
               "## Implementation" and "## Testing" must appear exactly like that (see PLAN
               FILE FORMAT above for exactly why). Fill "## Context and Prerequisites" with
               real architecture notes, a data model, or constraints worth recording for the
               passes that follow — this is the one section that is genuinely yours to write
               in full now.
            2. Under "## Implementation" and "## Testing", write ONLY level-1 parent bullets —
               the project's major pieces (e.g. "data model", "API layer", "UI",
               "build/verify pipeline") — each a plain "- 1. ..." bullet with NO checkbox,
               even one that looks small enough to be a single step already.
            3. If {plan_path} ALREADY EXISTS with prior content, read_file it first and extend
               its existing level-1 structure for any NEW request, continuing the existing
               numbering — never touch an existing "- [x]" line.
            4. Never ask the user a question and never wait for approval.

            When the top-level shape is saved, output the exact phrase on its own line: ARCHITECT_STAGE_COMPLETE
        """

    if phase == "planner" and planner_stage == "team_lead":
        return f"""
            You are acting as the TEAM LEAD for this plan — the second of four passes
            (Architect → Team Lead → Journeyman → Function Breakdown). The Architect already wrote {plan_path}'s
            top-level shape; your ONLY job is breaking EVERY level-1 item into feature-sized
            subtasks. Do not invent new level-1 items, and do not write final leaf checkboxes
            yet unless a subtask is ALREADY unambiguously a single atomic action — when in
            doubt, leave it a plain bullet for the Journeyman pass to finish.
            {rules}

            Your job:
            1. read_file {plan_path} first.
            2. For EVERY level-1 bullet ("- 1. ...", "- 2. ...", ...) under "## Implementation"
               and "## Testing", add AT LEAST 2 level-2 children ("- 1.1 ...", "- 1.2 ...") that
               break it into its real features/concerns — and, likewise, at least 2 level-3
               children under any level-2 item that is STILL an obvious bundle of more than one
               feature. In the rare case a level-1 item's real work turns out to be only ONE
               genuine piece, that one piece is still its own level-2 bullet, checkboxed
               directly if it's already atomic (per rule 4 below) — the level-1 item itself
               NEVER gets a checkbox either way (see rule 3), so this never means inventing a
               fake second child just to hit a count. Use replace_in_file/append_to_file on
               {plan_path}; never rewrite the whole file just to add children.
            3. Leave every level-1 bullet exactly as the Architect wrote it — add children
               under it, do not rename, merge, split, or reorder the level-1 items themselves.
            4. Never checkbox anything that still names more than one concern or more than one
               tool call's worth of work — a level-2/3 item is only a leaf if it is GENUINELY
               that small already; otherwise leave it a plain, un-checkboxed bullet. The same
               AT LEAST 2 children rule from step 2 applies at every level: never split an item
               into exactly one child at level-3 either.
            5. Never ask the user a question and never wait for approval.

            When every level-1 item has real feature-level children, output the exact phrase on its own line: TEAM_LEAD_STAGE_COMPLETE
        """

    if phase == "planner" and planner_stage == "journeyman":
        return f"""
            You are acting as a JOURNEYMAN DEVELOPER given this plan to execute — the third
            of four passes (Architect → Team Lead → Journeyman → Function Breakdown), one more
            pass (Function Breakdown) still follows this one before implementation starts. The
            structure is already right; your ONLY job is making sure every branch bottoms out
            in genuinely atomic, checkbox-ready leaves. Do not restructure or re-group anything
            the Architect/Team Lead already built — only go deeper.
            {rules}

            Two real failures this pass exists to catch (from actual runs — treat these as the
            standard, not hypotheticals):
            - A "leaf" reading "GET /endpoint happy paths: empty case -> ...; seeded case ->
              ...; bad-input case -> ..." was really 3-4 independent test scenarios wearing one
              bullet. Each scenario/case is its OWN leaf.
            - A "leaf" reading "kill any already-running server, start a fresh one, make a real
              request, then inspect the database" was really 4 independent actions (kill /
              start / request / inspect) wearing one bullet. Each single tool call is its OWN
              leaf.

            Your job:
            1. read_file {plan_path} first.
            2. Walk every branch under "## Implementation" and "## Testing". For every bullet
               that is NOT yet a checkbox leaf, AND for every existing checkbox leaf too (an
               earlier pass can still get this wrong the same way one pass can), ask: "could I
               do this correctly in ONE focused tool call?" Read the description back and check
               for "+", "and", "/", a semicolon, or more than one verb phrase joining separate
               actions — split every one you find, exactly like the two examples above, and ask
               the same question of each new piece, recursing until every leaf is genuinely
               that small. EVERY parent bullet you create or find must end up with AT LEAST 2
               children — a parent with exactly one child underneath it is pointless nesting,
               not a real decomposition. If a bullet genuinely cannot be broken into 2 or more
               distinct pieces, it is already a leaf: give IT the checkbox directly instead of
               wrapping it in a parent bullet with a single child.
            3. Only LEAF items get a checkbox ("- [ ] 1.1.1 ..."); every parent you broke down
               (or that was already a parent) stays an unchecked, un-checkboxed bullet (see
               PLAN FILE FORMAT above for exactly why).
            4. When a split leaves the numbering after it wrong (gaps, duplicates, or siblings
               now out of order), do NOT fix it line-by-line with replace_in_file — that is
               exactly the failure mode observed in practice (a stale read of the file between
               edits produced duplicate/gapped numbers, needing yet another manual fix-up pass,
               burning turns on pure bookkeeping instead of decomposition). Instead run:
                   python -m JFI.tool.plan_renumber {plan_path} <parent-number>
               (e.g. `python -m JFI.tool.plan_renumber {plan_path} 3` after splitting something
               under item 3) via execute_command — it deterministically renumbers every
               descendant of that parent in one pass, from each line's own number, and also
               repairs indentation to match, in one shot.
            5. Before marking a section done, check leaf ORDERING within it: if a leaf verifies,
               imports, or otherwise depends on something (a package, a file, a table) that a
               LATER-numbered leaf in the same top-level section is responsible for creating or
               installing, that is a real ordering bug — the dependency must be created before
               anything tries to use it. Observed in practice: a leaf "verify the ported
               package imports cleanly" was numbered before the leaf that added the package it
               imports to pyproject.toml and installed it, so the verify step was guaranteed to
               fail the moment it actually ran. Renumber (via step 4's tool) so the
               creating/installing leaf comes first, or merge the two if they're really one
               step.
            6. The Testing section must include at least one concrete, mechanically-checkable
               leaf per the VERIFICATION STANDARD above — e.g. "run `npm run build` and confirm
               it exits 0", "start the server and curl it", "run the script against sample
               input and check the output" — even when nobody asked for automated tests. A
               vague leaf like "manually verify everything looks right" does not satisfy this;
               name the actual command that will run.
            7. Never ask the user a question and never wait for approval.

            When every branch bottoms out in genuinely atomic leaves, output the exact phrase on its own line: JOURNEYMAN_STAGE_COMPLETE
        """

    if phase == "planner" and planner_stage == "function_breakdown":
        return f"""
            You are doing the FOURTH and final pass over this plan (Architect → Team Lead →
            Journeyman → Function Breakdown). Every branch already bottoms out in atomic,
            checkbox-ready leaves — your ONLY job is: for every leaf whose work is writing or
            changing code in a real source file, break IT down further into one child checkbox
            per function/method that leaf will implement. A leaf that is not itself writing code
            (a curl/smoke-test step, a research/read step, a doc update, a manual/eyeball check)
            is NOT a programming task — leave it exactly as the Journeyman wrote it.
            {rules}

            Before reading any source file, call context_lookup with no keyword to see what
            the earlier planner passes already recorded — objective, architecture, module/
            file layout, existing signatures. Re-check it with a keyword (a file or module
            name) before you open that file again for a later leaf: if a fact you need (a
            file's existing functions, a class's shape, where a handler lives) is already
            there, use it instead of re-reading the file. When you DO read a file to work out
            a leaf's function breakdown, context_save what you found (existing functions/
            classes in that file, their signatures, the module's role) under a key named for
            the file/area before moving to the next leaf — so the next leaf touching the same
            file, or a later phase, doesn't re-read it from scratch. Observed failure this
            prevents: re-reading the same source file in full for every single leaf under it
            instead of once.

            Your job:
            1. read_file {plan_path} first.
            2. For every checkbox leaf under "## Implementation" whose description is about
               adding or changing a function, method, handler, endpoint, or similar unit of
               code, work out every individual function/method that leaf's work actually
               touches or creates — checking context_lookup before re-reading a file you've
               already opened for an earlier leaf in this same pass.
            3. If that leaf covers 2 or more functions, turn it back into a plain, un-checkboxed
               bullet (remove its own checkbox) and add one new checkbox child per function —
               "- [ ] N.M.1 implement <function_name>(...): <what it does>",
               "- [ ] N.M.2 implement <next_function_name>(...): ...". If a leaf genuinely
               touches exactly ONE function, leave it as a single leaf unchanged — never invent
               a pointless single child just to run this pass.
            4. Name the actual function/method in each new leaf — its real name and, where
               already decided, its parameters/return shape — not a vague verb: write
               "- [ ] 5.7.2.1 implement collect_news(held: list[str]) -> list[dict]: fan out one
               RSS fetch per held symbol via asyncio.gather" rather than
               "- [ ] 5.7.2.1 write the fetch function".
            5. "## Testing" leaves, and any Implementation leaf that is NOT itself writing code,
               are OUT OF SCOPE for this pass — never touch them.
            6. When a split leaves the numbering after it wrong (gaps, duplicates, or siblings
               out of order), do NOT hand-patch it with replace_in_file — run:
                   python -m JFI.tool.plan_renumber {plan_path} <parent-number>
               via execute_command, exactly as the Journeyman pass does.
            7. Never ask the user a question and never wait for approval.

            When every code-writing leaf is broken down to one child per function (or confirmed
            already single-function), output the exact phrase on its own line: PLANNER_COMPLETE
        """

    if phase == "planner":
        return f"""
            You are a master Planner Agent. You own the plan file and nothing else.
            The plan must work for any kind of goal: writing, coding, research, data processing.
            {rules}

            Your job:
            1. If {plan_path} does not exist yet, create it with write_file. If the plan is long,
               write it in several append_to_file calls rather than one oversized write_file.
               Use this structure:
                   # <Project Title>
                   ## Context and Prerequisites
                   ## Implementation
                   - 1.1 First high-level task
                     - [ ] 1.1.1 Smallest step under it (leaf — no checkbox above it)
                     - 1.1.2 Still too big to do in one step
                       - [ ] 1.1.2.1 Smallest step
                       - [ ] 1.1.2.2 Smallest step
                   - [ ] 1.2 Second high-level task (already small enough as one leaf)
                   ## Testing
                   - [ ] 2.1 ...
               "## Implementation" and "## Testing" must appear exactly like that, however many
               logical groups (scaffolding, API, UI, ...) your own project needs — nest all of
               them as bullets under those two headers, never as extra headers of your own (see
               PLAN FILE FORMAT above for exactly why that breaks silently).
               The Testing section must include at least one concrete, mechanically-checkable
               leaf per the VERIFICATION STANDARD above — e.g. "run `npm run build` and confirm
               it exits 0", "start the server and curl it", "run the script against sample
               input and check the output" — even when nobody asked for automated tests. A
               vague leaf like "manually verify everything looks right" does not satisfy this;
               name the actual command that will be run.
               For EVERY task you add, ask "can I do this correctly in one focused step?" If
               not, break it into AT LEAST 2 subtasks (a parent with exactly one child is
               pointless nesting, not a real decomposition — if you can only find one genuine
               piece, that task was already a leaf: checkbox it directly instead of wrapping it
               in a parent) and ask the same question of each new piece — recurse until every
               leaf is genuinely that small. Only leaves get a checkbox; every parent you broke
               down stays an unchecked, un-checkboxed bullet (see PLAN FILE FORMAT above for
               exactly why).
            2. If {plan_path} ALREADY EXISTS, read_file it first. Every "- [x]" line is work that
               is already finished: leave those lines exactly as they are. Add new tasks for the
               new request the same way — break each down into the smallest leaves before adding
               any checkboxes — continuing the existing numbering.
            3. Do NOT implement anything in this phase. Write the plan file only.
            4. Never ask the user a question and never wait for approval.

            When the plan file is saved, output the exact phrase on its own line: PLANNER_COMPLETE
        """

    elif phase == "product_owner":
        feedback_path = str(Path(plan_path).with_name("feedback_to_plan.md"))
        return f"""
            You are acting as PRODUCT OWNER — a pass between planning and implementation, before
            any code gets written. Your job is to catch a bad plan BEFORE it's too late to fix
            cheaply, by checking what {plan_path} proposes against what ACTUALLY EXISTS in this
            codebase right now. You do NOT write or edit any deliverable code yourself — read-only,
            the same posture as the Reviewer role, just at the opposite end of the pipeline.
            {rules}

            Your job:
            1. read_file {plan_path} in full.
            2. Inspect the real current state of the repo with execute_command/read_file (list
               directories, grep, read the specific files the plan says it will create, modify, or
               depend on) — never just trust the plan's own "Context and Prerequisites" section;
               verify it against what is actually on disk. Look specifically for:
               - A leaf that assumes a file/module/table/dependency doesn't exist when it already
                 does, or vice versa.
               - An ordering bug: something used before whatever creates or installs it (the
                 Journeyman pass already checks this once during planning — a second read from a
                 fresh perspective catches what that pass missed, not a duplicate of the same check).
               - A real requirement from the stated goal that no leaf anywhere actually covers.
               - An approach that conflicts with how the existing code already does the same kind
                 of thing elsewhere in this repo, with no stated reason for diverging.
            3. Decide: is the plan ready to implement exactly as written?
            4. If YES: do NOT write or touch {feedback_path}. Just output a short "Product Owner:
               APPROVED" summary in your reply — what you checked, and what you confirmed against
               the real repo state (not just the plan's own claims about it).
            5. If NO: use write_file to create {feedback_path} with concrete, actionable feedback —
               one numbered item per concern, each naming the specific plan item number and/or file
               involved, plus what should change. This sends the plan back to the planner for one
               more pass, then back to you to review again — so be specific enough that a second
               pass can actually resolve it, the same standard the Reviewer's own report is held to.
            6. Never ask the user a question and never wait for approval.

            When you are done (an APPROVED summary given, or {feedback_path} saved), output the
            exact phrase on its own line: PRODUCT_OWNER_COMPLETE
        """

    elif phase == "imp":
        notes_path = str(Path(plan_path).with_name("NotesForReviewer.md"))
        return f"""
            You are an expert Implementation Agent. You build the deliverables and you keep the
            plan file honest as you go.
            {rules}

            Work ONE step at a time, in this exact loop:
            1. Your work queue (the unchecked Implementation items) is already listed for you
               below the rules — take the FIRST item from it. If anything looks stale, verify
               with read_file {plan_path}.
            2. State which item you are on, in exactly this format:
               **[CURRENT TASK: 1.1]**
            3. Do the work with the tools (write_file, append_to_file, replace_in_file,
               execute_command). To just READ an existing file's contents, prefer read_file
               over `cat`-ing it through execute_command — same information, one less shell
               round trip.
            4. If this step hit a problem worth the reviewer knowing about — something you had
               to work around, an assumption you made because the plan or spec was ambiguous, a
               check you could not fully verify (e.g. no network/sandbox limitation), a
               discrepancy from what was planned — use append_to_file to add a short note to
               {notes_path} (create it with a "# Notes for Reviewer" heading if it doesn't exist
               yet) before moving on: the task number, what happened, and why. Skip this for
               clean, uneventful steps — it exists so the reviewer sees what you couldn't fully
               verify yourself, not a log of everything you did.
            5. IMMEDIATELY tick that one item, using replace_in_file on {plan_path}:
                   old_string: "- [ ] 1.1 Short description of the step"
                   new_string: "- [x] 1.1 Short description of the step"
               Tick exactly one box per step, right after finishing that step. Do NOT batch the
               ticks until the end, and do NOT rewrite the whole plan file just to tick a box.
            6. Repeat from step 1 until no unchecked Implementation items are left.
            7. Once every Implementation item reads "- [x]": if this project has ANY single
               command that builds, compiles, bundles, or type-checks the WHOLE project at once
               (`npm run build`, `tsc --noEmit`, `go build ./...`, `cargo build`, a full test
               COLLECTION step even without running the tests, ...), run it ONCE now with
               execute_command — even though every individual item already passed its own check.
               Two files written in separate steps can drift apart (a function renamed, moved, or
               never added on one side, still imported or called from the other) without either
               item's own local check ever catching it; only a whole-project build/typecheck
               catches that kind of cross-file mismatch. Fix anything it reports — it means an
               earlier "done" item is not actually done — before moving on.

            If a tool or command returns an error, fix the cause and retry it before moving on.
            Leave the Testing items alone; that is the next phase's job.
            Never ask the user a question and never wait for approval.

            When every Implementation item reads "- [x]" AND step 7's whole-project build/typecheck
            (when one exists for this project) is clean, output the exact phrase on its own line:
            IMP_COMPLETE
        """

    elif phase == "testing":
        return f"""
            You are an expert Testing Agent. You verify the work and keep the plan file honest.
            {rules}

            Work ONE step at a time, in this exact loop:
            1. Your work queue (the unchecked Testing items) is already listed for you below
               the rules — take the FIRST item from it. Re-read {plan_path} only for
               surrounding context or when the list looks stale.
            2. State what you are testing, in exactly this format:
               **[CURRENT TEST: 2.1]**
            3. Run it with execute_command per the VERIFICATION STANDARD above. Only read the
               produced files instead when there is genuinely nothing to execute (e.g. checking
               prose content) — never as a shortcut around running code that can be run. When
               you do read a file, prefer read_file over `cat`-ing it through execute_command —
               same information, one less shell round trip.
            4. If it fails, fix the implementation with the file tools and re-run until it passes.
            5. IMMEDIATELY tick that one item with replace_in_file on {plan_path}, exactly as the
               Implementation agent does. One box per step, right after it passes.
            6. Repeat from step 1 until no unchecked Testing items are left.

            Never ask the user a question and never wait for approval.

            When every Testing item reads "- [x]", output the exact phrase on its own
            line: TESTING_COMPLETE
        """

    elif phase == "reviewer":
        review_path = str(Path(plan_path).with_name("review.md"))
        notes_path = str(Path(plan_path).with_name("NotesForReviewer.md"))
        return f"""
            You are an expert Reviewer Agent. You evaluate the finished work and decide whether it
            needs another iteration of planner → imp → testing → reviewer.
            You do NOT gate anything: you never ask for approval, never ask the user a question,
            and never wait for a reply.
            {rules}

            1. read_file {plan_path}, then inspect the files that were produced. If {notes_path}
               exists, read_file it too — it holds the Implementation agent's own notes on
               problems it hit, workarounds it made, or things it could not fully verify. Treat
               each note as something to specifically re-check yourself, not something to accept
               at face value; a note the Implementation agent flagged as a concern is exactly the
               kind of thing an easy PASS can miss. You MUST personally re-run the project's own
               mechanical checks (build/compile, test suite, start-and-hit-it, run-with-sample-
               input — per the VERIFICATION STANDARD above) with execute_command before you may
               say PASS. A Testing-phase item already reading "- [x]" is NOT evidence it still
               passes — later steps may have edited those same files since, silently invalidating
               it. Re-run it yourself, now, in this phase. A review with zero
               execute_command/read_file calls is not a review.
            2. Decide: is the work good — every planned item genuinely done, verified BY YOU JUST
               NOW, free of defects, and with every note from {notes_path} (if it existed) either
               resolved or confirmed harmless?
            3. If the review is GOOD: do NOT write or touch {review_path}. Just output a short
               "Review: PASS" summary in your reply (what was built, what was verified, and how
               any implementation notes were resolved).
            4. Only if problems exist (broken/unfinished work, failing tests, missing pieces, or
               an implementation note that turned out to be a real issue):
               use write_file to create {review_path} with concrete, actionable issue descriptions —
               one numbered item per problem, each naming the file(s) and line(s) involved where relevant,
               plus how to fix it. The next planner iteration will read that file and add new plan
               items from it, so be specific.

            When you are done (PASS summary written or {review_path} saved), output the exact phrase
            on its own line: REVIEWER_COMPLETE
        """

    elif phase == "cleanup":
        session_dir = str(Path(plan_path).parent)
        return f"""
            You are a Cleanup Agent. Your sole job is to tidy the working directory now that
            planning, implementation, testing, and review are done for this pass. You do not
            touch the deliverable's own logic or content.
            {rules}

            1. {session_dir} is this session's own bookkeeping folder ({plan_path}, review.md,
               NotesForReviewer.md, feedback_to_plan.md, context.json, history.jsonl.gz,
               metadata.json, run.log, llm_debug.jsonl, .lock). NEVER delete, move, or overwrite
               anything already inside it — that is how this run tracks itself, and the next
               phase depends on it being intact.
            2. Use execute_command (e.g. `find`, `ls -la`, `git status --short` if this is a
               git repo) to inspect the REST of the working directory for files that are not
               part of the finished deliverable: throwaway debug scripts, one-off screenshots
               or renders taken to eyeball something, scratch notes, leftover scaffolding
               directories, stray logs — anything created along the way that has no business
               sitting in the project root once the work is done.
            3. For each such file, decide:
               - Worth keeping for reference (a screenshot the reviewer looked at, a debug
                 script that helped diagnose something)? Move it into {session_dir}/ with
                 execute_command's `mv`, so it survives without littering the deliverable.
               - Pure noise (empty temp directories, editor swap files, accidentally-created
                 cache/log files with no reference value)? Delete it outright with
                 execute_command's `rm`.
            4. NEVER touch: .git, the deliverable's own tracked source/tests/docs, dependency
               directories (node_modules, venv, __pycache__ a build still needs), or anything
               you are not sure about — when in doubt, leave it exactly where it is.
            5. Never ask the user a question and never wait for approval.

            When the working directory is tidy — or it already was, and nothing needed moving
            or removing — output the exact phrase on its own line: CLEANUP_COMPLETE
        """

    return ""


def get_phase_trigger(phase: str, goal: str = "", plan_path: str = DEFAULT_PLAN_PATH, iteration: int = 1,
                      review_path: Optional[str] = None, po_feedback_path: Optional[str] = None) -> str:
    """The opening human turn that kicks off a phase."""
    if phase == "planner":
        if iteration > 1 or review_path or po_feedback_path:
            trigger = (
                f"Update the plan at {plan_path} so it covers the request above.\n"
                f"Read it first, keep every '- [x]' line untouched, and append new '- [ ]' items "
                f"for the new work, continuing the numbering."
            )
            if review_path:
                trigger += (
                    f"\nThe previous iteration's review FAILED — its full report is quoted in the "
                    f"feedback message above (the reviewer saved it to {review_path} and it has "
                    f"since been archived). Add one new '- [ ]' item per issue to fix it — do NOT "
                    f"touch any already-ticked '- [x]' lines."
                )
            if po_feedback_path:
                trigger += (
                    f"\nThe Product Owner reviewed this plan against the real repo state and had "
                    f"concerns — the full feedback is quoted in the message above (saved to "
                    f"{po_feedback_path}, since archived). Update the plan to address every point "
                    f"raised before implementation starts — do NOT touch any already-ticked "
                    f"'- [x]' lines."
                )
            return trigger
        return (
            f"My goal is: {goal}\n\n"
            f"Write the step-by-step plan to {plan_path} now, using the mandated '- [ ]' format."
        )

    if phase == "product_owner":
        feedback_path = str(Path(plan_path).with_name("feedback_to_plan.md"))
        return (
            f"Planning is done. Review {plan_path} against the ACTUAL current state of this "
            f"repository before any implementation starts. If it's ready, just reply with a short "
            f"'Product Owner: APPROVED' summary — do not write any file. Only if you find real "
            f"problems (checked against the real repo, not just the plan's own claims), write "
            f"them to {feedback_path} as concrete, actionable feedback naming the specific plan "
            f"item number, which sends the plan back to the planner for one more pass before you "
            f"review it again. Do not ask for approval."
        )

    if phase == "imp":
        return (
            f"Begin implementation. Work through the unchecked '- [ ]' Implementation items in "
            f"{plan_path} one at a time, ticking each one with replace_in_file the moment you "
            f"finish it."
        )

    if phase == "testing":
        return (
            f"Implementation is done. Work through the unchecked '- [ ]' Testing items in "
            f"{plan_path} one at a time, fixing anything that fails and ticking each item as it "
            f"passes."
        )

    if phase == "reviewer":
        review_path = str(Path(plan_path).with_name("review.md"))
        return (
            f"Testing is done. Evaluate the finished work against {plan_path}. If it is good, do NOT "
            f"write any file — just reply with a short 'Review: PASS' summary. Only if problems "
            f"exist, write them to {review_path} as concrete, actionable issues (file/line references "
            f"where relevant), which triggers another planner → imp → testing → reviewer iteration. "
            f"Do not ask for approval."
        )

    if phase == "cleanup":
        session_dir = str(Path(plan_path).parent)
        return (
            f"Review has landed for this pass. Scan the working directory (excluding .git "
            f"and {session_dir}) for stray files that aren't part of the deliverable. Move "
            f"anything worth keeping into {session_dir}/ for reference, delete the rest, and "
            f"leave the deliverable's own files untouched."
        )

    return "Please continue."


# --- context compression helpers -------------------------------------------

_CORE_TOOLS_TOKENS_CACHE: Optional[int] = None
_DEFERRED_TOOL_TOKENS_CACHE: Dict[str, int] = {}


def _tools_schema_tokens(unlocked_tools=()) -> int:
    """Estimated token cost of the tool schemas actually sent with a
    request: CORE_TOOLS (always sent -- see tool/schemas.py) plus whichever
    DEFERRED_TOOLS this session has unlocked via load_tool.

    CORE_TOOLS' total is cached after the first call (a static constant,
    otherwise re-serialized to JSON on every :meth:`context_budget` call --
    every single turn once a session runs long enough to compress); each
    deferred tool's own size is cached the first time it's looked up too,
    since a session that unlocks one keeps asking about the same small set
    for the rest of its life.
    """
    global _CORE_TOOLS_TOKENS_CACHE
    if _CORE_TOOLS_TOKENS_CACHE is None:
        try:
            from JFI.tool.schemas import CORE_TOOLS
            _CORE_TOOLS_TOKENS_CACHE = len(json.dumps(CORE_TOOLS)) // 4
        except Exception:
            _CORE_TOOLS_TOKENS_CACHE = 0
    total = _CORE_TOOLS_TOKENS_CACHE
    if not unlocked_tools:
        return total
    try:
        from JFI.tool.schemas import DEFERRED_TOOLS
    except Exception:
        return total
    for name in unlocked_tools:
        if name not in _DEFERRED_TOOL_TOKENS_CACHE:
            tool = next((t for t in DEFERRED_TOOLS if t["function"]["name"] == name), None)
            _DEFERRED_TOOL_TOKENS_CACHE[name] = len(json.dumps(tool)) // 4 if tool else 0
        total += _DEFERRED_TOOL_TOKENS_CACHE[name]
    return total


# view_image attaches an image as a multimodal `content` list (OpenAI's
# vision format: [{"type": "image_url", ...}, ...]) instead of a plain
# string. A naive `str(content)` on that list would stringify the raw
# base64 payload — for a screenshot that's easily hundreds of thousands of
# "tokens" by the char/4 estimate, wildly overshooting the model's real
# per-image cost and forcing needless aggressive compression. Charge a fixed,
# provider-agnostic estimate per image instead; providers vary (roughly
# 85-1500 tokens depending on resolution/detail), so this is a deliberately
# rough middle-ground, not a per-provider calculation.
IMAGE_TOKEN_ESTIMATE = 800


def _content_char_cost(content) -> int:
    """Character-equivalent cost of one message's `content` for _estimate_tokens
    (the caller applies the final //4). Handles both a plain string and the
    multimodal list form (text parts counted normally, one flat charge per
    image part)."""
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                total += IMAGE_TOKEN_ESTIMATE * 4
            elif part.get("type") == "text":
                total += len(str(part.get("text") or ""))
        return total
    return len(str(content or ""))


def _estimate_tokens(messages) -> int:
    """Cheap character-based estimate; good enough to drive a budget."""
    total = 0
    for message in messages:
        total += _content_char_cost(message.get("content")) + 8
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            total += len(str(function.get("name") or ""))
            total += len(str(function.get("arguments") or ""))
    return total // 4


def _is_nudge(message) -> bool:
    return (
        message.get("role") == "user"
        and "when you are entirely finished with this phase" in str(message.get("content") or "")
    )


def _blocks(history):
    """
    Groups the history so that an assistant message carrying tool_calls stays
    glued to its tool results. Splitting those apart is rejected by the API.
    """
    grouped, current = [], []
    for message in history:
        if message.get("role") == "tool" and current:
            current.append(message)
            continue
        if current:
            grouped.append(current)
        current = [message]
    if current:
        grouped.append(current)
    return grouped


def _elide(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n… [{dropped} characters elided] …\n{text[-tail:]}"


DIGEST_MARKER = "[EARLIER HISTORY COMPRESSED TO SAVE CONTEXT]"


def _is_digest(block) -> bool:
    return any(DIGEST_MARKER in str(m.get("content") or "") for m in block)


def _trim_long_content(block) -> None:
    """Elides any message's long ``content`` string, whatever its role.

    This used to check ``role == "tool"`` only, on the theory that tool
    results (file dumps, command output) were the only thing worth shrinking.
    In practice a plain assistant report, a big pasted user goal, or the
    "USER FEEDBACK FOR ITERATION" block (which appends the whole tracked-file
    list) can just as easily be the single largest message in a block — and
    with the role check, none of that was ever touched by any tier, so a
    session with one oversized plain-text turn could stay over budget no
    matter how many tiers ran. Every other tier already accepts "lose some
    detail to fit" for tool output; plain text gets the same tradeoff here.
    """
    for message in block:
        content = message.get("content")
        if isinstance(content, list):
            # Multimodal (image) content: never stringify it — str(list)
            # would corrupt it into invalid API content, not a shorter
            # version of it. The image is still on disk; re-run view_image
            # if it's needed again, same tradeoff as an elided tool result.
            message["content"] = [{"type": "text", "text": "[image elided to save context]"}]
        elif content:
            message["content"] = _elide(str(content), TOOL_RESULT_HEAD, TOOL_RESULT_TAIL)


def _elide_payloads(block) -> None:
    """Strips write_file/append_to_file bodies; the bytes are already on disk."""
    for message in block:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") not in ("write_file", "append_to_file"):
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                continue
            content = str(args.get("content") or "")
            if len(content) <= TOOL_RESULT_HEAD:
                continue
            args["content"] = (
                f"<{len(content)} characters already written to "
                f"{args.get('file_path', 'the file')}; read_file it if you need them>"
            )
            function["arguments"] = json.dumps(args)


class SimpleSessionManager(SessionManager):
    """SessionManager backed by a plan.md checkbox tree plus gzip'd JSONL
    history on local disk -- see SessionManager for the interface contract
    this fulfills."""

    def __init__(self, console: AbstractManager, session_id: str):
        self.console: AbstractManager = console
        self.session_id = session_id.lower().replace(" ", "_")

        # Path logic handled entirely inside the manager
        os_session_path = os.environ.get("SESSION_PATH", ".")
        self.session_path = Path(os_session_path) / "JFI" / self.session_id
        self._lock_path = None
        self._acquire_session_lock()
        self.history_path = self.session_path / "history.jsonl.gz"
        # Old full-rewrite-per-message format; read-only, for one-time migration.
        self._legacy_history_path = self.session_path / "history.pkl"

        # New: Metadata tracking path
        self.metadata_path = self.session_path / "metadata.json"

        # Check if history exists BEFORE loading it
        self.is_resuming = self.history_path.exists() or self._legacy_history_path.exists()

        # How many of self.history's messages are already durably appended to
        # history_path — set by load_history(), advanced by save_history().
        self._flushed_count = 0
        self.history = self.load_history()
        self._repair_dangling_tool_calls()
        self.metadata = self.load_metadata()

        # Path the agent passes to the file tools (they are sandboxed to cwd).
        self.plan_path = self._resolve_plan_path()

        # A small persistent JSON scratchpad the model can read/write across
        # turns and phases — see CONTEXT_CACHE_RULES. Pre-created empty so a
        # read_file before anything's been remembered never errors.
        self.context_cache_path = self._resolve_session_file_path("context.json")
        self.ensure_context_cache_file()

        # Set by compress_history() every time it runs; see token_usage().
        self._last_sent_tokens = 0

        # phase -> BaseLLMStream, set post-construction by set_llm_streams()
        # once runner.py has built them (this class has no LLM access of its
        # own otherwise). Used only to LLM-summarize aged-out history in
        # compress_history's Tier 4 -- everything else about a session works
        # identically with this left empty (falls back to the cheap
        # tool/file/command digest, same as before summarization existed).
        self._llms = {}

        # Which of the tiered planner's 3 roles (see get_system_message's
        # planner_stage param) the NEXT get_messages("planner") call should
        # request -- None reproduces today's single combined planner prompt
        # unchanged. Set by runner.run_phase via set_planner_stage() before
        # each stage's own turn loop; irrelevant to every other phase.
        self._planner_stage: Optional[str] = None

    def set_planner_stage(self, stage: Optional[str]) -> None:
        """Selects which tiered-planner role (see get_system_message's
        planner_stage param) get_messages("planner") builds its system
        message for next -- "architect", "team_lead", "journeyman", or None
        for today's original single combined pass. Purely in-memory (not
        persisted): a resumed session restarts the planner phase from
        Architect regardless of which stage it was on before -- see the
        tiered-planner design note on resumability."""
        self._planner_stage = stage

    def set_llm_streams(self, llms: dict) -> None:
        """Wires in this session's phase -> BaseLLMStream map so Tier 4 of
        compress_history can ask the model to summarize aged-out history
        instead of only recording tool/file/command names. Optional: never
        called (e.g. in tests that construct a SessionManager directly)
        just means Tier 4 keeps using the cheap metadata-only digest."""
        self._llms = llms or {}

    # ------------------------------------------------------------ plan file

    def _resolve_session_file_path(self, filename: str) -> str:
        """
        Single source of truth for locating a file inside this session's JFI
        folder (the plan, the context cache, ...): made cwd-relative when
        possible so it works with the file tools (sandboxed to cwd).
        """
        target = (self.session_path / filename).resolve()
        cwd = Path.cwd().resolve()
        if target.is_relative_to(cwd):
            return str(target.relative_to(cwd))
        # Session dir lives outside the tool sandbox; express it relative to
        # cwd so the file still lands in JFI/<session>/<filename>.
        self.console.display_system(
            "Session path is outside the working directory — keeping "
            f"{filename} inside the JFI session folder."
        )
        return f"JFI/{self.session_id}/{filename}"

    def _resolve_plan_path(self) -> str:
        return self._resolve_session_file_path("plan.md")

    @property
    def plan_file(self) -> Path:
        """Absolute location of the plan file (always inside this session's JFI folder)."""
        return (self.session_path / "plan.md").resolve()

    # -------------------------------------------------------- context cache

    def _acquire_session_lock(self) -> None:
        """
        Exclusive OS-level lock (flock) on this session's own folder —
        refuses to run the same session_id twice at once, which would
        otherwise let two processes race on history.jsonl.gz/plan.md/
        context.json and corrupt them.

        Released automatically when this process exits or the underlying
        file handle is closed (see release_session_lock), even on a crash
        — flock is held by the OS against the open file description, not a
        stale PID file that would need its own cleanup/staleness logic. A
        no-op wherever fcntl isn't available (Windows).

        flock's exclusivity is scoped to the open file description, not the
        process: two independent open()s of the same path *in this same
        process* would otherwise block each other exactly like a genuinely
        different process would — which a second SimpleSessionManager for
        the same session_id, constructed within this process, legitimately
        does (a Ctrl+N handoff overlapping briefly, or simply building a
        fresh manager to inspect a session already open elsewhere in the
        same run). _SESSION_LOCKS refcounts by resolved lock path so only a
        genuinely different process ever gets refused.
        """
        if fcntl is None:
            return
        self.session_path.mkdir(parents=True, exist_ok=True)
        lock_path = str((self.session_path / ".lock").resolve())

        entry = _SESSION_LOCKS.get(lock_path)
        if entry is not None:
            entry[0] += 1
            self._lock_path = lock_path
            return

        lock_file = open(lock_path, "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            raise SessionInUseError(
                f"Session '{self.session_id}' is already running in another JFI process "
                f"(lock held on {lock_path}). Stop that run first, or pick a different "
                f"session name."
            )
        _SESSION_LOCKS[lock_path] = [1, lock_file]
        self._lock_path = lock_path

    def release_session_lock(self) -> None:
        """Releases this session's claim on its lock, if held — call once
        this SimpleSessionManager is done being used (see
        runner._run_session), so a later SimpleSessionManager (this
        process or another) can acquire it. The underlying OS lock is only
        actually released once every claim on it in this process has been
        released (see _acquire_session_lock)."""
        lock_path, self._lock_path = getattr(self, "_lock_path", None), None
        if lock_path is None:
            return
        entry = _SESSION_LOCKS.get(lock_path)
        if entry is None:
            return
        entry[0] -= 1
        if entry[0] > 0:
            return
        del _SESSION_LOCKS[lock_path]
        _, lock_file = entry
        try:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            lock_file.close()
        except OSError:
            pass

    @property
    def context_cache_file(self) -> Path:
        """Absolute location of the context cache (see CONTEXT_CACHE_RULES)."""
        return (self.session_path / "context.json").resolve()

    def ensure_context_cache_file(self) -> None:
        """Creates an empty context cache ("{}") if one doesn't exist yet, so
        the model's first read_file on it never errors with "does not exist"."""
        if not self.context_cache_file.exists():
            self.context_cache_file.write_text("{}\n", encoding="utf-8")

    def plan_progress(self):
        """(resolved, total) task-list checkboxes in the plan file. A
        user-skipped "- [○]" item counts as resolved alongside "- [x]" —
        it's no longer pending action, just not done by the model."""
        try:
            text = self.plan_file.read_text(encoding="utf-8")
        except Exception:
            return 0, 0
        done = len(re.findall(r"^[ \t]*[-*][ \t]*\[[xX]\]", text, re.M))
        skipped = len(re.findall(r"^[ \t]*[-*][ \t]*\[○\]", text, re.M))
        todo = len(re.findall(r"^[ \t]*[-*][ \t]*\[[ ]\]", text, re.M))
        return done + skipped, done + skipped + todo

    def phase_progress(self, phase: str):
        """(resolved, total) task-list checkboxes within just `phase`'s own
        section (e.g. imp -> "Implementation" only), same resolved/total
        rule as plan_progress but scoped instead of whole-file — so the
        console can show "3/19 this phase" alongside "3/35 overall".
        Returns (0, 0) for planner/reviewer, which have no per-item
        checklist of their own (see PHASE_SECTION)."""
        section = PHASE_SECTION.get(phase)
        if not section:
            return 0, 0
        try:
            text = self.plan_file.read_text(encoding="utf-8")
        except Exception:
            return 0, 0

        done = skipped = todo = 0
        in_section = False
        for line in text.splitlines():
            header = re.match(r"^\s*#{1,6}\s+(.*)", line)
            if header:
                in_section = header.group(1).strip().lower().startswith(section.lower())
                continue
            if not in_section:
                continue
            if re.match(r"^[ \t]*[-*][ \t]*\[[xX]\]", line):
                done += 1
            elif re.match(r"^[ \t]*[-*][ \t]*\[○\]", line):
                skipped += 1
            elif re.match(r"^[ \t]*[-*][ \t]*\[[ ]\]", line):
                todo += 1
        return done + skipped, done + skipped + todo

    def _pending_items(self, section: str) -> list[str]:
        """
        Returns the unchecked "- [ ]" task-list lines of one plan section (e.g.
        "Implementation" or "Testing"), in file order. Completed "- [x]" lines and
        items from other sections are excluded; a missing or empty plan file yields
        an empty list. Section headers may carry suffixes such as "(iteration 2)".
        """
        try:
            text = self.plan_file.read_text(encoding="utf-8")
        except Exception:
            return []

        pending, in_section = [], False
        for line in text.splitlines():
            header = re.match(r"^\s*#{1,6}\s+(.*)", line)
            if header:
                title = header.group(1).strip()
                # A section matches when it starts with the requested name (case-
                # insensitive), so "## Implementation (iteration 2)" still counts.
                in_section = title.lower().startswith(section.lower())
                continue
            item = re.match(r"^[ \t]*[-*][ \t]+\[[ ]\]\s+(.*)", line)
            if in_section and item:
                pending.append(line.strip())
        return pending

    def skip_current_task(self, phase: str) -> Optional[str]:
        """
        Ctrl+K: marks `phase`'s first unchecked item "- [○] ..." instead of
        ticking it — the user's own call that this one item is done with,
        not the model's. Only imp/testing have a checklist to skip from;
        returns None (no-op) for any other phase, or when nothing is
        pending. Returns the skipped item's description text on success.

        Edits the raw line directly (not through _pending_items' stripped
        copy) so original indentation is preserved exactly, the same
        byte-for-byte-except-the-marker discipline PLAN_FORMAT_RULES asks
        the model to follow for its own "- [x]" ticks.
        """
        section = PHASE_SECTION.get(phase)
        if not section:
            return None
        try:
            text = self.plan_file.read_text(encoding="utf-8")
        except Exception:
            return None

        lines = text.splitlines(keepends=True)
        in_section = False
        for i, raw_line in enumerate(lines):
            line = raw_line.splitlines()[0] if raw_line.splitlines() else ""
            header = re.match(r"^\s*#{1,6}\s+(.*)", line)
            if header:
                in_section = header.group(1).strip().lower().startswith(section.lower())
                continue
            item = re.match(r"^([ \t]*[-*][ \t]+)\[ \]([ \t]+.*)", line)
            if in_section and item:
                newline = "\n" if raw_line.endswith("\n") else ""
                lines[i] = item.group(1) + "[○]" + item.group(2) + newline
                self.plan_file.write_text("".join(lines), encoding="utf-8")
                return item.group(2).strip()
        return None

    def skip_remaining_tasks(self, phase: str) -> int:
        """
        Ctrl+Q: marks EVERY still-unchecked item in `phase`'s section
        "- [○] ..." in one pass — the bulk version of skip_current_task, for
        "I'm done reviewing this phase item by item, move on." Returns how
        many items were skipped (0 for a phase with no checklist, or one
        already clear).
        """
        section = PHASE_SECTION.get(phase)
        if not section:
            return 0
        try:
            text = self.plan_file.read_text(encoding="utf-8")
        except Exception:
            return 0

        lines = text.splitlines(keepends=True)
        in_section = False
        skipped = 0
        for i, raw_line in enumerate(lines):
            line = raw_line.splitlines()[0] if raw_line.splitlines() else ""
            header = re.match(r"^\s*#{1,6}\s+(.*)", line)
            if header:
                in_section = header.group(1).strip().lower().startswith(section.lower())
                continue
            item = re.match(r"^([ \t]*[-*][ \t]+)\[ \]([ \t]+.*)", line)
            if in_section and item:
                newline = "\n" if raw_line.endswith("\n") else ""
                lines[i] = item.group(1) + "[○]" + item.group(2) + newline
                skipped += 1
        if skipped:
            self.plan_file.write_text("".join(lines), encoding="utf-8")
        return skipped

    def current_task_title(self, phase: str, max_len: int = 140) -> Optional[str]:
        """
        The first unchecked item's descriptive text for `phase`'s section
        (e.g. "1.1 Verify .env loading happens before theme resolution..."),
        or None when the phase has no checkbox-driven task queue at all
        (planner/reviewer just work on the plan/report directly) or nothing
        is pending. Mirrors `_phase_system_message`'s own work-queue
        extraction, so this is exactly the item the model was just handed as
        its next one — for display in the console's status line, not the
        model's own self-reported "[CURRENT TASK: ...]" text, which would
        need parsing streamed output and could drift out of sync mid-turn.
        """
        section = PHASE_SECTION.get(phase)
        if not section:
            return None
        pending = self._pending_items(section)
        if not pending:
            return None
        match = re.match(r"^[ \t]*[-*][ \t]+\[[ ]\]\s*(.*)", pending[0])
        title = match.group(1).strip() if match else pending[0]
        if len(title) > max_len:
            title = title[:max_len - 1].rstrip() + "…"
        return title

    def ensure_plan_file(self) -> bool:
        """
        The planner writes the plan itself via the file tools; this only tracks a
        plan that already exists so progress shows up in the status bar. A missing
        or empty plan is never scraped from the transcript into markdown anymore.
        """
        if self.plan_file.exists() and not self.plan_file.stat().st_size:
            # Present but blank — the planner has not (yet) written anything.
            self.console.display_system(
                f"Warning: {self.plan_path} exists but is empty."
            )
            return False
        if self.plan_file.exists():
            self.track_file(self.plan_path)
            done, total = self.plan_progress()
            if total:
                self.console.display_system(
                    f"Plan ready at {self.plan_path} — {done}/{total} items ticked."
                )
                # Catches the planner drifting away from the two literal
                # "## Implementation"/"## Testing" headers phase_progress
                # depends on (a different name, a typo, its own extra
                # headers instead of nested bullets, ...) -- see
                # PLAN_FORMAT_RULES. Silent otherwise: the imp/testing phase
                # would just see an empty work queue with no explanation,
                # which is exactly what this is here to surface instead.
                for phase, section in PHASE_SECTION.items():
                    _, section_total = self.phase_progress(phase)
                    if not section_total:
                        self.console.display_system(
                            f"⚠️  Warning: no '## {section}' section found in {self.plan_path} "
                            f"(or it has no checkbox items) — the {phase_display_name(phase)} "
                            f"phase will see an empty work queue."
                        )
                return True
            self.console.display_system(
                f"Warning: {self.plan_path} exists but has no '- [ ]' items to track."
            )
        else:
            self.console.display_system(f"No plan file found at {self.plan_path}.")
        return False

    # ------------------------------------------------------------- metadata

    def load_metadata(self):
        """Loads project metadata like tracked files."""
        if self.metadata_path.exists():
            try:
                with open(self.metadata_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                self.console.display_system(f"Error loading metadata: {e}")
        return {"implemented_files": []}

    def save_metadata(self):
        """Saves project metadata to disk."""
        with open(self.metadata_path, "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=4)

    def track_file(self, file_path: str):
        """Registers a newly created or modified file into the project state."""
        if file_path and file_path not in self.metadata["implemented_files"]:
            self.metadata["implemented_files"].append(file_path)
            self.save_metadata()

    def load_queued_requests(self) -> list[str]:
        """Plain-queued (not yet consumed) console input from a prior run of
        this session, if any — see :meth:`save_queued_requests`."""
        return list(self.metadata.get("queued_requests", []))

    def save_queued_requests(self, items: list) -> None:
        """
        Persists the console's *current* queued-input contents to
        metadata.json, called by the console every time that queue changes
        (something typed in, or the whole queue drained/promoted).

        The queue used to live only in the console's in-memory
        ``queue.Queue`` — closing the process (even cleanly) lost anything
        the user had queued but the pipeline hadn't reached the "review
        landed, drain queue" point for yet. Persisting it here means
        `console.set_queue_store(ssm.load_queued_requests(), ...)` on the
        next resume can hand it right back.
        """
        self.metadata["queued_requests"] = list(items)
        self.save_metadata()

    def get_project_state_summary(self) -> str:
        """Returns a string listing all files currently tracked in the project."""
        files = self.metadata.get("implemented_files", [])
        if not files:
            return "No files have been tracked yet."

        summary = "CURRENT PROJECT FILES:\n"
        for f in files:
            summary += f"- {f}\n"
        summary += (
            "\n(Tip for AI: If you need to change anything, use the read_file tool to inspect "
            "these files first, then use replace_in_file for small edits, or write_file / "
            "append_to_file to rewrite them.)"
        )
        return summary

    # -------------------------------------------------------------- history

    def load_history(self):
        # Ensure the session folder exists
        self.session_path.mkdir(parents=True, exist_ok=True)

        if self.history_path.exists():
            history = _read_jsonl_gz(self.history_path)
            self._flushed_count = len(history)
            if history:
                self.console.display_system(
                    f"Resumed existing session '{self.session_id}' with {len(history)} past messages."
                )
            return history

        # No new-format file yet — migrate a legacy history.pkl if present.
        if self._legacy_history_path.exists():
            try:
                with open(self._legacy_history_path, "rb") as f:
                    history = pickle.load(f)
                self.console.display_system(
                    f"Resumed existing session '{self.session_id}' with {len(history)} past messages "
                    "(migrating history.pkl to the append-only history.jsonl.gz format)."
                )
                # Write it out under the new format immediately: every save
                # from here on appends instead of rewriting the whole thing.
                _append_jsonl_gz(self.history_path, history)
                self._flushed_count = len(history)
                return history
            except Exception as e:
                self.console.display_system(f"Error loading history: {e}. Starting fresh.")

        return []

    def _repair_dangling_tool_calls(self) -> None:
        """
        Fixes a specific broken resume state: the session was killed or
        crashed while runner.execute_tool_call's loop was mid-way through a
        multi-tool-call turn — the assistant's tool-call message and each
        tool's result are separate append_raw calls, so an interrupt can land
        after the assistant message but before any (or all) of its results.
        On resume the transcript then either ends with an assistant message
        carrying tool_calls with no results at all, or has one further along
        whose tool_calls only got SOME of their results recorded before the
        interrupt. Either way, at least one tool_call_id from the most recent
        assistant turn has no matching "role": "tool" reply anywhere after
        it — and most OpenAI-compatible servers reject the very next request
        outright with "Cannot continue an assistant message that contains
        tool calls", so the session could never resume at all.

        Synthesizes a failure tool-result for each unanswered tool_call_id
        from that turn, which makes the transcript structurally valid again
        and tells the model plainly what happened so it can check whether the
        work actually landed before retrying or moving on. Persisted
        immediately so the fix survives even if this run is interrupted again
        before the next real save.
        """
        if not self.history:
            return

        # Walk back to the most recent assistant message, if any; a plain
        # (non-tool-calls) reply after it, or no assistant message at all,
        # means there is nothing to repair.
        last_assistant = None
        last_assistant_idx = None
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i].get("role") == "assistant":
                last_assistant, last_assistant_idx = self.history[i], i
                break
        if last_assistant is None or not last_assistant.get("tool_calls"):
            return

        answered_ids = {
            m.get("tool_call_id")
            for m in self.history[last_assistant_idx + 1:]
            if m.get("role") == "tool"
        }
        missing = [c for c in last_assistant["tool_calls"] if c.get("id") not in answered_ids]
        if not missing:
            return

        self.console.display_system(
            f"⚠️  Last run stopped mid-turn ({len(missing)} tool call(s) made but never "
            "recorded a result) — synthesizing failure results so this session can resume."
        )
        for call in missing:
            function = call.get("function") or {}
            self.history.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "name": function.get("name", "unknown"),
                "content": (
                    "Error: this tool call was interrupted before its result was recorded "
                    "(the previous run stopped mid-turn — killed, crashed, or force-quit). "
                    "Treat it as not completed: verify whether the work was actually done "
                    "(e.g. read_file the target) before retrying or moving on."
                ),
            })
        self.save_history()

    def save_history(self):
        """
        Appends only the messages added since the last save, as one new gzip
        member — O(new messages), never O(total history already on disk).
        The old pickle version rewrote the entire history on every call,
        which made each turn's save cost grow with the whole session's size.
        """
        pending = self.history[self._flushed_count:]
        if not pending:
            return
        _append_jsonl_gz(self.history_path, pending)
        self._flushed_count = len(self.history)

    def add_message(self, role, message):
        """For simple text messages."""
        self.history.append({"role": role, "content": message})
        self.save_history()

    def append_raw(self, message_dict):
        """For complex dictionary messages (like tool calls) from the runner."""
        self.history.append(message_dict)
        self.save_history()

    def add_messages(self, messages):
        for message in messages:
            self.history.append(message)
        self.save_history()

    def get_messages(self, phase: str):
        message = [{"role": "system", "content": self._phase_system_message(phase)}]
        reserve = len(message[0]["content"]) // 4 + 256  # headroom for the pending-items block
        message.extend(self.compress_history(reserve=reserve, phase=phase))
        return message

    def _plan_format_rules(self) -> str:
        """Which PLAN FILE FORMAT rules text to send the model -- the generic,
        task-agnostic PLAN_FORMAT_RULES block by default. Overridden by
        AdaptiveSessionManager to swap in a smaller, task-type-specific
        block instead (see task_rules.py); everything else about how a
        session runs is unaffected by this hook."""
        return PLAN_FORMAT_RULES

    def _phase_system_message(self, phase: str) -> str:
        """System prompt for a phase plus its own work queue.

        The Implementation and Testing agents are handed exactly their unchecked
        items inline (from the plan file, parsed by `_pending_items`), so they do
        not need to scan the whole plan just to find what is left.
        """
        base = get_system_message(phase, self.plan_path, self.context_cache_path,
                                  plan_format_rules=self._plan_format_rules(),
                                  unlocked_tools=self.unlocked_tools(),
                                  planner_stage=self._planner_stage if phase == "planner" else None)

        # Auto-load whatever's been context_save()'d so far straight into the
        # system message -- which is rebuilt fresh every request and never
        # touched by compress_history -- instead of relying on the model to
        # remember to call context_lookup itself at the right moment (a
        # step we've watched it skip: it forgot server PIDs it had already
        # saved and had to context_lookup them back mid-session). Facts
        # still round-trip through context_save/context_lookup exactly as
        # before; this only adds a standing copy the model doesn't have to
        # go fetch.
        saved_facts = render_facts_for_auto_load(self.context_cache_path)
        if saved_facts:
            base = (
                f"{base}\n\n"
                f"SAVED CONTEXT (auto-loaded from context_save — already here, no need to "
                f"context_lookup these specific facts unless you want to search among many):\n"
                f"{saved_facts}"
            )

        section = PHASE_SECTION.get(phase)
        if phase in ("planner", "reviewer") or not section:
            return base

        pending = self._pending_items(section)
        queue = "\n".join(pending) if pending else f"(no unchecked {section} items found)"
        return (
            f"{base}\n\n"
            f"Your work queue — the currently unchecked {section} items in "
            f"{self.plan_path}, already extracted for you:\n"
            f"{queue}"
        )

    # ---------------------------------------------------------- compression

    def context_window(self, phase: str = "") -> int:
        """The model's full context window, for display.

        Resolved via phase_env exactly like MODEL/TEMPERATURE/OPENAI_URL
        (see PHASE_ENV_PREFIX in runner.py and base_llm_stream.phase_env):
        ``{PHASE}_CONTEXT_SIZE`` wins when set for this phase (e.g.
        TESTING_CONTEXT_SIZE), else the shared ``CONTEXT_SIZE``. This
        matters because a phase can already run a genuinely different model
        via its own *_MODEL/*_OPENAI_URL override -- without a matching
        per-phase context-size override, compress_history would budget
        every phase against one model's window even when another phase's
        real model has a smaller (risking silent truncation) or larger
        (risking needless over-compression) one. `phase` is the plain phase
        name ("planner", "imp", ...); its env prefix is just its upper-case
        form, same convention PHASE_ENV_PREFIX uses.

        Distinct from :meth:`context_budget`, which is the smaller, ratio-
        reduced threshold that actually triggers compression.
        """
        try:
            return int(phase_env(phase.upper() if phase else "", "CONTEXT_SIZE", str(DEFAULT_CONTEXT_SIZE)))
        except ValueError:
            return DEFAULT_CONTEXT_SIZE

    def context_budget(self, phase: str = "") -> int:
        try:
            size = self.context_window(phase)
        except ValueError:
            size = DEFAULT_CONTEXT_SIZE
        try:
            ratio = float(os.environ.get("CONTEXT_COMPRESSION_RATIO", DEFAULT_CONTEXT_RATIO))
        except ValueError:
            ratio = DEFAULT_CONTEXT_RATIO
        # The tool schemas are serialized into every request right alongside
        # the messages (see OpenAICompatableStream.send_message's `tools=`), so they
        # count against the same context window even though compress_history
        # never sees them. Without this, "compressed to fit" could still be
        # wrong by a few hundred tokens on a small CONTEXT_SIZE. Only CORE_TOOLS
        # plus whatever this session has unlocked are ever actually sent (see
        # tool/schemas.py's CORE_TOOLS/DEFERRED_TOOLS split and runner._tools_for_session).
        return max(1024, int(size * ratio) - _tools_schema_tokens(self.unlocked_tools()))

    def estimate_request_tokens(self, messages) -> int:
        """Estimated size of one outgoing request: `messages` plus the tool
        schema overhead sent alongside every call (see :meth:`context_budget`)."""
        return _estimate_tokens(messages) + _tools_schema_tokens(self.unlocked_tools())

    def unlocked_tools(self) -> list[str]:
        """Deferred tool names (see tool/schemas.py's DEFERRED_TOOLS) this
        session has unlocked via load_tool -- persisted so a resumed
        session doesn't have to re-unlock the same tool every time."""
        return list(self.metadata.get("unlocked_tools", []))

    def unlock_tool(self, name: str) -> bool:
        """Adds `name` to this session's unlocked deferred tools (idempotent
        -- unlocking an already-unlocked tool is a no-op, not an error).
        Returns False when `name` isn't a real deferred tool name, so the
        load_tool handler can report that back to the model instead of
        silently accepting a typo."""
        from JFI.tool.schemas import DEFERRED_TOOL_NAMES
        if name not in DEFERRED_TOOL_NAMES:
            return False
        unlocked = self.metadata.setdefault("unlocked_tools", [])
        if name not in unlocked:
            unlocked.append(name)
            self.save_metadata()
        return True

    def token_usage(self, phase: str = "") -> tuple[int, int]:
        """(estimated tokens in the *last request actually sent*, context window).

        This used to report ``_estimate_tokens(self.history)`` — the raw,
        ever-growing transcript on disk — instead of what compression had
        already trimmed it down to. On a long session those diverge hugely
        (the header was seen showing "566.9k/32.8k (100%)" while the very
        same turn's compression log line right below it read "~566357 ->
        ~7341 tokens"), so the counter looked permanently, alarmingly
        over-budget even though the actual request was comfortably within
        it. :attr:`_last_sent_tokens` is updated by :meth:`compress_history`
        every time it runs, so this now matches those log lines exactly.

        `phase` picks whose context window to report (see
        :meth:`context_window`) -- omit it (as any caller outside an active
        phase does) to report the shared default.
        """
        return self._last_sent_tokens, self.context_window(phase)

    def compress_history(self, reserve: int = 0, phase: str = ""):
        """
        Returns a view of the history that fits the context budget.

        The full transcript always stays on disk; only what we hand to the model
        shrinks. Compression runs in tiers, cheapest loss first, and stops as
        soon as the estimate fits. An assistant message carrying tool_calls is
        never separated from its results, at any tier.

        `phase` (optional, empty by default) picks which of self._llms'
        streams Tier 4 may use to LLM-summarize aged-out history instead of
        only recording tool/file/command names — see _build_digest.
        """
        budget = max(512, self.context_budget(phase) - reserve)
        original = _estimate_tokens(self.history)
        if original <= budget:
            self._last_sent_tokens = original
            return self.history

        blocks = _blocks(copy.deepcopy(self.history))

        def flat():
            return [m for block in blocks for m in block]

        def over():
            return _estimate_tokens(flat()) > budget

        def middle(keep_tail=KEEP_RECENT_BLOCKS):
            """Blocks we may edit: never the opening goal, never the live tail."""
            return blocks[1:-keep_tail] if len(blocks) > keep_tail + 1 else []

        # Tier 1: drop the stall-prevention nudges. Pure filler, repeated often.
        for block in middle():
            block[:] = [m for m in block if not _is_nudge(m)]

        # Snapshot the middle RIGHT AFTER Tier 1 (filler dropped, nothing else
        # touched yet) so Tier 4 can later summarize from this instead of from
        # `blocks` post-Tier-2/3. Tier 2/3 elide tool results and file payloads
        # down to a couple hundred chars each -- the right tradeoff for the
        # ACTUAL model request, which must fit the hard token budget, but dead
        # wrong for the digest's summarization call: that call has its own
        # separate, much larger budget (DIGEST_INPUT_CHAR_CAP), goes through an
        # LLM whose entire job is extracting durable facts from exactly this
        # material, and used to receive the SAME pre-elided ~600+400-char
        # scraps the live request got -- so a command's output (a discovered
        # file path, a function signature, a root-cause conclusion sitting past
        # character 600) could be gone before the summarizer ever saw it,
        # forcing the agent to re-run the same diagnostic commands turns later
        # with no memory of having already answered the question. Only Tier 1's
        # nudge-drop is safe to share between both paths: nudges are pure
        # filler no summary would ever want either.
        pretrim_blocks = copy.deepcopy(blocks)

        # Tier 2: trim long message content — tool results (file dumps, command
        # output) as well as plain assistant/user text (reports, big goals,
        # feedback blocks) — anything that's bulk rather than structure.
        if over():
            for block in middle():
                _trim_long_content(block)

        # Tier 3: elide file payloads already written to disk — they are
        # recoverable with read_file, so the bytes need not sit in context.
        if over():
            for block in middle():
                _elide_payloads(block)

        # Tier 4: collapse the remaining middle into a digest.
        blocks = [block for block in blocks if block]
        pretrim_blocks = [block for block in pretrim_blocks if block]
        if over() and len(blocks) > KEEP_RECENT_BLOCKS + 1:
            # The digest is a fixed-size summary whatever it covers, so there is
            # nothing to gain by digesting only part of the middle.
            head, tail = blocks[:1], blocks[-KEEP_RECENT_BLOCKS:]
            # pretrim_blocks was filtered by the identical "drop now-empty
            # blocks" predicate applied to the identical post-Tier-1 content,
            # so it stays index-aligned with `blocks` -- but if that ever stops
            # holding (a future tier reordering, etc.), fall back to the
            # already-elided slice rather than summarizing misaligned turns.
            digest_source = (
                pretrim_blocks[1:-KEEP_RECENT_BLOCKS]
                if len(pretrim_blocks) == len(blocks)
                else blocks[1:-KEEP_RECENT_BLOCKS]
            )
            blocks = head + [[self._build_digest(digest_source, phase)]] + tail

        # Tier 5: the protected tail alone can outgrow the budget. Trim it too,
        # leaving the most recent block untouched so the live turn stays exact.
        if over():
            for block in blocks[1:-1]:
                _trim_long_content(block)
                _elide_payloads(block)

        # Tier 6: last resort — shed whole blocks from the front. Dropping a
        # block at a time keeps every tool_calls/tool pair together, and we stop
        # before the opening goal is lost (it must survive to be re-read later).
        while over() and len(blocks) > 3:
            # Keep the digest: it is the only trace of everything already shed.
            index = 2 if _is_digest(blocks[1]) else 1
            del blocks[index]

        # Nothing left to give up but the newest turn's own bulk.
        if over() and blocks:
            _trim_long_content(blocks[-1])
            _elide_payloads(blocks[-1])

        result = flat()
        compressed = _estimate_tokens(result)
        self._last_sent_tokens = compressed
        note = f"~{original} → ~{compressed} tokens (budget {budget})"
        if compressed > budget:
            self.console.display_system(
                f"🗜  Context compressed to its floor: {note}. The newest turn alone exceeds "
                f"CONTEXT_SIZE — raise it if the model starts truncating."
            )
        else:
            self.console.display_system(f"🗜  Context compressed: {note}.")
        return result

    @staticmethod
    def _metadata_note(blocks) -> str:
        """Cheap, deterministic fallback note for a batch of blocks: which
        tools were called, which files touched, which commands run — no LLM
        call, no reasoning/conclusions. Used standalone when no LLM stream
        is available for this phase, and as the "not yet condensed" tail
        _build_digest appends for blocks too small to justify summarizing
        yet. Returns "" when the blocks carry no tool calls at all."""
        tools, files, commands = [], [], []
        for block in blocks:
            for message in block:
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    name = function.get("name") or "?"
                    tools.append(name)
                    try:
                        args = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        continue
                    if args.get("file_path"):
                        files.append(args["file_path"])
                    if name == "execute_command" and args.get("command"):
                        commands.append(args["command"])

        lines = []
        if files:
            lines.append("Files touched: " + ", ".join(dict.fromkeys(files)))
        if commands:
            lines.append("Commands run: " + "; ".join(dict.fromkeys(commands))[:600])
        if tools:
            counts = {name: tools.count(name) for name in dict.fromkeys(tools)}
            lines.append("Tool calls: " + ", ".join(f"{k}×{v}" for k, v in counts.items()))
        return "\n".join(lines)

    @staticmethod
    def _digest(blocks) -> dict:
        """Standalone cheap digest message (marker + _metadata_note only, no
        LLM summary, no persisted state) — kept for callers that just want
        the old metadata-only behavior for one batch of blocks in isolation."""
        note = SimpleSessionManager._metadata_note(blocks)
        lines = [
            DIGEST_MARKER,
            "This is a factual digest of turns that were removed. The files below are on disk; "
            "use read_file to inspect any of them, and the plan file remains the source of truth "
            "for what is done and what is left.",
        ]
        if note:
            lines.append(note)
        return {"role": "user", "content": "\n".join(lines)}

    @staticmethod
    def _render_blocks_for_summary(blocks) -> str:
        """Flattens a batch of blocks into plain text for an LLM
        summarization prompt: assistant reasoning/tool calls, tool results,
        and user notes, each lightly truncated per-message so one huge
        message can't dominate the summarizer's own input, then hard-capped
        overall (DIGEST_INPUT_CHAR_CAP) as a final backstop.

        Tool results get the largest per-message allowance of the three --
        they're where a command's actual output (a discovered path, a
        signature, a root cause) lives, and that's exactly the material this
        summary exists to keep the agent from having to re-discover. Assistant
        reasoning tends to restate/circle the same point at length, so it can
        afford a tighter cap without losing much."""
        parts = []
        for block in blocks:
            for message in block:
                role = message.get("role")
                content = message.get("content")
                if isinstance(content, list):
                    content = "[image]"
                text = str(content or "")
                if role == "assistant":
                    if text.strip():
                        parts.append(f"[assistant] {text[:700]}")
                    for call in message.get("tool_calls") or []:
                        function = call.get("function") or {}
                        args = str(function.get("arguments") or "")[:200]
                        parts.append(f"[tool call] {function.get('name') or '?'}({args})")
                elif role == "tool":
                    parts.append(f"[tool result] {text[:1500]}")
                elif text.strip():
                    parts.append(f"[{role}] {text[:500]}")
        rendered = "\n".join(parts)
        if len(rendered) > DIGEST_INPUT_CHAR_CAP:
            rendered = rendered[:DIGEST_INPUT_CHAR_CAP] + "\n… [older material in this batch truncated]"
        return rendered

    def _summarize_with_llm(self, llm, prior_summary: str, new_blocks) -> str:
        """One side-channel LLM call (same pattern as BaseLLMStream's own
        check_user_approval: an explicit messages list, consumed silently,
        never touching self.history) that folds `new_blocks` into an
        updated version of `prior_summary`. Raises on any failure or an
        empty response — callers must catch and fall back to the cheap
        metadata note; this must never be allowed to crash compression."""
        rendered = self._render_blocks_for_summary(new_blocks)
        system_msg = (
            "You are condensing part of a coding agent's own past work into a factual "
            "summary for that SAME agent to read later, after these original turns are deleted "
            "from its context to save space. Preserve concrete facts, conclusions, discovered "
            "bugs/gotchas, and decisions made — the kind of thing that would otherwise force the "
            "agent to re-investigate something it already figured out. Do not just list which "
            "tools were called; say what was LEARNED or DECIDED and why, with enough specificity "
            "(exact paths, names, numbers, error messages) that the agent never has to re-derive "
            "them. Favor completeness over brevity here — this is the ONLY trace of these turns "
            "once they're deleted, so a fact left out is gone, not just shortened. Up to roughly "
            "500 words is fine if the material genuinely needs it; don't pad to reach that, and "
            "don't cut a real fact just to stay under it. Plain prose or short bullet points, no "
            "markdown headers."
        )
        user_msg = rendered
        if prior_summary:
            user_msg = (
                f"Existing summary of even-earlier history (update/extend it, don't just repeat "
                f"it verbatim):\n{prior_summary}\n\n"
                f"Additional history to fold in now:\n{rendered}"
            )
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]
        response_stream = llm.send_message(messages)
        summary = ""
        for chunk in response_stream:
            delta = chunk.choices[0].delta.content
            if delta is not None:
                summary += delta
        summary = summary.strip()
        if not summary:
            raise ValueError("LLM summarization returned an empty response")
        return summary

    def _build_digest(self, middle_blocks, phase: str) -> dict:
        """Tier 4's actual digest builder: an LLM-summarized prose digest
        when possible, updated incrementally (never re-summarizing history
        already folded in), falling back to the cheap tool/file/command
        note — exactly today's behavior — whenever no LLM stream is
        available, the newly-aged-out batch is still too small to be worth
        a call, or the call itself fails.

        State (how much of `middle_blocks` the current summary already
        covers, plus the summary text itself) persists in self.metadata
        across turns AND across a resumed session, so a long run pays for
        incremental summarization, not one full re-summarize per turn.
        """
        current_count = len(middle_blocks)
        stored_count = self.metadata.get("digest_block_count", 0)
        stored_summary = self.metadata.get("digest_summary", "")

        if stored_summary and current_count <= stored_count:
            # Nothing new has aged out since the last summary — reuse it.
            return self._digest_message(stored_summary)

        # current_count < stored_count only if blocks were pruned out from
        # under us (e.g. Tier 1 emptied one) — resync from scratch rather
        # than index off a boundary that no longer means what it did.
        new_blocks = middle_blocks[stored_count:] if current_count > stored_count else middle_blocks

        llm = self._llms.get(phase)
        new_chars = sum(len(json.dumps(m, default=str)) for block in new_blocks for m in block)
        worth_summarizing = llm is not None and (new_chars >= DIGEST_SUMMARY_MIN_CHARS or not stored_summary)

        if worth_summarizing:
            try:
                updated = self._summarize_with_llm(llm, stored_summary, new_blocks)
                self.metadata["digest_summary"] = updated
                self.metadata["digest_block_count"] = current_count
                self.save_metadata()
                return self._digest_message(updated)
            except Exception as e:
                self.console.display_system(f"⚠️  Digest summarization failed, using metadata note instead: {e}")

        # Below threshold, no LLM available, or the call just failed: keep
        # the existing prose summary as-is (don't advance digest_block_count
        # — these blocks stay "new" and get a real shot at summarization
        # next time) and cheaply note what happened in the meantime so
        # nothing is silently invisible in between.
        pending_note = self._metadata_note(new_blocks)
        combined = stored_summary
        if pending_note:
            combined = (
                f"{combined}\n\n(Not yet condensed into the summary above:)\n{pending_note}"
                if combined else pending_note
            )
        return self._digest_message(combined)

    @staticmethod
    def _digest_message(body: str) -> dict:
        header = (
            f"{DIGEST_MARKER}\n"
            "This replaces turns removed to save context. The plan file remains the source of "
            "truth for what is done and what is left; files mentioned below are on disk and "
            "readable with read_file if you need more than this summary."
        )
        content = f"{header}\n\n{body}" if body else header
        return {"role": "user", "content": content}

    # --------------------------------------------------------------- phases

    def get_remaining_phases(self, all_phases: list[str]) -> list[str]:
        """
        Scans history to see which phases have already completed.
        Returns a sliced list starting from the first incomplete phase.
        """
        completed_phases = set()
        for msg in self.history:
            if msg.get("role") == "assistant" and msg.get("content"):
                for phase in all_phases:
                    if phase_completed(msg["content"], phase):
                        completed_phases.add(phase)

        # Find the first phase in sequence that hasn't completed yet
        for i, phase in enumerate(all_phases):
            if phase not in completed_phases:
                return all_phases[i:]

        # If all phases are already marked complete, return empty list
        return []


def _marker_present(content: str, keyword: str) -> bool:
    """
    True when `keyword` stands alone on its own line in `content` (markdown
    decoration -- *emphasis*, `code`, blockquote '>', a trailing '.'/'!' --
    tolerated around it). A plan that merely *mentions* the keyword in prose
    does not count: this is what phase_completed uses for '{PHASE}_COMPLETE'
    markers, and what runner.py's tiered-planner stages use for their own
    stage markers (ARCHITECT_STAGE_COMPLETE, TEAM_LEAD_STAGE_COMPLETE) --
    same rule, same regex, one place to keep them consistent.
    """
    if not content:
        return False
    for line in str(content).splitlines():
        if re.fullmatch(rf"[\s*_`#>-]*{keyword}[\s*_`.!:]*", line):
            return True
    return False


def phase_completed(content: str, phase: str) -> bool:
    """
    True when the model signed off on a phase.

    The keyword must stand alone on its own line: a plan that merely *mentions*
    'IMP_COMPLETE' in prose used to end the phase instantly.
    """
    return _marker_present(content, f"{phase.upper()}_COMPLETE")
