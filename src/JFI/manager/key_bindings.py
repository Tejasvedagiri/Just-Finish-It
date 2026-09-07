"""Shared prompt_toolkit key-binding setup for multiline input.

Both console managers need the same thing: Enter submits, Shift+Enter (or the
universally-reliable Alt+Enter fallback) inserts a newline instead. This used
to be implemented twice — PromptToolkitConsoleManager taught the terminal
parser both Shift+Enter escape-code encodings (CSI-u and xterm's
modifyOtherKeys=2) plus Ctrl+Enter, while RichConsoleManager only hand-bound
the CSI-u sequence as a literal key tuple, so it silently missed the
modifyOtherKeys=2 encoding and Ctrl+Enter entirely. Centralizing it here means
one fix covers both managers, and both interpret "Shift+Enter" identically.
"""

from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

_taught = False


def teach_terminal_shift_enter() -> None:
    """
    Teaches the vt100 parser the Shift+Enter / Ctrl+Enter escape codes.

    A bare terminal throws the modifier away and sends plain CR for
    Shift+Enter, so there is nothing to bind. Terminals that *do* report it use
    one of two encodings, and neither resolves to a distinct key by default
    (prompt_toolkit even folds xterm's variant back into a plain Enter). Both
    are remapped to Escape+Enter here, so they land on the same "insert a
    newline" binding as Alt+Enter instead of submitting.

    Idempotent and safe to call from every manager that builds multiline key
    bindings: ``ANSI_SEQUENCES`` is a process-wide table, so only the first
    call does anything.
    """
    global _taught
    if _taught:
        return
    _taught = True

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


def add_shift_enter_newline(kb: KeyBindings) -> KeyBindings:
    """
    Binds Shift+Enter/Alt+Enter (via the terminal teaching above) and Ctrl+J
    to insert a newline into ``kb``, in place, and returns it. Does not bind
    plain Enter — callers wire up their own submit behavior for that, since
    PromptToolkitConsoleManager and RichConsoleManager each need slightly
    different submit logic (paste detection vs. plain ``validate_and_handle``).
    """
    teach_terminal_shift_enter()

    @kb.add("escape", "enter")  # Alt+Enter, and Shift+Enter once taught above
    @kb.add("c-j")  # Ctrl+J, plus terminals that map Shift+Enter to LF
    def _newline(event):
        event.current_buffer.insert_text("\n")

    return kb
