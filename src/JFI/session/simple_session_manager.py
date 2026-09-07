import copy
import gzip
import json
import os
import pickle
import re
from pathlib import Path
from typing import Optional

from JFI.manager.abstract_manager import AbstractManager

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
KEEP_RECENT_BLOCKS = 6
TOOL_RESULT_HEAD = 600
TOOL_RESULT_TAIL = 400

PLAN_FORMAT_RULES = """
    PLAN FILE FORMAT (mandatory, no exceptions):
    - The plan file is exactly: {plan_path}
    - Every actionable item MUST be a GitHub task-list line and nothing else:
          - [ ] 1.1 Short description of the step
      Not started is "- [ ] ", finished is "- [x] ".
      NEVER use any other marker for progress: no U+2610 ballot boxes, no emoji
      ticks, no tables of checkboxes. Only "- [ ]" and "- [x]".
    - Numbering: sections are 1, 2, 3 ...; steps inside them are 1.1, 1.2 ...
    - Item text must stay byte-identical when you tick it: change only the
      space inside the brackets to an x, so a targeted replace can find it.
"""

CONTEXT_CACHE_RULES = """
    CONTEXT CACHE (optional, persists across turns and phases):
    - A small JSON file at {context_cache_path} holds facts worth remembering
      that would otherwise be lost once older turns are compressed out of your
      context: key decisions, discovered schema/API/config details, gotchas —
      anything a later step or phase would otherwise have to re-derive.
    - It already exists (starts as "{{}}"); read_file it whenever you need
      context from earlier work, in this phase or an earlier one.
    - To add or update a fact: read_file it first, then write_file the whole
      file back with your fact merged in. It stays a small flat JSON object,
      e.g. {{"db_schema": "users table: id, email, created_at"}}. Keep it
      small — a handful of high-value facts, not a transcript — and never
      remove another entry just because you didn't write it.
"""


# Default plan location: inside the .JFI session folder. The manager overrides this
# with its own resolved path (see SimpleSessionManager.plan_path); it is only used as a
# fallback when callers do not pass an explicit plan_path.
DEFAULT_PLAN_PATH = ".JFI/plan.md"
DEFAULT_CONTEXT_CACHE_PATH = ".JFI/context.json"


