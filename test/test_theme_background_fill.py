"""Regression test for a real bug: a themed background that only *looked*
right in the style dict.

PT_THEME_PRESETS carries a "" rule (see pt_console_manager.py) so the
terminal's own background gets painted, not just message text — but that
rule only reaches the screen for cells prompt_toolkit actually decides to
repaint. `_input_window` had no explicit `style=` and a flexible height
(allocated up to 8 rows but usually showing 1 line of typed text): its
unused padding rows never got painted, because `Window._apply_style()`
fills padding via `Screen.fill_area()`, which no-ops entirely when the
computed style string is empty (`if not style.strip(): return` in
layout/screen.py). Those rows then showed the terminal's own raw background
instead of the theme's — invisible to any test that only inspects
PT_THEME_PRESETS values, since the dict itself was already completely
correct; the gap only exists in how prompt_toolkit actually paints it.

The only reliable way to catch a gap like this is to run the real app end
to end and look at what actually lands on a real terminal: this spawns the
real `python -m JFI.runner` process on a real pty (same as a user's
terminal), captures its raw output, and replays it into an actual VT100
emulator (pyte) to check every cell's background. An in-process
Application built around a bare `io.StringIO` output was tried first and
did NOT reproduce real terminal behavior faithfully (it under-reported
fills prompt_toolkit does perform against a real fd) — so this test
deliberately pays the cost of a real subprocess+pty rather than trust a
synthetic harness that already proved misleading once.
"""

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pyte
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROWS, COLS = 30, 100


def _capture_real_app_frame(theme: str, timeout: float = 3.0) -> pyte.Screen:
    """Runs `python -m JFI.runner` on a real pty sized ROWSxCOLS under
    THEME=`theme`, captures whatever it writes for `timeout` seconds (it
    blocks on the session-name prompt, which is exactly the steady state we
    want to inspect), then kills it and replays the bytes into pyte."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["THEME"] = theme
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["OPENAI_URL"] = "http://127.0.0.1:1/v1"
    env["OPENAI_API_KEY"] = "x"
    env["MODEL"] = "x"

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    proc = subprocess.Popen(
        [sys.executable, "-m", "JFI.runner"],
        stdin=slave, stdout=slave, stderr=slave,
        env=env, cwd=str(REPO_ROOT), preexec_fn=os.setsid,
    )
    os.close(slave)

    data = b""
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.2)
            if master in r:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                data += chunk
    finally:
        _kill(proc)
        os.close(master)

    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    stream.feed(data.decode("utf-8", errors="replace"))
    return screen


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _every_cell_bg(screen: pyte.Screen) -> set:
    return {screen.buffer[y][x].bg for y in range(ROWS) for x in range(COLS)}


def test_dark_default_leaves_terminal_background_untouched():
    """The one preset that must NOT paint a background — every cell should
    stay at pyte's 'default', proving dark-default really inherits the
    terminal instead of also picking up a stray fill."""
    screen = _capture_real_app_frame("dark-default")
    assert _every_cell_bg(screen) == {"default"}


@pytest.mark.parametrize("theme, expected_bg", [
    ("dark-ocean", "141b26"),
    ("catppuccin-mocha", "1e1e2e"),
    ("light-paper", "faf7ef"),
])
def test_preset_fills_the_whole_viewport_including_input_padding(theme, expected_bg):
    """Every allocated row — including the input window's unused padding
    rows below the typed line, the actual bug — must show this exact
    background. This is what would have caught the real bug before it
    shipped; checking PT_THEME_PRESETS values alone could not."""
    screen = _capture_real_app_frame(theme)
    bgs = _every_cell_bg(screen)
    assert bgs == {expected_bg}, f"{theme}: found {bgs - {expected_bg}} instead of {expected_bg!r}"
