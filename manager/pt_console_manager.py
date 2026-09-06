from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.styles import Style

from manager.abstract_manager import AbstractManager


def _teach_terminal_shift_enter() -> None:
    """
    Teaches the vt100 parser the Shift+Enter / Ctrl+Enter escape codes.

    A bare terminal throws the modifier away and sends plain CR for
    Shift+Enter, so there is nothing to bind. Terminals that *do* report it use
    one of two encodings, and neither resolves to a distinct key by default
    (prompt_toolkit even folds xterm's variant back into a plain Enter). Both
    are remapped to Escape+Enter here, so they land on the same "insert a
    newline" binding as Alt+Enter instead of submitting.
    """
    alt_enter = (Keys.Escape, Keys.ControlM)
    ANSI_SEQUENCES.update({
        # CSI-u (kitty, foot, WezTerm, Ghostty, recent xterm)
        "\x1b[13;2u": alt_enter,  # Shift+Enter
        "\x1b[13;5u": alt_enter,  # Ctrl+Enter
        "\x1b[13;6u": alt_enter,  # Ctrl+Shift+Enter
        # xterm modifyOtherKeys=2
        "\x1b[27;2;13~": alt_enter,  # Shift+Enter
        "\x1b[27;5;13~": alt_enter,  # Ctrl+Enter
    })


_teach_terminal_shift_enter()

# ANSI colour names are used on purpose: they inherit the user's terminal
# palette, so the UI stays readable on both light and dark backgrounds.
UI_STYLE = Style.from_dict({
    "header": "bold ansicyan",
    "header.dim": "ansibrightblack",
    "header.phase": "ansibrightblack",
    "header.phase.active": "bold ansimagenta",
    "header.phase.done": "ansigreen",
    "header.loop": "bold ansiyellow",
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
})

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

        # --- output state (guarded, read during render) -------------------
        self._lock = threading.RLock()
        self._blocks: List[List[str]] = []  # [style, text] pairs, merged greedily
        self._line_count = 1
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

        # --- input / control ----------------------------------------------
        self._answers: "queue.Queue[str]" = queue.Queue()   # replies to a question
        self._forced: "queue.Queue[str]" = queue.Queue()    # jump the queue, run now
        self._queued: "queue.Queue[str]" = queue.Queue()    # run after the review phase
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
            Window(char="─", height=1, style="class:rule"),
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

        @kb.add("escape", "enter")  # Alt+Enter, and Shift+Enter where reported
        @kb.add("c-j")  # Ctrl+J, plus terminals that map Shift+Enter to LF
        def _newline(event):
            event.current_buffer.insert_text("\n")

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

    def _page_size(self) -> int:
        info = self._out_window.render_info
        return max(1, (info.window_height - 1) if info else 10)

    def _scroll(self, delta: int) -> None:
        with self._lock:
            last = max(0, self._line_count - 1)
            current = last if self._follow else self._anchor
            target = min(last, max(0, current + delta))
            self._anchor = target
            self._follow = target >= last

    def _cursor_position(self) -> Point:
        with self._lock:
            last = max(0, self._line_count - 1)
            return Point(x=0, y=last if self._follow else min(self._anchor, last))

    # ------------------------------------------------------- render getters

    def _output_fragments(self):
        with self._lock:
            return [(style, text) for style, text in self._blocks]

    def _header_fragments(self):
        with self._lock:
            session, phases = self._session, list(self._phases)
            phase, done = self._phase, set(self._done_phases)
            iteration, plan = self._iteration, self._plan

        frags = [("class:header", f" {self.title}")]
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
                # the current one until the next phase starts.
                if name in done:
                    frags.append(("class:header.phase.done", f"✓ {name}"))
                elif name == phase:
                    frags.append(("class:header.phase.active", f"▸ {name}"))
                else:
                    frags.append(("class:header.phase", name))
        if plan and plan[1]:
            ticked, total = plan
            style = "class:header.phase.done" if ticked == total else "class:header.loop"
            frags += [("class:header.dim", "  ·  plan "), (style, f"{ticked}/{total}")]
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
            label = f"{phase} · {state}" if phase else state
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
            self._line_count += text.count("\n")
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

    def print_agent_response(self, agent_response: Any) -> Dict[str, Any]:
        """Consumes the LLM stream into the AI space and returns the parsed turn."""
        full_text = ""
        tool_calls_dict: Dict[int, Dict[str, Any]] = {}

        self.set_status(state="streaming")
        self._line("class:out.assistant.tag", "🤖 Assistant")

        for chunk in agent_response:
            if self._stop.is_set():
                break

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
        return self._drain(self._queued)

    def wait_for_queued_input(self, poll: float = 0.2) -> List[str]:
        """
        Blocks until the user queues something, or the run is stopped.

        Used instead of asking a question: the pipeline idles here with the
        input line live, so feeding it more work stays entirely optional.
        """
        while not self._stop.is_set():
            items = self._drain(self._queued)
            if items:
                return items
            time.sleep(poll)
        return []

    def pending_input_count(self) -> int:
        return self._queued.qsize()

    def forced_input_count(self) -> int:
        return self._forced.qsize()

    def set_status(self, session: Optional[str] = None, phase: Optional[str] = None,
                   state: Optional[str] = None, phases: Optional[List[str]] = None,
                   plan: Optional[tuple] = None) -> None:
        with self._lock:
            if plan is not None:
                self._plan = plan
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
                self._line("class:out.tool", f" ⚡ forcing {promoted} queued request(s) into the current turn")
            else:
                self._line("class:out.system", " (nothing queued to force)")
        elif forced:
            self._forced.put(text)
            self._line("class:out.tool", f" ⚡ forced ▸ {self._preview(text)}")
        else:
            self._queued.put(text)
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
            style=UI_STYLE,
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
