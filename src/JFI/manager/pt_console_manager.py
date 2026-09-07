from __future__ import annotations

import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.styles import Style

from JFI.manager.abstract_manager import AbstractManager, phase_display_name
from JFI.manager.key_bindings import add_shift_enter_newline, teach_terminal_shift_enter
from JFI.manager.theme_env import resolve_explicit_theme, theme_label

teach_terminal_shift_enter()

# ANSI colour names are used on purpose: they inherit the user's terminal
# palette, so the UI stays readable on both light and dark backgrounds. This is
# the base style; THEME env presets (see PT_THEME_PRESETS below) only override
# the message-role keys (out.user / out.assistant(.tag) / out.system) on top
# of it, so UI chrome (header, status, rule, prompt) stays consistent.
UI_STYLE_BASE: Dict[str, str] = {
    "header": "bold ansicyan",
    "header.dim": "ansibrightblack",
    "header.phase": "ansibrightblack",
    "header.phase.active": "bold ansimagenta",
    "header.phase.done": "ansigreen",
    "header.loop": "bold ansiyellow",
    "header.tokens": "ansibrightblack",
    "header.tokens.warn": "bold ansired",
    "tasktitle.phase": "bold ansimagenta",
    "tasktitle.dim": "ansibrightblack",
    "tasktitle": "ansiwhite",
    "rule": "ansibrightblack",
    "status": "ansibrightblack",
    "status.queue": "bold ansiyellow",
    "status.forced": "bold ansimagenta",
    "status.busy": "bold ansigreen",
    "status.wait": "bold ansicyan",
    "status.locked": "bold ansired",
    "prompt": "bold ansigreen",
    "out.user": "bold ansicyan",
    "out.assistant": "ansigreen",
    "out.assistant.tag": "bold ansigreen",
    "out.system": "ansibrightblack",
    "out.tool": "ansiyellow",
    "out.result": "ansibrightblack",
    "out.error": "bold ansired",
    "out.rule": "bold ansibrightblack",
}

# Per-preset overrides for the three message-role colors, keyed the same as
# README's THEME table. Colors here are literal hex so they render the same
# regardless of the user's terminal ANSI palette (unlike the ansi* names in
# UI_STYLE_BASE, which deliberately inherit it). "dark-default" overrides
# nothing: it *is* the ansi*-based baseline above, so a THEME-less run and
# THEME=dark-default look identical.
PT_THEME_PRESETS: Dict[str, Dict[str, str]] = {
    "dark-default": {},
    "dark-ocean": {
        "out.user": "bold #5fafff",
        "out.assistant": "#00afaf",
        "out.assistant.tag": "bold #00afaf",
        "out.system": "#5f5fbe",
    },
    "dark-mono": {
        "out.user": "bold #ffffff",
        "out.assistant": "#d0d0d0",
        "out.assistant.tag": "bold #d0d0d0",
        "out.system": "#808080",
    },
    "light-default": {
        "out.user": "bold #0000ff",
        "out.assistant": "bold #006400",
        "out.assistant.tag": "bold #006400",
        "out.system": "#444444",
    },
    "light-sunrise": {
        "out.user": "bold #af00af",
        "out.assistant": "#870000",
        "out.assistant.tag": "bold #870000",
        "out.system": "#87875f",
    },
    "light-paper": {
        "out.user": "#00005f",
        "out.assistant": "#5f8767",
        "out.assistant.tag": "bold #5f8767",
        "out.system": "#808080",
    },
}


def _check_light_env() -> bool:
    """Common environment hint for a light-themed terminal (fg;bg COLORFGBG)."""
    colorfgbg = os.environ.get("COLORFGBG", "")
    if colorfgbg and ";" in colorfgbg:
        try:
            return int(colorfgbg.split(";")[-1]) >= 7
        except ValueError:
            pass
    return False


