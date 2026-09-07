"""Tests for the THEME env presets in manager/rich_console_manager.py.

Covers every preset in ``THEME_PRESETS``: an explicit ``THEME`` value must
return exactly that palette, while empty or unknown values fall back to
auto-detection (never crash).
"""

from unittest import mock

import pytest

# Import at module level so the parametrize decorator can reference the table.
from JFI.manager.pt_console_manager import PT_THEME_PRESETS


def test_adaptive_palette_matches_default_presets():
    """_get_adaptive_palette is the auto-detection fallback; it must hand out
    the default dark/light palettes."""
    from JFI.manager.rich_console_manager import THEME_PRESETS, _get_adaptive_palette

    assert _get_adaptive_palette(True) == dict(THEME_PRESETS["dark-default"])
    assert _get_adaptive_palette(False) == dict(THEME_PRESETS["light-default"])


@pytest.mark.parametrize("preset", [
    "dark-default", "dark-ocean", "dark-mono",
    "light-default", "light-sunrise", "light-paper",
])
def test_explicit_theme_env_returns_matching_preset(monkeypatch, preset):
    """Setting THEME=<preset> must return that exact palette and an env source."""
    import JFI.manager.rich_console_manager as rcm

    monkeypatch.setenv("THEME", preset)
    palette, source = rcm.resolve_theme()
    assert palette == dict(rcm.THEME_PRESETS[preset])
    assert source == f"env:THEME={preset}"


def test_six_presets_exist(monkeypatch):
    """The documented set is 3 dark + 3 light presets."""
    import JFI.manager.rich_console_manager as rcm

    names = set(rcm.THEME_PRESETS)
    assert {
        "dark-default", "dark-ocean", "dark-mono",
        "light-default", "light-sunrise", "light-paper",
    } <= names


@pytest.mark.parametrize("raw", ["", "   ", "auto"])
def test_empty_or_auto_theme_falls_back_to_detection(monkeypatch, raw):
    """Empty/whitespace/auto THEME must not crash and must skip the env branch."""
    import JFI.manager.rich_console_manager as rcm

    monkeypatch.setenv("THEME", raw)
    # Force a deterministic auto-detection environment.
    monkeypatch.setattr(rcm.Console, "is_terminal", property(lambda self: True))
    monkeypatch.setenv("COLORFGBG", "15;0")  # bg=0 -> dark

    palette, source = rcm.resolve_theme()
    assert palette == dict(rcm.THEME_PRESETS["dark-default"])
    assert source == "auto (dark)"


def test_unknown_theme_falls_back_without_crashing(monkeypatch):
    """A typo in THEME logs a hint and falls back; it must not raise."""
    import JFI.manager.rich_console_manager as rcm

    monkeypatch.setenv("THEME", "pastel-dreams")
    monkeypatch.setattr(rcm.Console, "is_terminal", property(lambda self: True))
    monkeypatch.delenv("COLORFGBG", raising=False)  # default -> dark

    palette, source = rcm.resolve_theme()
    assert palette == dict(rcm.THEME_PRESETS["dark-default"])
    assert source.startswith("auto")


def test_light_env_hints_select_light_default(monkeypatch):
    """Auto-detection honors COLORFGBG light backgrounds."""
    import JFI.manager.rich_console_manager as rcm

    monkeypatch.delenv("THEME", raising=False)
    monkeypatch.setattr(rcm.Console, "is_terminal", property(lambda self: True))
    monkeypatch.setenv("COLORFGBG", "0;15")  # bg=15 -> light

    palette, source = rcm.resolve_theme()
    assert palette == dict(rcm.THEME_PRESETS["light-default"])
    assert source == "auto (light)"


def test_non_terminal_without_explicit_theme_returns_dark_default(monkeypatch):
    """No terminal + no THEME: deterministic dark default, unknown source."""
    import JFI.manager.rich_console_manager as rcm

    monkeypatch.delenv("THEME", raising=False)
    # ``is_terminal`` is a rich property; patch it with mock.PropertyMock.
    from unittest import mock

    with mock.patch.object(rcm.Console, "is_terminal", new=mock.PropertyMock(return_value=False)):
        palette, source = rcm.resolve_theme()

    assert palette == dict(rcm.THEME_PRESETS["dark-default"])
    assert source is None


def test_rich_console_manager_builds_with_each_preset(monkeypatch):
    """Instantiating the manager under every preset must not raise."""
    import JFI.manager.rich_console_manager as rcm

    for preset in sorted(rcm.THEME_PRESETS):
        monkeypatch.setenv("THEME", preset)
        mgr = rcm.RichConsoleManager()
        assert mgr.theme_source == f"env:THEME={preset}"
        # Rich Theme is a dict subclass; the palette keys must be present.
        for key in ("user_theme", "assistant_theme", "system_theme"):
            assert key in mgr.theme.styles


# --- Normalization gaps: case + underscores on the Rich path -----------------
# The contract says ``Dark_Ocean`` == ``dark_ocean`` == ``dark-ocean``. These
# variants were only exercised on the prompt_toolkit side; pin them here too so
# a regression in normalize_theme_name() can't silently break .env themes.

