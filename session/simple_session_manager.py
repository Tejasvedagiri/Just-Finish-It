import copy
import json
import os
import pickle
import re
from pathlib import Path

from manager.abstract_manager import AbstractManager

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


# Default plan location: inside the .JFI session folder. The manager overrides this
# with its own resolved path (see SimpleSessionManager.plan_path); it is only used as a
# fallback when callers do not pass an explicit plan_path.
DEFAULT_PLAN_PATH = ".JFI/plan.md"


def get_system_message(phase: str, plan_path: str = DEFAULT_PLAN_PATH) -> str:
    rules = PLAN_FORMAT_RULES.format(plan_path=plan_path)

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
        return f"""
            You are an expert Reviewer Agent. You report on the finished work.
            You do NOT gate anything: you never ask for approval, never ask the user a question,
            and never wait for a reply.
            {rules}

            1. read_file {plan_path} and inspect the files that were produced.
            2. Write a short, concrete report: what was built, what was verified, and anything
               that is still incomplete or looks wrong.
            3. If an item is still "- [ ]" but is genuinely finished, tick it with replace_in_file.
               If it is genuinely unfinished, just say so plainly in your report.

            When the report is written, output the exact phrase on its own line: REVIEWER_COMPLETE
        """

    return ""


def get_phase_trigger(phase: str, goal: str = "", plan_path: str = DEFAULT_PLAN_PATH, iteration: int = 1) -> str:
    """The opening human turn that kicks off a phase."""
    if phase == "planner":
        if iteration > 1:
            return (
                f"Update the plan at {plan_path} so it covers the request above.\n"
                f"Read it first, keep every '- [x]' line untouched, and append new '- [ ]' items "
                f"for the new work, continuing the numbering."
            )
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
        return (
            f"Testing is done. Review the final state against {plan_path} and write your report. "
            f"Do not ask for approval."
        )

    return "Please continue."


# --- context compression helpers -------------------------------------------

def _estimate_tokens(messages) -> int:
    """Cheap character-based estimate; good enough to drive a budget."""
    total = 0
    for message in messages:
        total += len(str(message.get("content") or "")) + 8
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


def _trim_tool_results(block) -> None:
    for message in block:
        if message.get("role") == "tool":
            message["content"] = _elide(
                str(message.get("content") or ""), TOOL_RESULT_HEAD, TOOL_RESULT_TAIL
            )


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
        self.session_pickle_path = self.session_path / "history.pkl"

        # New: Metadata tracking path
        self.metadata_path = self.session_path / "metadata.json"

        # Check if history exists BEFORE loading it
        self.is_resuming = self.session_pickle_path.exists()

        self.history = self.load_history()
        self.metadata = self.load_metadata()

        # Path the agent passes to the file tools (they are sandboxed to cwd).
        self.plan_path = self._resolve_plan_path()

    # ------------------------------------------------------------ plan file

    def _resolve_plan_path(self) -> str:
        """
        Single source of truth for the plan location: it always lives inside this
        session's .JFI folder. The path is made cwd-relative when possible so it
        works with the file tools (which are sandboxed to the working directory).
        """
        plan = (self.session_path / "plan.md").resolve()
        cwd = Path.cwd().resolve()
        if plan.is_relative_to(cwd):
            return str(plan.relative_to(cwd))
        # Session dir lives outside the tool sandbox; express it relative to cwd
        # so the plan still lands in .JFI/<session>/plan.md.
        self.console.display_system(
            "Session path is outside the working directory — keeping the plan inside the "
            ".JFI session folder."
        )
        return f".JFI/{self.session_id}/plan.md"

    @property
    def plan_file(self) -> Path:
        """Absolute location of the plan file (always inside this session's .JFI folder)."""
        return (self.session_path / "plan.md").resolve()

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

        # Check if history.pkl exists for this specific session
        if self.session_pickle_path.exists():
            try:
                with open(self.session_pickle_path, "rb") as f:
                    history = pickle.load(f)
                    self.console.display_system(
                        f"Resumed existing session '{self.session_id}' with {len(history)} past messages.")
                    return history
            except Exception as e:
                self.console.display_system(f"Error loading history: {e}. Starting fresh.")
        return []

    def save_history(self):
        """Dedicated method to write the history to disk."""
        with open(self.session_pickle_path, "wb") as f:
            pickle.dump(self.history, f)

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
        base = get_system_message(phase, self.plan_path)
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

    def context_budget(self) -> int:
        try:
            size = int(os.environ.get("CONTEXT_SIZE", DEFAULT_CONTEXT_SIZE))
        except ValueError:
            size = DEFAULT_CONTEXT_SIZE
        try:
            ratio = float(os.environ.get("CONTEXT_COMPRESSION_RATIO", DEFAULT_CONTEXT_RATIO))
        except ValueError:
            ratio = DEFAULT_CONTEXT_RATIO
        return max(1024, int(size * ratio))

    def compress_history(self, reserve: int = 0):
        """
        Returns a view of the history that fits the context budget.

        The full transcript always stays on disk; only what we hand to the model
        shrinks. Compression runs in tiers, cheapest loss first, and stops as
        soon as the estimate fits. An assistant message carrying tool_calls is
        never separated from its results, at any tier.
        """
        budget = max(512, self.context_budget() - reserve)
        if _estimate_tokens(self.history) <= budget:
            return self.history

        original = _estimate_tokens(self.history)
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

        # Tier 2: trim long tool results (file dumps, command output).
        if over():
            for block in middle():
                _trim_tool_results(block)

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
                _trim_tool_results(block)
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
            _trim_tool_results(blocks[-1])
            _elide_payloads(blocks[-1])

        result = flat()
        compressed = _estimate_tokens(result)
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
