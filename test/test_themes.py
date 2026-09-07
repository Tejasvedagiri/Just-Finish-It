"""Tests for the THEME env presets in manager/pt_console_manager.py.

Covers every preset in ``PT_THEME_PRESETS``: an explicit ``THEME`` value must
return exactly that palette, while empty or unknown values fall back to
auto-detection (never crash).
"""

import pytest

# Import at module level so the parametrize decorator can reference the table.
from JFI.manager.pt_console_manager import PT_THEME_PRESETS, UI_STYLE_BASE


def test_ten_presets_exist():
    """3 dark + 3 light originals, plus all four Catppuccin flavors."""
    names = set(PT_THEME_PRESETS)
    assert {
        "dark-default", "dark-ocean", "dark-mono",
        "light-default", "light-sunrise", "light-paper",
        "catppuccin-mocha", "catppuccin-macchiato",
        "catppuccin-frappe", "catppuccin-latte",
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
        runner.OpenAICompatableStream, "close", lambda self: None
    )
    fake_llm = type("FakeLLM", (), {"close": staticmethod(lambda *a, **k: None)})()
    monkeypatch.setattr(runner, "OpenAICompatableStream", lambda: fake_llm)

    def no_pipeline(console, llm):  # noqa: ANN001 - signature matches run_pipeline
        pass

    monkeypatch.setattr(runner, "run_pipeline", no_pipeline)
    runner.main()

    assert order == ["load_dotenv", "console_init"]


# --------------------------------------------------------- background fill
#
# Every preset except dark-default now sets a "" style rule (bg + default
# fg) so the *actual terminal background* changes with the theme, instead of
# leaving whatever background color the user's own terminal profile had —
# previously nothing did this, so THEME=dark-ocean recolored messages but the
# screen behind them stayed whatever the terminal already was.

NON_DEFAULT_PRESETS = sorted(name for name in PT_THEME_PRESETS if name != "dark-default")


@pytest.mark.parametrize("preset", NON_DEFAULT_PRESETS)
def test_non_default_preset_sets_background_fill(preset):
    """Every preset but dark-default carries a "" rule with both bg: and fg:."""
    rule = PT_THEME_PRESETS[preset].get("")
    assert rule is not None, f"{preset} has no '' background rule"
    assert "bg:#" in rule
    assert "fg:#" in rule


def test_dark_default_has_no_background_override():
    """dark-default must stay the terminal's own background — it's the one
    preset documented as 'inherits your terminal's own palette'."""
    assert PT_THEME_PRESETS["dark-default"] == {}


@pytest.mark.parametrize("preset", NON_DEFAULT_PRESETS)
def test_preset_background_actually_resolves_via_style(preset):
    """The '' rule isn't just present — it must be what a real Style object
    hands back for blank/unstyled screen space (Style.get_attrs_for_style_str
    matches every lookup against the "" class; see prompt_toolkit's
    styles/style.py). This is the exact mechanism the live app relies on to
    paint the background, not just decorate message text."""
    from prompt_toolkit.styles import Style

    style = Style.from_dict({**UI_STYLE_BASE, **PT_THEME_PRESETS[preset]})
    attrs = style.get_attrs_for_style_str("")
    expected_bg = PT_THEME_PRESETS[preset][""].split("bg:")[1].split()[0]
    assert attrs.bgcolor == expected_bg.lstrip("#")


def test_dark_default_background_is_unset_via_style():
    """Without a '' override, blank screen space keeps DEFAULT_ATTRS (empty
    bg/fg) so the terminal's own background actually shows through."""
    from prompt_toolkit.styles import Style

    style = Style.from_dict({**UI_STYLE_BASE, **PT_THEME_PRESETS["dark-default"]})
    attrs = style.get_attrs_for_style_str("")
    assert attrs.bgcolor == ""
    assert attrs.color == ""


CATPPUCCIN_BACKGROUNDS = {
    "catppuccin-mocha": "#1e1e2e",
    "catppuccin-macchiato": "#24273a",
    "catppuccin-frappe": "#303446",
    "catppuccin-latte": "#eff1f5",
}


@pytest.mark.parametrize("preset, bg", sorted(CATPPUCCIN_BACKGROUNDS.items()))
def test_catppuccin_presets_match_official_palette(preset, bg):
    """Pins each flavor's background to catppuccin/catppuccin's published hex
    (https://github.com/catppuccin/catppuccin) so a future edit can't drift
    from the real palette without this test catching it."""
    assert PT_THEME_PRESETS[preset][""].startswith(f"bg:{bg} ")


# ------------------------------------------------------------- color depth
#
# Even with the "" background rule above, the app rendered no visible color
# change: prompt_toolkit's own default color depth is 256-color (see
# vt100.Vt100_Output.get_default_color_depth — "we prefer 256 colors almost
# always"), so every literal 24-bit hex in PT_THEME_PRESETS was getting
# quantized down to the nearest xterm-256 entry — confirmed by capturing raw
# output in a real pty: THEME=dark-ocean's #141b26 background rendered as
# plain "48;5;234" (a generic near-black grey from the 256 palette), not the
# navy that was actually configured. PromptToolkitConsoleManager._resolve_color_depth
# is what fixes this — these tests pin its env-override precedence.

def test_resolve_color_depth_defaults_to_true_color(monkeypatch):
    from prompt_toolkit.output.color_depth import ColorDepth

    from JFI.manager.pt_console_manager import PromptToolkitConsoleManager as PTCM

    monkeypatch.delenv("PROMPT_TOOLKIT_COLOR_DEPTH", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert PTCM._resolve_color_depth() == ColorDepth.DEPTH_24_BIT


def test_resolve_color_depth_honors_prompt_toolkit_env_override(monkeypatch):
    from prompt_toolkit.output.color_depth import ColorDepth

    from JFI.manager.pt_console_manager import PromptToolkitConsoleManager as PTCM

    monkeypatch.setenv("PROMPT_TOOLKIT_COLOR_DEPTH", "DEPTH_4_BIT")
    assert PTCM._resolve_color_depth() == ColorDepth.DEPTH_4_BIT