@pytest.mark.parametrize("preset, raw", [
    ("dark-default", "Dark_Default"),
    ("dark-default", "DARK_DEFAULT"),
    ("dark-ocean", "  Dark_Ocean  "),
    ("dark-mono", "dark_mono"),
    ("light-default", "Light-Default"),
    ("light-sunrise", "LIGHT_SUNRISE"),
    ("light-paper", "Light_Paper"),
])
def test_explicit_theme_normalizes_case_and_underscores(monkeypatch, preset, raw):
    """A THEME value with mixed case and/or underscores must resolve to the same
    normalized palette as its canonical name — never fall back or crash."""
    import JFI.manager.rich_console_manager as rcm

    monkeypatch.setenv("THEME", raw)
    # Force a dark terminal so that *if* normalization regressed and we fell
    # through to auto-detection, the palette would mismatch the expectation.
    monkeypatch.setattr(rcm.Console, "is_terminal", property(lambda self: True))
    monkeypatch.delenv("COLORFGBG", raising=False)

    palette, source = rcm.resolve_theme()
    assert palette == dict(rcm.THEME_PRESETS[preset])
    assert source == f"env:THEME={preset}"


@pytest.mark.parametrize("raw", ["", "   ", "auto"])
def test_auto_variants_bypass_env_branch_even_on_terminal(monkeypatch, raw):
    """Empty/whitespace/auto must be treated as 'not set' — the env branch is
    skipped entirely and detection runs (no crash, no [system] hint)."""
    import JFI.manager.rich_console_manager as rcm

    monkeypatch.setenv("THEME", raw)
    monkeypatch.setattr(rcm.Console, "is_terminal", property(lambda self: True))
    monkeypatch.setenv("COLORFGBG", "0;15")  # bg=15 -> light

    palette, source = rcm.resolve_theme()
    assert palette == dict(rcm.THEME_PRESETS["light-default"])
    assert source == "auto (light)"


def test_explicit_preset_wins_over_light_terminal(monkeypatch):
    """An explicit dark preset must override a light terminal background."""
    import JFI.manager.rich_console_manager as rcm

    monkeypatch.setenv("THEME", "dark-ocean")
    monkeypatch.setattr(rcm.Console, "is_terminal", property(lambda self: True))
    monkeypatch.setenv("COLORFGBG", "0;15")  # light terminal

    palette, source = rcm.resolve_theme()
    assert palette == dict(rcm.THEME_PRESETS["dark-ocean"])
    assert source == "env:THEME=dark-ocean"


def test_resolve_theme_returns_copy_not_shared_dict(monkeypatch):
    """The returned palette must be a fresh dict so callers can't mutate the preset table."""
    import JFI.manager.rich_console_manager as rcm

    monkeypatch.setenv("THEME", "dark-mono")
    before = {k: v for k, v in rcm.THEME_PRESETS["dark-mono"].items()}
    palette, _ = rcm.resolve_theme()
    assert palette is not rcm.THEME_PRESETS["dark-mono"]
    palette["user_theme"] = "magenta"
    assert dict(rcm.THEME_PRESETS["dark-mono"]) == before


def test_unknown_theme_on_non_terminal_still_falls_back(monkeypatch):
    """Unknown THEME + non-terminal: deterministic dark default, source None."""
    import JFI.manager.rich_console_manager as rcm

    monkeypatch.setenv("THEME", "pastel-dreams")
    with mock.patch.object(rcm.Console, "is_terminal", new=mock.PropertyMock(return_value=False)):
        palette, source = rcm.resolve_theme()

    assert palette == dict(rcm.THEME_PRESETS["dark-default"])
    assert source is None


def test_unknown_theme_prints_system_hint(monkeypatch, capsys):
    """The fallback hint must name the value and list valid presets."""
    import JFI.manager.rich_console_manager as rcm

    monkeypatch.setenv("THEME", "pastel-dreams")
    monkeypatch.setattr(rcm.Console, "is_terminal", property(lambda self: True))
    monkeypatch.delenv("COLORFGBG", raising=False)

    rcm.resolve_theme()
    out = capsys.readouterr().out
    assert "[system] Unknown THEME 'pastel-dreams'" in out
    for preset in sorted(rcm.THEME_PRESETS):
        assert preset in out


# --- prompt_toolkit path: resolve_pt_theme ----------------------------------
# The live full-screen UI uses its own preset table (prompt_toolkit style
# overrides, not Rich color names), so the .env contract must hold there too.

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
    ("dark-ocean", "  DARK_OCEAN  "),
    ("light-paper", "Light_Paper"),
])
def test_pt_explicit_preset_normalizes_case_and_underscores(monkeypatch, preset, raw):
    """Same normalization contract as the Rich path: case + _ vs - are ignored."""
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
    """Explicit preset always wins over auto-detection, on the PT path too."""
    import JFI.manager.pt_console_manager as ptm

    monkeypatch.setenv("THEME", "dark-ocean")
    monkeypatch.setenv("COLORFGBG", "0;15")  # light terminal

    overrides, source = ptm.resolve_pt_theme()
    assert overrides == dict(ptm.PT_THEME_PRESETS["dark-ocean"])
    assert source == "env:THEME=dark-ocean"


def test_pt_unknown_preset_falls_back_without_raising(monkeypatch):
    """Unknown THEME on the PT path must auto-detect, not crash."""
    import JFI.manager.pt_console_manager as ptm

    monkeypatch.setenv("THEME", "pastel-dreams")
    monkeypatch.delenv("COLORFGBG", raising=False)  # dark terminal

    overrides, source = ptm.resolve_pt_theme()
    assert overrides == dict(ptm.PT_THEME_PRESETS["dark-default"])
    assert source == "auto (dark)"


def test_pt_unknown_preset_prints_system_hint(monkeypatch, capsys):
    """The fallback hint names the value and lists every valid PT preset."""
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


def test_pt_preset_names_match_rich_table():
    """Both renderers must expose the same six preset names so a THEME value
    in .env is valid for whichever renderer is running."""
    import JFI.manager.rich_console_manager as rcm

    assert set(PT_THEME_PRESETS) == set(rcm.THEME_PRESETS)


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
