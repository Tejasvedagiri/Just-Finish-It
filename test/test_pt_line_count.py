"""Regression tests: line counting in ``PromptToolkitConsoleManager``.

The AI output window's cursor position must never point past the number of
lines prompt_toolkit actually rendered, or its redraw crashes with
``IndexError: list index out of range`` in ``_scroll_when_linewrapping``
(fragment_lines[i] with i == line_count). That used to happen because the
cursor position was computed from a live counter (``_line_count``) that a
background thread could bump *between* the moment prompt_toolkit fetched the
output fragments and the moment it asked for the cursor position — the two
calls race, since streaming assistant output writes from a worker thread while
the UI redraws on the main thread.

The fix (:meth:`PromptToolkitConsoleManager._output_fragments`) freezes the
fragment list and its line count together, atomically, into ``_snapshot``;
:meth:`_cursor_position` and :meth:`_scroll` clamp against that frozen count
instead of the live one. These tests exercise only the manager's public write
API plus the two render-getter methods prompt_toolkit calls — no Application
is started, so they run headless.
"""

from prompt_toolkit.data_structures import Point

from JFI.manager.pt_console_manager import PromptToolkitConsoleManager


def _new_manager() -> PromptToolkitConsoleManager:
    return PromptToolkitConsoleManager(title="line-count")


def test_logical_line_count_starts_at_one_and_counts_streamed_newlines():
    console = _new_manager()
    assert console._logical_line_count() == 1

    # Stream a response in chunks; the count must track embedded newlines.
    for chunk in ("hello\n", "world\n", "tail"):
        console._write("class:out.assistant", chunk)
    console._output_fragments()  # prompt_toolkit always fetches content first

    # Two "\n" characters were written -> two extra lines, starting from 1.
    assert console._logical_line_count() == 3


def test_display_helpers_add_one_line_each():
    console = _new_manager()
    console._output_fragments()
    baseline = console._logical_line_count()

    console.display_assistant("a line of output")
    console._output_fragments()
    assert console._logical_line_count() == baseline + 1

    console.display_system("status note\nsecond status line")
    console._output_fragments()
    # display_system emits one physical line per splitlines() entry.
    assert console._logical_line_count() == baseline + 3


def test_cursor_position_tracks_last_line():
    console = _new_manager()
    for i in range(5):
        console.display_assistant(f"output {i}")

    # prompt_toolkit's create_content() always fetches fragments before the
    # cursor position within one render pass.
    console._output_fragments()
    last = console._logical_line_count() - 1

    # Following the tail: cursor sits on the last rendered line (0-based).
    assert console._cursor_position() == Point(x=0, y=last)


def test_cursor_position_ignores_writes_after_the_content_snapshot():
    """The exact race that used to crash the redraw.

    prompt_toolkit freezes the fragment list (and, with it, the true line
    count) before it ever asks for the cursor position. If a worker thread
    appends more streamed text in between those two calls, the cursor must
    still land inside the *frozen* content, never past it.
    """
    console = _new_manager()
    console.display_assistant("line one")
    console._output_fragments()  # freezes the snapshot prompt_toolkit will draw
    frozen_last = console._logical_line_count() - 1

    # Simulate the worker thread racing ahead with more streamed output
    # after prompt_toolkit already grabbed its content for this redraw.
    console._write("class:out.assistant", "\nline two\nline three\n")

    # The cursor for the in-flight redraw must still match the frozen
    # snapshot, not the content that arrived afterwards.
    assert console._cursor_position() == Point(x=0, y=frozen_last)

    # The next redraw's snapshot catches up to the new content, and the
    # cursor then advances along with it — no lines are lost.
    console._output_fragments()
    assert console._logical_line_count() - 1 > frozen_last
    assert console._cursor_position() == Point(x=0, y=console._logical_line_count() - 1)
