"""Tests for PromptToolkitConsoleManager.clear_console().

The method must erase a real terminal (TTY stdout) with ANSI escapes and be
best-effort everywhere else: non-TTY streams get nothing, and closed / odd
streams never raise — it runs on the exit path after Ctrl+C.
"""

import io


class _FakeStdout(io.StringIO):
    """A controllable stand-in for sys.stdout."""

    def __init__(self, is_tty: bool = True, fail_on_write: Exception | None = None):
        super().__init__()
        self._is_tty = is_tty
        self.flushed = 0
        self.fail_on_write = fail_on_write

    def isatty(self) -> bool:  # type: ignore[override]
        return self._is_tty

    def write(self, s):  # type: ignore[override]
        if self.fail_on_write is not None:
            raise self.fail_on_write
        return super().write(s)

    def flush(self):  # type: ignore[override]
        self.flushed += 1


def _make_manager():
    from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

    # Skip the real terminal UI; only clear_console is under test.
    return object.__new__(PromptToolkitConsoleManager)


def test_clear_console_writes_ansi_and_flushes_on_tty(monkeypatch):
    """A TTY stdout receives home+erase-screen(+scrollback) and a flush."""
    import JFI.manager.pt_console_manager as ptm

    buf = _FakeStdout(is_tty=True)
    monkeypatch.setattr(ptm.sys, "stdout", buf)

    _make_manager().clear_console()

    assert "\x1b[H" in buf.getvalue()   # home cursor
    assert "\x1b[2J" in buf.getvalue()  # erase screen
    assert buf.flushed >= 1


def test_clear_console_writes_nothing_on_non_tty(monkeypatch):
    """Piped/captured stdout (pytest, CI) must stay untouched."""
    import JFI.manager.pt_console_manager as ptm

    buf = _FakeStdout(is_tty=False)
    monkeypatch.setattr(ptm.sys, "stdout", buf)

    _make_manager().clear_console()

    assert buf.getvalue() == ""
    assert buf.flushed == 0


def test_clear_console_survives_closed_stdout(monkeypatch):
    """A stream that raises on write/flush must not propagate the error."""
    import JFI.manager.pt_console_manager as ptm

    closed = _FakeStdout(is_tty=True, fail_on_write=OSError("closed"))
    monkeypatch.setattr(ptm.sys, "stdout", closed)

    # Must not raise.
    _make_manager().clear_console()


def test_clear_console_survives_none_stdout(monkeypatch):
    """sys.stdout == None (e.g. after a redirect to devnull) must not raise."""
    import JFI.manager.pt_console_manager as ptm

    monkeypatch.setattr(ptm.sys, "stdout", None)

    _make_manager().clear_console()


def test_clear_console_is_idempotent_and_safe_to_call_twice(monkeypatch):
    """Calling twice (e.g. double exit path) must still write valid escapes."""
    import JFI.manager.pt_console_manager as ptm

    buf = _FakeStdout(is_tty=True)
    monkeypatch.setattr(ptm.sys, "stdout", buf)

    manager = _make_manager()
    manager.clear_console()
    manager.clear_console()

    assert "\x1b[2J" in buf.getvalue()
    assert buf.flushed >= 2


def test_main_clears_console_after_dump_transcript(monkeypatch):
    """runner.main()'s exit path must call clear_console() AFTER dump_transcript(),
    so the replayed transcript itself gets wiped on Ctrl+C / normal exit."""
    import JFI.runner as runner
    import JFI.manager.pt_console_manager as ptm

    order: list[str] = []
    monkeypatch.setattr(runner, "load_dotenv", lambda *a, **k: True)
    monkeypatch.setattr(runner, "find_dotenv", lambda *a, **k: ".env")

    real_init = ptm.PromptToolkitConsoleManager.__init__

    def spy_init(self, *args, **kwargs):
        order.append("console_init")
        self.run = lambda fn: None
        self.dump_transcript = lambda: order.append("dump_transcript")
        self.clear_console = lambda: order.append("clear_console")

    monkeypatch.setattr(ptm.PromptToolkitConsoleManager, "__init__", spy_init)

    # Patch the real class first (as test_themes does), then swap its factory.
    monkeypatch.setattr(runner.OpenAICompatableStream, "close", lambda self: None)
    fake_llm = type("FakeLLM", (), {"close": staticmethod(lambda *a, **k: None)})()
    monkeypatch.setattr(runner, "OpenAICompatableStream", lambda *a, **k: fake_llm)

    def no_pipeline(console, llm):  # noqa: ANN001 - signature matches run_pipeline
        pass

    monkeypatch.setattr(runner, "run_pipeline", no_pipeline)
    runner.main()

    assert order == ["console_init", "dump_transcript", "clear_console"]
