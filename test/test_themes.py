"""Tests for the THEME env presets in manager/pt_console_manager.py.

Covers every preset in ``PT_THEME_PRESETS``: an explicit ``THEME`` value must
return exactly that palette, while empty or unknown values fall back to
auto-detection (never crash).
"""

import pytest

# Import at module level so the parametrize decorator can reference the table.
from JFI.manager.pt_console_manager import PT_THEME_PRESETS


def test_six_presets_exist():
    """The documented set is 3 dark + 3 light presets."""
    names = set(PT_THEME_PRESETS)
    assert {
        "dark-default", "dark-ocean", "dark-mono",
        "light-default", "light-sunrise", "light-paper",
    } <= names


@pytest.mark.parametrize("preset", sorted(PT_THEME_PRESETS))
def test_pt_explicit_preset_honored(monkeypatch, preset):
    """Every PT preset selectable via THEME returns its exact overrides."""
    import JFI.manager.pt_console_manager as ptm

    monkeypatch.setenv("THEME", preset)
    overrides, source = ptm.resolve_pt_theme()
    assert overrides == dict(ptm.PT_THEME_PRESETS[preset])
    assert source == f"env:THEME={preset}"


@pytest.mark.parametrize("preset, raw", [
    ("dark-default", "Dark_Default"),
    ("dark-default", "DARK_DEFAULT"),
    ("dark-ocean", "  DARK_OCEAN  "),
    ("dark-mono", "dark_mono"),
    ("light-default", "Light-Default"),
    ("light-sunrise", "LIGHT_SUNRISE"),
    ("light-paper", "Light_Paper"),
])
def test_pt_explicit_preset_normalizes_case_and_underscores(monkeypatch, preset, raw):
    """A THEME value with mixed case and/or underscores must resolve to the same
    normalized palette as its canonical name — never fall back or crash."""
    import JFI.manager.pt_console_manager as ptm

    monkeypatch.setenv("THEME", raw)
    overrides, source = ptm.resolve_pt_theme()
    assert overrides == dict(ptm.PT_THEME_PRESETS[preset])
    assert source == f"env:THEME={preset}"


@pytest.mark.parametrize("raw", ["", "   ", "auto"])
def test_pt_auto_variants_detect_terminal(monkeypatch, raw):
    """Empty/whitespace/auto must skip the env branch and detect via COLORFGBG."""
    import JFI.manager.pt_console_manager as ptm

    monkeypatch.setenv("THEME", raw)
    # Light terminal -> light-default fallback.
    monkeypatch.setenv("COLORFGBG", "0;15")
    overrides, source = ptm.resolve_pt_theme()
    assert overrides == dict(ptm.PT_THEME_PRESETS["light-default"])
    assert source == "auto (light)"

    # Dark terminal -> dark-default fallback.
    monkeypatch.delenv("COLORFGBG", raising=False)
    overrides, source = ptm.resolve_pt_theme()
    assert overrides == dict(ptm.PT_THEME_PRESETS["dark-default"])
    assert source == "auto (dark)"


def test_pt_explicit_preset_wins_over_light_terminal(monkeypatch):
    """Explicit preset always wins over auto-detection."""
    import JFI.manager.pt_console_manager as ptm

    monkeypatch.setenv("THEME", "dark-ocean")
    monkeypatch.setenv("COLORFGBG", "0;15")  # light terminal

    overrides, source = ptm.resolve_pt_theme()
    assert overrides == dict(ptm.PT_THEME_PRESETS["dark-ocean"])
    assert source == "env:THEME=dark-ocean"


def test_pt_unknown_preset_falls_back_without_raising(monkeypatch):
    """Unknown THEME must auto-detect, not crash."""
    import JFI.manager.pt_console_manager as ptm

    monkeypatch.setenv("THEME", "pastel-dreams")
    monkeypatch.delenv("COLORFGBG", raising=False)  # dark terminal

    overrides, source = ptm.resolve_pt_theme()
    assert overrides == dict(ptm.PT_THEME_PRESETS["dark-default"])
    assert source == "auto (dark)"


def test_pt_unknown_preset_prints_system_hint(monkeypatch, capsys):
    """The fallback hint names the value and lists every valid preset."""
    import JFI.manager.pt_console_manager as ptm

    monkeypatch.setenv("THEME", "pastel-dreams")
    monkeypatch.delenv("COLORFGBG", raising=False)

    ptm.resolve_pt_theme()
    out = capsys.readouterr().out
    assert "[system] Unknown THEME 'pastel-dreams'" in out
    for preset in sorted(ptm.PT_THEME_PRESETS):
        assert preset in out


def test_pt_returns_copy_not_shared_dict(monkeypatch):
    """Callers mutating the returned overrides must not corrupt the preset table."""
    import JFI.manager.pt_console_manager as ptm

    monkeypatch.setenv("THEME", "dark-mono")
    before = {k: v for k, v in ptm.PT_THEME_PRESETS["dark-mono"].items()}
    overrides, _ = ptm.resolve_pt_theme()
    assert overrides is not ptm.PT_THEME_PRESETS["dark-mono"]
    overrides.update({"user": "#ff0000"})
    assert dict(ptm.PT_THEME_PRESETS["dark-mono"]) == before


def test_main_loads_dotenv_before_console(monkeypatch):
    """`load_dotenv` must run BEFORE the console is built in `runner.main()`,
    otherwise a THEME set in .env would be resolved from an empty environment.
    A spy records the call order so any future reorder fails this test."""
    import JFI.runner as runner
    import JFI.manager.pt_console_manager as ptm

    order: list[str] = []
    monkeypatch.setattr(
        runner, "load_dotenv", lambda *a, **k: order.append("load_dotenv") or True
    )
    monkeypatch.setattr(runner, "find_dotenv", lambda *a, **k: ".env")

    real_init = ptm.PromptToolkitConsoleManager.__init__

    def spy_init(self, *args, **kwargs):
        order.append("console_init")
        # main() calls these on the console; stub them since we skipped the real UI.
        self.run = lambda fn: None
        self.dump_transcript = lambda: None

    # No need to build a real terminal UI in this test.
    monkeypatch.setattr(ptm.PromptToolkitConsoleManager, "__init__", spy_init)

    monkeypatch.setattr(
        runner.ColibriLLMStream, "close", lambda self: None
    )
    fake_llm = type("FakeLLM", (), {"close": staticmethod(lambda *a, **k: None)})()
    monkeypatch.setattr(runner, "ColibriLLMStream", lambda: fake_llm)

    def no_pipeline(console, llm):  # noqa: ANN001 - signature matches run_pipeline
        pass

    monkeypatch.setattr(runner, "run_pipeline", no_pipeline)
    runner.main()

    assert order == ["load_dotenv", "console_init"]