def resolve_pt_theme() -> tuple[Dict[str, str], Optional[str]]:
    """Picks the live console's style overrides and reports how they were chosen.

    An explicit THEME preset always wins; empty/"auto" falls back to
    COLORFGBG detection; an unknown name logs a hint and falls back too, so a
    typo can never crash startup (see ``theme_env.resolve_explicit_theme``
    for the shared normalization/precedence rules).
    """
    explicit = resolve_explicit_theme(PT_THEME_PRESETS)
    if explicit is not None:
        return explicit

    is_dark = not _check_light_env()
    name = "dark-default" if is_dark else "light-default"
    return dict(PT_THEME_PRESETS[name]), f"auto ({'dark' if is_dark else 'light'})"


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Typing "!something" jumps the queue; a bare "!" promotes the whole queue.
FORCE_PREFIX = "!"


class PromptToolkitConsoleManager(AbstractManager):
    """
    Full-screen terminal UI where the bottom three lines belong to the user:

        Just Finish It  ·  session: demo  ·  planner › imp › testing › reviewer
        ────────────────────────────────────────────────────────────────────────
        <AI space: streaming output, scrollable>
        ────────────────────────────────────────────────────────────────────────
        Queue_Size: (2)   ⚡1   ⠦ imp · streaming   Enter queue · !force · …
        > _

    The agent pipeline runs on a worker thread (see :meth:`run`) so the input
    line is always live. Typing never blocks, and a submitted line lands in one
    of three places:

    * the pipeline is asking a question  -> it answers that question;
    * the line starts with ``!``         -> forced, injected into the AI's very
                                            next turn (:meth:`drain_forced_input`);
    * otherwise                          -> queued, and the runner replays the
                                            whole queue as a fresh iteration once
                                            the review phase lands
                                            (:meth:`drain_queued_input`).

    A bare ``!`` promotes everything already queued into the current turn.
    """

    def __init__(self, title: str = "Just Finish It"):
        super().__init__()
        self.title = title

        # An explicit THEME env preset wins; otherwise auto-detect the terminal.
        overrides, self.theme_source = resolve_pt_theme()
        self._style = Style.from_dict({**UI_STYLE_BASE, **overrides})

        # --- output state (guarded, read during render) -------------------
        self._lock = threading.RLock()
        self._blocks: List[List[str]] = []  # [style, text] pairs, merged greedily
        self._snapshot: Optional[tuple] = None  # (fragments, line_count) from the last render pass
        self._log_file = None  # open file handle from start_session_log(), or None
        self._follow = True
        self._anchor = 0

        # --- status state --------------------------------------------------
        self._session = ""
        self._phases: List[str] = []
        self._phase = ""
        self._done_phases: List[str] = []
        self._state = "starting"
        self._awaiting: Optional[str] = None
        self._iteration = 1
        self._plan = None  # (ticked, total) checkbox progress
        self._task: Optional[str] = None  # current plan item's text (imp/testing only)
        self._tokens = None  # (estimated tokens used, context window)
        self._tokens_read = 0  # cumulative prompt tokens sent this run
        self._tokens_written = 0  # cumulative completion tokens generated this run

        # --- input / control ----------------------------------------------
        self._answers: "queue.Queue[str]" = queue.Queue()   # replies to a question
        self._forced: "queue.Queue[str]" = queue.Queue()    # jump the queue, run now
        self._queued: "queue.Queue[str]" = queue.Queue()    # run after the review phase
        # Mirrors self._queued's contents under self._lock, purely so
        # set_queue_store's on_change callback can report "everything
        # currently queued" without peeking queue.Queue's internals from
        # across threads (mutated from the UI thread in _on_accept, read from
        # the worker thread in drain_queued_input/wait_for_queued_input).
        self._queued_snapshot: List[str] = []
        self._queue_on_change: Optional[Callable[[List[str]], None]] = None
        self._stop = threading.Event()
        self._app: Optional[Application] = None
        self._worker_error: Optional[BaseException] = None

        self._build_ui()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self._input_buffer = Buffer(multiline=True, accept_handler=self._on_accept)

        self._out_control = FormattedTextControl(
            self._output_fragments,
            get_cursor_position=self._cursor_position,
        )
        self._out_window = Window(
            content=self._out_control,
            wrap_lines=True,
            always_hide_cursor=True,
            style="class:out.assistant",
        )

        self._input_window = Window(
            content=BufferControl(buffer=self._input_buffer),
            height=D(min=1, max=8),
            wrap_lines=True,
            get_line_prefix=self._input_line_prefix,
        )

        root = HSplit([
            Window(FormattedTextControl(self._header_fragments), height=1),
            Window(FormattedTextControl(self._task_line_fragments), height=1),
            self._out_window,
            # --- the three reserved lines -------------------------------
            Window(char="─", height=1, style="class:rule"),
            Window(FormattedTextControl(self._status_fragments), height=1),
            self._input_window,
        ])

        self._layout = Layout(root, focused_element=self._input_window)
        self._kb = self._key_bindings()

    @staticmethod
    def _input_line_prefix(line_number: int, wrap_count: int):
        if line_number == 0 and wrap_count == 0:
            return [("class:prompt", "> ")]
        return [("class:prompt", "· ")]

    def _key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def _submit(event):
            # Bracketed paste (on by default) delivers a pasted block as one
            # event, so its newlines never reach this handler. Terminals
            # without it deliver the block as a burst of keys instead: if more
            # keys are already buffered, this newline came from the paste, not
            # from the user reaching for send.
            pending = [
                key for key in event.app.key_processor.input_queue
                if key.key != Keys.CPRResponse
            ]
            if pending:
                event.current_buffer.insert_text("\n")
            else:
                event.current_buffer.validate_and_handle()

        add_shift_enter_newline(kb)

        @kb.add("pageup")
        def _page_up(event):
            self._scroll(-self._page_size())

        @kb.add("pagedown")
        def _page_down(event):
            self._scroll(self._page_size())

        @kb.add("c-up")
        def _line_up(event):
            self._scroll(-1)

        @kb.add("c-down")
        def _line_down(event):
            self._scroll(1)

        @kb.add("c-end")
        def _to_bottom(event):
            with self._lock:
                self._follow = True

        @kb.add("c-c")
        @kb.add("c-d", filter=Condition(lambda: not self._input_buffer.text))
        def _interrupt(event):
            if self._stop.is_set():
                event.app.exit()  # second press: leave now
                return
            self.request_stop()

        return kb

    def _logical_line_count(self) -> int:
        """Number of *renderable* logical lines for the fragments prompt_toolkit is
        currently drawing, kept consistent with :meth:`_output_fragments`.

        prompt_toolkit renders exactly ``split_lines(fragments)`` logical lines for
        the output window (see ``controls.py::create_content``), so that count is
        the single source of truth: every cursor/scroll offset must stay below it.
        The value is recorded together with the fragment list in :data:`_snapshot`
        by ``_output_fragments``, which prompt_toolkit always calls *before* it
        reads the cursor position within one render pass — so the count here
        always matches the exact fragments frozen for that draw. That removes the
        race where a live-updated line count (from the worker thread streaming
        more text mid-redraw) let an offset point past the end of the content,
        crashing the redraw with "Exception list index out of range".

        ``split_lines`` always yields at least one line, so an empty AI space
        maps to exactly 1 logical line.
        """
        with self._lock:
            if self._snapshot is None:
                return 1
            return self._snapshot[1]

    def _page_size(self) -> int:
        info = self._out_window.render_info
        return max(1, (info.window_height - 1) if info else 10)

    def _scroll(self, delta: int) -> None:
        with self._lock:
            last = max(0, self._logical_line_count() - 1)
            current = last if self._follow else self._anchor
            target = min(last, max(0, current + delta))
            self._anchor = target
            self._follow = target >= last

    def _cursor_position(self) -> Point:
        with self._lock:
            last = max(0, self._logical_line_count() - 1)
            y = last if self._follow else min(max(0, self._anchor), last)
        return Point(x=0, y=y)

    # ------------------------------------------------------- render getters

    def _output_fragments(self):
        # Build the fragment list AND its line count together, atomically under
        # the lock, and cache them as one snapshot. prompt_toolkit calls this
        # first in a render pass (freezing the fragments) and only afterwards
        # reads the cursor position; recording the matching line count here
        # means _cursor_position/_scroll always clamp against the exact content
        # being drawn, so no offset can ever address a missing line — even while
        # another thread is still appending output.
        with self._lock:
            fragments = [(style, text) for style, text in self._blocks]
            # split_lines() yields one line per '\n' plus always a final
            # (possibly empty) line; its length is the true number of
            # renderable logical lines for these fragments.
            self._snapshot = (fragments, len(list(split_lines(fragments))))
            return fragments

    @staticmethod
    def _fmt_tokens(n: int) -> str:
        """Compact token count: 987, 12.3k, or 3.1M (cumulative read/written
        counters cross into the millions on a long session, and 4-digit 'k'
        values like '3124.1k' are harder to read at a glance than '3.1M')."""
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1000:
            return f"{n / 1000:.1f}k"
        return str(n)

    def _header_fragments(self):
        with self._lock:
            session, phases = self._session, list(self._phases)
            phase, done = self._phase, set(self._done_phases)
            iteration, plan, tokens = self._iteration, self._plan, self._tokens
            tokens_read, tokens_written = self._tokens_read, self._tokens_written

        frags = [("class:header", f" {self.title}")]
        if tokens_read or tokens_written:
            frags += [
                ("class:header.dim", "  ·  "),
                ("class:header.tokens", f"↓{self._fmt_tokens(tokens_read)}"),
                ("class:header.dim", " "),
                ("class:header.tokens", f"↑{self._fmt_tokens(tokens_written)}"),
            ]
        if session:
            frags += [("class:header.dim", "  ·  session: "), ("class:header", session)]
        if iteration > 1:
            frags += [("class:header.dim", "  ·  "), ("class:header.loop", f"loop #{iteration}")]
        if phases:
            frags.append(("class:header.dim", "  ·  "))
            for i, name in enumerate(phases):
                if i:
                    frags.append(("class:header.dim", " › "))
                # Done wins over active: the phase we just ticked off is still
                # the current one until the next phase starts. Comparisons use
                # the raw phase key; only the printed label is renamed.
                label = phase_display_name(name)
                if name in done:
                    frags.append(("class:header.phase.done", f"✓ {label}"))
                elif name == phase:
                    frags.append(("class:header.phase.active", f"▸ {label}"))
                else:
                    frags.append(("class:header.phase", label))
        if plan and plan[1]:
            ticked, total = plan
            style = "class:header.phase.done" if ticked == total else "class:header.loop"
            frags += [("class:header.dim", "  ·  plan "), (style, f"{ticked}/{total}")]
        if tokens and tokens[1]:
            used, budget = tokens
            pct = min(100, int(used * 100 / budget))
            style = "class:header.tokens.warn" if pct >= 90 else "class:header.tokens"
            frags += [
                ("class:header.dim", "  ·  ctx "),
                (style, f"{self._fmt_tokens(used)}/{self._fmt_tokens(budget)} ({pct}%)"),
            ]
        # Theme source is persistent info: an explicit THEME from .env must be
        # visible so the user can confirm their setting took effect.
        if self.theme_source:
            frags += [
                ("class:header.dim", "  ·  "),
                ("class:header.dim", theme_label(self.theme_source)),
            ]
        return frags

    def _task_line_fragments(self):
        """The line right under the header: which phase is active and, for
        imp/testing (the only phases with a checkbox work queue), the exact
        item currently being worked. Falls back to a plain divider when idle
        (no phase set), matching the look this line had before it did
        anything — planner/reviewer show just the phase, since they have no
        per-item task concept.
        """
        with self._lock:
            phase, task = self._phase, self._task

        if not phase:
            return [("class:rule", "─" * self._width())]

        frags = [("class:tasktitle.phase", f" ▸ {phase_display_name(phase).upper()}")]
        if task:
            frags += [("class:tasktitle.dim", "  ·  "), ("class:tasktitle", task)]
        return frags

    def _status_fragments(self):
        with self._lock:
            phase, state, awaiting, follow = self._phase, self._state, self._awaiting, self._follow
        queued, forced = self._queued.qsize(), self._forced.qsize()

        frags = [
            ("class:status", " Queue_Size: "),
            ("class:status.queue", f"({queued})"),
        ]
        if forced:
            frags += [("class:status", "  "), ("class:status.forced", f"⚡{forced}")]
        frags.append(("class:status", "   "))

        if awaiting is not None:
            frags += [("class:status.wait", "⌨ waiting for your input")]
        else:
            tick = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
            label = f"{phase_display_name(phase)} · {state}" if phase else state
            frags += [("class:status.busy", f"{tick} {label}")]

        if not follow:
            frags += [("class:status", "   "), ("class:status.locked", "SCROLL LOCK · PgDn to resume")]

        if awaiting is not None:
            hint = "   Enter answers · Alt+Enter newline · PgUp/PgDn scroll · Ctrl+C stop"
        else:
            hint = ("   Enter queues for after review · !text runs now · ! runs the whole queue · "
                    "Alt+Enter newline · Ctrl+C stop")
        frags.append(("class:status", hint))
        return frags

    # ---------------------------------------------------------- write path

    def _invalidate(self) -> None:
        if self._app is not None:
            self._app.invalidate()

    def _write(self, style: str, text: str) -> None:
        """Appends text to the AI space, merging into the previous block."""
        if not text:
            return
        with self._lock:
            if self._blocks and self._blocks[-1][0] == style:
                self._blocks[-1][1] += text
            else:
                self._blocks.append([style, text])
            if self._log_file is not None:
                try:
                    self._log_file.write(text)
                    self._log_file.flush()
                except OSError:
                    self._log_file = None
        self._invalidate()

    def _line(self, style: str, text: str) -> None:
        with self._lock:
            at_line_start = not self._blocks or self._blocks[-1][1].endswith("\n")
        self._write(style, ("" if at_line_start else "\n") + text + "\n")

    def _width(self) -> int:
        try:
            return max(20, self._app.output.get_size().columns)  # type: ignore[union-attr]
        except Exception:
            return 80

    # --------------------------------------------------- AbstractManager API

    def display_user(self, text: str) -> None:
        self._line("class:out.user", f"🧑 you ▸ {text}")

    def display_assistant(self, text: str) -> None:
        self._line("class:out.assistant", f"🤖 {text}")

    def display_system(self, text: str) -> None:
        for line in str(text).splitlines() or [""]:
            self._line("class:out.system", f" ⚙  {line}")

    def display_error(self, text: str) -> None:
        self._line("class:out.error", f" ✗  {text}")

    def display_tool_call(self, name: str, args: Optional[Dict[str, Any]] = None) -> None:
        preview = ""
        if args:
            bits = []
            for key, value in args.items():
                flat = " ".join(str(value).split())
                # execute_command's command is worth seeing in full — with a
                # long-running or backgrounded command especially, knowing
                # exactly what's running matters more than staying short.
                # Other tools' args (write_file's content, replace_in_file's
                # old_string/new_string, ...) can be genuinely huge blobs, so
                # those still truncate to keep the console from flooding.
                if name == "execute_command" and key == "command":
                    bits.append(f"{key}={flat}")
                else:
                    bits.append(f"{key}={flat[:60]}{'…' if len(flat) > 60 else ''}")
            preview = "  (" + ", ".join(bits) + ")"
        self._line("class:out.tool", f" ⚙️ {name}{preview}")

    def display_tool_result(self, text: str) -> None:
        flat = str(text).strip()
        lines = flat.splitlines()
        head = lines[:8]
        for line in head:
            self._line("class:out.result", f"    │ {line[:self._width() - 8]}")
        if len(lines) > len(head):
            self._line("class:out.result", f"    └ … {len(lines) - len(head)} more line(s)")

    def display_rule(self, label: str = "") -> None:
        width = self._width() - 2
        if label:
            label = f" {label} "
            left = max(0, (width - len(label)) // 2)
            right = max(0, width - left - len(label))
            self._line("class:out.rule", " " + "─" * left + label + "─" * right)
        else:
            self._line("class:out.rule", " " + "─" * width)

    def get_user_input(
            self,
            prompt_label: str = "You",
            multiline: bool = True,
            queue_size_getter: Optional[Callable[[], int]] = None,
    ) -> str:
        """
        Blocks the worker thread until the user answers.

        While a question is on screen every submitted line answers it, so the
        ``!`` prefix and the queue stay out of the way.
        """
        if self._stop.is_set():
            return ""

        self._line("class:out.user", f"❯ {prompt_label}")
        with self._lock:
            self._awaiting = prompt_label
        self._invalidate()
        try:
            while not self._stop.is_set():
                try:
                    text = self._answers.get(timeout=0.2)
                except queue.Empty:
                    continue
                self.display_user(text)
                return text
            return ""
        finally:
            with self._lock:
                self._awaiting = None
            self._invalidate()

    def print_agent_response(self, agent_response: Any, prompt_tokens_estimate: int = 0) -> Dict[str, Any]:
        """Consumes the LLM stream into the AI space and returns the parsed turn.

        ``prompt_tokens_estimate`` is the caller's char-based estimate of what
        this request's messages (plus tool schemas) cost — used for the
        read/written header counters only when the server never reports real
        ``usage`` (most OpenAI-compatible servers omit it in streaming mode
        unless ``stream_options.include_usage`` was requested, which isn't
        universally supported, so we don't require it).
        """
        full_text = ""
        tool_calls_dict: Dict[int, Dict[str, Any]] = {}
        usage_seen = False

        self.set_status(state="streaming")
        self._line("class:out.assistant.tag", "🤖 Assistant")

        for chunk in agent_response:
            if self._stop.is_set():
                break

            usage = getattr(chunk, "usage", None)
            if usage is not None:
                usage_seen = True
                with self._lock:
                    self._tokens_read += usage.prompt_tokens or 0
                    self._tokens_written += usage.completion_tokens or 0

            # A usage-only trailer chunk (sent when the server supports
            # stream_options.include_usage) carries no choices.
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            if delta.content is not None:
                full_text += delta.content
                self._write("class:out.assistant", delta.content)

            if getattr(delta, "tool_calls", None):
                for tc in delta.tool_calls:
                    idx = tc.index

                    if idx not in tool_calls_dict:
                        tool_calls_dict[idx] = {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": getattr(tc.function, "name", ""),
                                "arguments": "",
                            },
                        }
                        if tc.function and tc.function.name:
                            self._line("class:out.tool", f" 🛠  preparing call: {tc.function.name}")

                    if tc.function and getattr(tc.function, "arguments", None):
                        tool_calls_dict[idx]["function"]["arguments"] += tc.function.arguments

        if not usage_seen:
            written_chars = len(full_text)
            for call in tool_calls_dict.values():
                function = call["function"]
                written_chars += len(function.get("name") or "") + len(function.get("arguments") or "")
            with self._lock:
                self._tokens_read += prompt_tokens_estimate
                self._tokens_written += (written_chars + 8) // 4
        self._invalidate()

        self._write("class:out.assistant", "" if full_text.endswith("\n") else "\n")
        self.set_status(state="thinking")

        return {
            "content": full_text.strip() if full_text else None,
            "tool_calls": list(tool_calls_dict.values()) if tool_calls_dict else None,
        }

    # ------------------------------------------------------- queue & status

    def _drain(self, q: "queue.Queue[str]") -> List[str]:
        items: List[str] = []
        while True:
            try:
                items.append(q.get_nowait())
            except queue.Empty:
                break
        if items:
            self._invalidate()
        return items

    def drain_forced_input(self) -> List[str]:
        """Lines the user pushed to the front; belong in the AI's next turn."""
        return self._drain(self._forced)

    def drain_queued_input(self) -> List[str]:
        """The default queue, replayed as a new iteration after the review phase."""
        items = self._drain(self._queued)
        if items:
            with self._lock:
                self._queued_snapshot = []
            self._notify_queue_change()
        return items

    def wait_for_queued_input(self, poll: float = 0.2) -> List[str]:
        """
        Blocks until the user queues something, or the run is stopped.

        Used instead of asking a question: the pipeline idles here with the
        input line live, so feeding it more work stays entirely optional.
        """
        while not self._stop.is_set():
            items = self._drain(self._queued)
            if items:
                with self._lock:
                    self._queued_snapshot = []
                self._notify_queue_change()
                return items
            time.sleep(poll)
        return []

    def _notify_queue_change(self) -> None:
        """Reports self._queued_snapshot to whoever is persisting the queue
        (see set_queue_store) — called on every add, drain, or promotion.
        Best-effort: persistence must never break the live queue."""
        callback = self._queue_on_change
        if callback is None:
            return
        with self._lock:
            snapshot = list(self._queued_snapshot)
        try:
            callback(snapshot)
        except Exception:
            pass

    def set_queue_store(self, initial_items: List[str], on_change: Callable[[List[str]], None]) -> None:
        with self._lock:
            self._queue_on_change = on_change
            for item in initial_items:
                self._queued.put(item)
            self._queued_snapshot = list(initial_items)
        if initial_items:
            self._line(
                "class:out.system",
                f" 📋 Restored {len(initial_items)} queued request(s) from before this run.",
            )
        self._invalidate()

    def pending_input_count(self) -> int:
        return self._queued.qsize()

    def forced_input_count(self) -> int:
        return self._forced.qsize()

    def set_status(self, session: Optional[str] = None, phase: Optional[str] = None,
                   state: Optional[str] = None, phases: Optional[List[str]] = None,
                   plan: Optional[tuple] = None, tokens: Optional[tuple] = None,
                   task: Optional[str] = None) -> None:
        with self._lock:
            if plan is not None:
                self._plan = plan
            if tokens is not None:
                self._tokens = tokens
            if task is not None:
                self._task = task
            if session is not None:
                self._session = session
            if phase is not None:
                self._phase = phase
            if state is not None:
                self._state = state
            if phases is not None:
                self._phases = list(phases)
        self._invalidate()

    def mark_phase_done(self, phase: str) -> None:
        with self._lock:
            if phase not in self._done_phases:
                self._done_phases.append(phase)
        self._invalidate()

    def start_iteration(self, number: int, phases: Optional[List[str]] = None) -> None:
        """
        Rewinds the phase breadcrumb for a fresh pass through the pipeline, so
        the header tracks the current loop rather than accumulating ticks.
        """
        with self._lock:
            self._iteration = number
            self._done_phases = []
            self._phase = ""
            self._task = None
            if phases is not None:
                self._phases = list(phases)
        self._invalidate()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()
        self.display_system("⚠️  Stop requested — finishing the current step, state is saved.")
        self.set_status(state="stopping")

    # ------------------------------------------------------------- lifecycle

    def _on_accept(self, buff: Buffer) -> bool:
        text = buff.text.strip()
        if not text:
            return False

        with self._lock:
            self._follow = True
            awaiting = self._awaiting is not None

        forced = text.startswith(FORCE_PREFIX)
        if forced:
            text = text[len(FORCE_PREFIX):].strip()

        if awaiting:
            # A question is on screen, so this line is simply the answer.
            self._answers.put(text or FORCE_PREFIX)
        elif forced and not text:
            promoted = self._move_queue(self._queued, self._forced)
            if promoted:
                with self._lock:
                    self._queued_snapshot = []
                self._notify_queue_change()
                self._line("class:out.tool", f" ⚡ forcing {promoted} queued request(s) into the current turn")
            else:
                self._line("class:out.system", " (nothing queued to force)")
        elif forced:
            self._forced.put(text)
            self._line("class:out.tool", f" ⚡ forced ▸ {self._preview(text)}")
        else:
            self._queued.put(text)
            with self._lock:
                self._queued_snapshot.append(text)
            self._notify_queue_change()
            self._line("class:out.system", f" ⏳ queued #{self._queued.qsize()} ▸ {self._preview(text)}")

        return False  # clear the input line

    @staticmethod
    def _preview(text: str, limit: int = 70) -> str:
        """One-line summary of a possibly pasted, multi-line request."""
        lines = text.splitlines() or [""]
        head = lines[0][:limit] + ("…" if len(lines[0]) > limit else "")
        if len(lines) > 1:
            head += f"  (+{len(lines) - 1} more line{'s' if len(lines) > 2 else ''})"
        return head

    @staticmethod
    def _move_queue(src: "queue.Queue[str]", dst: "queue.Queue[str]") -> int:
        moved = 0
        while True:
            try:
                dst.put(src.get_nowait())
            except queue.Empty:
                return moved
            moved += 1

    @staticmethod
    def _close_app(app: Application) -> None:
        """Asks the UI to exit, tolerating a worker that finished before it started."""
        deadline = time.monotonic() + 5
        while not app.is_running and time.monotonic() < deadline:
            time.sleep(0.02)
        try:
            app.loop.call_soon_threadsafe(lambda: app.exit() if app.is_running else None)
        except Exception:
            pass

    def run(self, worker: Callable[[], Any]) -> Any:
        """
        Takes over the terminal and runs ``worker`` on a background thread.
        Returns whatever ``worker`` returned; re-raises anything it raised.
        """
        result: List[Any] = [None]

        self._app = Application(
            layout=self._layout,
            key_bindings=self._kb,
            style=self._style,
            full_screen=True,
            mouse_support=True,
            refresh_interval=0.2,
        )
        app = self._app

        def target() -> None:
            try:
                result[0] = worker()
            except BaseException as exc:  # surfaced after the UI closes
                self._worker_error = exc
            finally:
                self._stop.set()
                self._close_app(app)

        thread = threading.Thread(target=target, name="jfi-pipeline", daemon=True)
        thread.start()
        try:
            app.run()
        finally:
            self._stop.set()
            thread.join(timeout=5)
            self._app = None

        if self._worker_error is not None:
            raise self._worker_error
        return result[0]

    def dump_transcript(self) -> None:
        """Reprints the AI space to the normal terminal after the UI closes."""
        with self._lock:
            text = "".join(block[1] for block in self._blocks)
        if text.strip():
            print(text.rstrip())
        self.close_session_log()

    # --------------------------------------------------------------- logging

    def start_session_log(self, path) -> None:
        """
        Mirrors everything shown in the AI output pane to a plain-text file at
        ``path``, live — the session id (and so this path) is only known
        partway through startup, once the user has answered the session-name
        prompt, so whatever was already shown before that (the startup
        banner) is flushed out first, and every :meth:`_write` after this
        appends to the file as it happens. Opened in append mode so resuming
        a session keeps its prior runs' logs rather than overwriting them;
        each open is marked with a timestamped banner so the boundary between
        runs is visible in the file.

        Live-appending (rather than writing once at exit) means the log
        survives a crash or a forced-stop that never reaches
        :meth:`dump_transcript`, and can be ``tail -f``'d while a run is in
        flight.
        """
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(path, "a", encoding="utf-8")
        except OSError as exc:
            self.display_error(f"Could not open run log at {path}: {exc}")
            return

        with self._lock:
            timestamp = datetime.now().isoformat(timespec="seconds")
            log_file.write(f"\n===== JFI run started {timestamp} =====\n")
            # Cover the part of the run that happened before the session id
            # (and so this path) was known.
            already_shown = "".join(block[1] for block in self._blocks)
            if already_shown:
                log_file.write(already_shown)
            log_file.flush()
            self._log_file = log_file

    def close_session_log(self) -> None:
        with self._lock:
            log_file, self._log_file = self._log_file, None
        if log_file is not None:
            try:
                log_file.close()
            except OSError:
                pass
