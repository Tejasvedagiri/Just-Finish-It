"""Regression tests: rich ``Console.print(...)`` interleaved with prompt_toolkit input.

Reproduces the historical crash where streamed assistant output (printed via
``Console.print(..., end="")``, leaving a half-finished line) was followed by
``RichConsoleManager.get_user_input()``; prompt_toolkit's redraw then raised an
exception out of its event loop ("Unhandled exception in event loop").

The managers run inside real child processes whose stdin is a *pseudo-tty* (via
``pty.openpty`` + ``subprocess.Popen``, no monkeypatching), so the full
rich/prompt_toolkit stack executes for real with genuine terminal semantics:
Enter submits, an embedded newline stays in the buffer. Each child prints one
JSON line to its (piped) stdout: ``{"ok": true, "result": ...}`` on success or
``{"ok": false, "error": ...}`` if an exception escaped the input call.

Assertions: no exception escapes AND the entered text is returned intact.
"""

import json
import os
import pty
import subprocess
import sys
import time


def _child_code(multiline: bool, pre_chunks) -> str:
    return f'''
import io, json
from JFI.manager.rich_console_manager import RichConsoleManager

mgr = RichConsoleManager.__new__(RichConsoleManager)  # skip env probing
from rich.console import Console as _C
# Render into a pipe-sized buffer so ANSI math is exercised like on a real tty.
mgr.console = _C(file=io.StringIO(), force_terminal=True, width=80)

chunks = {pre_chunks!r}
for c in chunks:
    mgr.console.print(c, end="")  # streamed assistant output; last chunk mid-line

try:
    result = mgr.get_user_input(prompt_label="You?", multiline={multiline!r})
except Exception as exc:
    print(json.dumps({{"ok": False, "error": repr(exc)}}), flush=True)
else:
    print(json.dumps({{"ok": True, "result": result}}), flush=True)
'''


def _run_child(multiline: bool, entered: str, pre_chunks, atomic: bool = False) -> dict:
    """Run one interleaved rich-print + get_user_input session in a pty-backed child.

    The child's stdin is a pseudo-tty (real terminal semantics for
    prompt_toolkit/rich); its stdout and stderr are plain pipes so we can
    capture the JSON result line cleanly, free of terminal echo bytes.

    ``atomic=True`` sends the whole input in one write (plus one trailing CR),
    which is how a pasted block arrives; with per-line pacing each CR would
    submit before the next line could be typed (Enter submits in prompt_toolkit).
    """
    code = _child_code(multiline, pre_chunks)

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    src_dir = os.path.join(project_root, "src")
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave_fd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=project_root,
    )
    os.close(slave_fd)

    def _send(text: str) -> None:
        data = text.encode("utf-8")
        while data:
            n = os.write(master_fd, data[:256])
            data = data[n:]
        time.sleep(0.3)  # let the child's event loop digest what we just wrote

    # Give the child time to boot and enter prompt_toolkit before typing.
    time.sleep(1.5)
    lines = entered.split("\n")
    if atomic:
        # Paste-style: one write delivers all embedded newlines as buffer content,
        # then a single CR submits (Enter).
        _send("".join(lines))
        _send("\r")
    else:
        for line in lines[:-1]:
            _send(line + "\r\n")  # per-line pacing; each CR would submit on its own
        _send(lines[-1] + "\r")  # CR = Enter on a pty; submits in prompt_toolkit

    out, err = proc.communicate(timeout=60)
    os.close(master_fd)
    text = out.decode("utf-8", "replace")
    last_line = None
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            last_line = stripped
            break
    assert last_line is not None, (
        f"no JSON result from child; stdout: {text!r}; stderr: {err.decode('utf-8', 'replace')[:500]!r}"
    )
    return json.loads(last_line)


def test_multiline_input_intact_after_streamed_output():
    entered = "run the planner turn\n"  # Enter submits in prompt_toolkit
    out = _run_child(True, entered, ["\U0001f916 Assistant: streaming reply part one ", "part two"])
    assert out["ok"], f"exception escaped event loop: {out['error']}"
    assert out["result"] == "run the planner turn"


def test_multiline_input_with_embedded_newline_intact():
    entered = "second line\nthird line\n"  # pasted block: embedded newline stays in buffer
    out = _run_child(True, entered, ["streamed tail"], atomic=True)
    assert out["ok"], f"exception escaped event loop: {out['error']}"
    assert "second line" in out["result"] and "third line" in out["result"]


def test_single_line_input_intact_after_streamed_output():
    entered = "hello world 42\n"
    out = _run_child(False, entered, ["\u270f\ufe0f Thinking", " about the plan", "..."])
    assert out["ok"], f"exception escaped event loop: {out['error']}"
    assert out["result"] == "hello world 42"