def get_system_message(phase: str, plan_path: str = DEFAULT_PLAN_PATH,
                       context_cache_path: str = DEFAULT_CONTEXT_CACHE_PATH) -> str:
    rules = (
        PLAN_FORMAT_RULES.format(plan_path=plan_path)
        + CONTEXT_CACHE_RULES.format(context_cache_path=context_cache_path)
    )

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
                   - [ ] 1.1 ...
                   - [ ] 1.2 ...
                   ## Testing
                   - [ ] 2.1 ...
            2. If {plan_path} ALREADY EXISTS, read_file it first. Every "- [x]" line is work that
               is already finished: leave those lines exactly as they are. Add new "- [ ]" items
               for the new request, continuing the existing numbering.
            3. Do NOT implement anything in this phase. Write the plan file only.
            4. Never ask the user a question and never wait for approval.

            When the plan file is saved, output the exact phrase on its own line: PLANNER_COMPLETE
        """

    elif phase == "imp":
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
               execute_command).
            4. IMMEDIATELY tick that one item, using replace_in_file on {plan_path}:
                   old_string: "- [ ] 1.1 Short description of the step"
                   new_string: "- [x] 1.1 Short description of the step"
               Tick exactly one box per step, right after finishing that step. Do NOT batch the
               ticks until the end, and do NOT rewrite the whole plan file just to tick a box.
            5. Repeat from step 1 until no unchecked Implementation items are left.

            If a tool or command returns an error, fix the cause and retry it before moving on.
            Leave the Testing items alone; that is the next phase's job.
            Never ask the user a question and never wait for approval.

            When every Implementation item reads "- [x]", output the exact phrase on its own
            line: IMP_COMPLETE
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
            3. Run it with execute_command, or read the produced files to verify them.
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
        return f"""
            You are an expert Reviewer Agent. You evaluate the finished work and decide whether it
            needs another iteration of planner → imp → testing → reviewer.
            You do NOT gate anything: you never ask for approval, never ask the user a question,
            and never wait for a reply.
            {rules}

            1. read_file {plan_path}, then inspect the files that were produced (and re-run any
               tests or commands needed to judge them).
            2. Decide: is the work good — every planned item genuinely done, verified, and free of
               defects?
            3. If the review is GOOD: do NOT write or touch {review_path}. Just output a short
               "Review: PASS" summary in your reply (what was built, what was verified).
            4. Only if problems exist (broken/unfinished work, failing tests, missing pieces):
               use write_file to create {review_path} with concrete, actionable issue descriptions —
               one numbered item per problem, each naming the file(s) and line(s) involved where
               relevant, plus how to fix it. The next planner iteration will read that file and add
               new plan items from it, so be specific.

            When you are done (PASS summary written or {review_path} saved), output the exact phrase
            on its own line: REVIEWER_COMPLETE
        """

    return ""


def get_phase_trigger(phase: str, goal: str = "", plan_path: str = DEFAULT_PLAN_PATH, iteration: int = 1,
                      review_path: Optional[str] = None) -> str:
    """The opening human turn that kicks off a phase."""
    if phase == "planner":
        if iteration > 1:
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
            return trigger
        return (
            f"My goal is: {goal}\n\n"
            f"Write the step-by-step plan to {plan_path} now, using the mandated '- [ ]' format."
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

    return "Please continue."


# --- context compression helpers -------------------------------------------

_TOOLS_SCHEMA_TOKENS_CACHE: Optional[int] = None


def _tools_schema_tokens() -> int:
    """Estimated token cost of the tool schemas sent with every request.

    Cached after the first call: the schema list is a static constant, and
    this otherwise gets re-serialized to JSON on every :meth:`context_budget`
    call (every single turn, once a session runs long enough to compress).
    """
    global _TOOLS_SCHEMA_TOKENS_CACHE
    if _TOOLS_SCHEMA_TOKENS_CACHE is None:
        try:
            from JFI.tool.schemas import AVAILABLE_TOOLS
            _TOOLS_SCHEMA_TOKENS_CACHE = len(json.dumps(AVAILABLE_TOOLS)) // 4
        except Exception:
            _TOOLS_SCHEMA_TOKENS_CACHE = 0
    return _TOOLS_SCHEMA_TOKENS_CACHE


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


class SimpleSessionManager:
    def __init__(self, console: AbstractManager, session_id: str):
        self.console: AbstractManager = console
        self.session_id = session_id.lower().replace(" ", "_")

        # Path logic handled entirely inside the manager
        os_session_path = os.environ.get("SESSION_PATH", ".")
        self.session_path = Path(os_session_path) / ".JFI" / self.session_id
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

    # ------------------------------------------------------------ plan file

    def _resolve_session_file_path(self, filename: str) -> str:
        """
        Single source of truth for locating a file inside this session's .JFI
        folder (the plan, the context cache, ...): made cwd-relative when
        possible so it works with the file tools (sandboxed to cwd).
        """
        target = (self.session_path / filename).resolve()
        cwd = Path.cwd().resolve()
        if target.is_relative_to(cwd):
            return str(target.relative_to(cwd))
        # Session dir lives outside the tool sandbox; express it relative to
        # cwd so the file still lands in .JFI/<session>/<filename>.
        self.console.display_system(
            "Session path is outside the working directory — keeping "
            f"{filename} inside the .JFI session folder."
        )
        return f".JFI/{self.session_id}/{filename}"

    def _resolve_plan_path(self) -> str:
        return self._resolve_session_file_path("plan.md")

    @property
    def plan_file(self) -> Path:
        """Absolute location of the plan file (always inside this session's .JFI folder)."""
        return (self.session_path / "plan.md").resolve()

    # -------------------------------------------------------- context cache

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
        """(ticked, total) task-list checkboxes in the plan file."""
        try:
            text = self.plan_file.read_text(encoding="utf-8")
        except Exception:
            return 0, 0
        done = len(re.findall(r"^[ \t]*[-*][ \t]*\[[xX]\]", text, re.M))
        todo = len(re.findall(r"^[ \t]*[-*][ \t]*\[[ ]\]", text, re.M))
        return done, done + todo

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
        section = {"imp": "Implementation", "testing": "Testing"}.get(phase)
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
        message.extend(self.compress_history(reserve=reserve))
        return message

    def _phase_system_message(self, phase: str) -> str:
        """System prompt for a phase plus its own work queue.

        The Implementation and Testing agents are handed exactly their unchecked
        items inline (from the plan file, parsed by `_pending_items`), so they do
        not need to scan the whole plan just to find what is left.
        """
        base = get_system_message(phase, self.plan_path, self.context_cache_path)
        section = {"imp": "Implementation", "testing": "Testing"}.get(phase)
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

    def context_window(self) -> int:
        """The model's full context window (``CONTEXT_SIZE``), for display.

        Distinct from :meth:`context_budget`, which is the smaller, ratio-
        reduced threshold that actually triggers compression.
        """
        try:
            return int(os.environ.get("CONTEXT_SIZE", DEFAULT_CONTEXT_SIZE))
        except ValueError:
            return DEFAULT_CONTEXT_SIZE

    def context_budget(self) -> int:
        try:
            size = self.context_window()
        except ValueError:
            size = DEFAULT_CONTEXT_SIZE
        try:
            ratio = float(os.environ.get("CONTEXT_COMPRESSION_RATIO", DEFAULT_CONTEXT_RATIO))
        except ValueError:
            ratio = DEFAULT_CONTEXT_RATIO
        # The tool schemas are serialized into every request right alongside
        # the messages (see ColibriLLMStream.send_message's `tools=`), so they
        # count against the same context window even though compress_history
        # never sees them. Without this, "compressed to fit" could still be
        # wrong by a few hundred tokens on a small CONTEXT_SIZE.
        return max(1024, int(size * ratio) - _tools_schema_tokens())

    def estimate_request_tokens(self, messages) -> int:
        """Estimated size of one outgoing request: `messages` plus the tool
        schema overhead sent alongside every call (see :meth:`context_budget`)."""
        return _estimate_tokens(messages) + _tools_schema_tokens()

    def token_usage(self) -> tuple[int, int]:
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
        """
        return self._last_sent_tokens, self.context_window()

    def compress_history(self, reserve: int = 0):
        """
        Returns a view of the history that fits the context budget.

        The full transcript always stays on disk; only what we hand to the model
        shrinks. Compression runs in tiers, cheapest loss first, and stops as
        soon as the estimate fits. An assistant message carrying tool_calls is
        never separated from its results, at any tier.
        """
        budget = max(512, self.context_budget() - reserve)
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

        # Tier 4: collapse the remaining middle into a factual digest.
        blocks = [block for block in blocks if block]
        if over() and len(blocks) > KEEP_RECENT_BLOCKS + 1:
            # The digest is a fixed-size summary whatever it covers, so there is
            # nothing to gain by digesting only part of the middle.
            head, tail = blocks[:1], blocks[-KEEP_RECENT_BLOCKS:]
            blocks = head + [[self._digest(blocks[1:-KEEP_RECENT_BLOCKS])]] + tail

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
    def _digest(blocks) -> dict:
        """Collapses old blocks into one factual note about what happened."""
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

        lines = [
            DIGEST_MARKER,
            "This is a factual digest of turns that were removed. The files below are on disk; "
            "use read_file to inspect any of them, and the plan file remains the source of truth "
            "for what is done and what is left.",
        ]
        if files:
            lines.append("Files touched: " + ", ".join(dict.fromkeys(files)))
        if commands:
            lines.append("Commands run: " + "; ".join(dict.fromkeys(commands))[:600])
        if tools:
            counts = {name: tools.count(name) for name in dict.fromkeys(tools)}
            lines.append("Tool calls: " + ", ".join(f"{k}×{v}" for k, v in counts.items()))
        return {"role": "user", "content": "\n".join(lines)}

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


def phase_completed(content: str, phase: str) -> bool:
    """
    True when the model signed off on a phase.

    The keyword must stand alone on its own line: a plan that merely *mentions*
    'IMP_COMPLETE' in prose used to end the phase instantly.
    """
    if not content:
        return False
    keyword = f"{phase.upper()}_COMPLETE"
    for line in str(content).splitlines():
        if re.fullmatch(rf"[\s*_`#>-]*{keyword}[\s*_`.!:]*", line):
            return True
    return False
