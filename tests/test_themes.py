"""Tests for the THEME env presets in manager/rich_console_manager.py.

Covers every preset in ``THEME_PRESETS``: an explicit ``THEME`` value must
return exactly that palette, while empty or unknown values fall back to
auto-detection (never crash).
"""

from unittest import mock

import pytest


def test_adaptive_palette_matches_default_presets():
    """_get_adaptive_palette is the auto-detection fallback; it must hand out
    the default dark/light palettes."""
    from manager.rich_console_manager import THEME_PRESETS, _get_adaptive_palette

    assert _get_adaptive_palette(True) == dict(THEME_PRESETS["dark-default"])
    assert _get_adaptive_palette(False) == dict(THEME_PRESETS["light-default"])


@pytest.mark.parametrize("preset", [
    "dark-default", "dark-ocean", "dark-mono",
    "light-default", "light-sunrise", "light-paper",
])
def test_explicit_theme_env_returns_matching_preset(monkeypatch, preset):
    """Setting THEME=<preset> must return that exact palette and an env source."""
    import manager.rich_console_manager as rcm

    monkeypatch.setenv("THEME", preset)
    palette, source = rcm.resolve_theme()
    assert palette == dict(rcm.THEME_PRESETS[preset])
    assert source == f"env:THEME={preset}"


def test_six_presets_exist(monkeypatch):
    """The documented set is 3 dark + 3 light presets."""
    import manager.rich_console_manager as rcm

    names = set(rcm.THEME_PRESETS)
    assert {
        "dark-default", "dark-ocean", "dark-mono",
        "light-default", "light-sunrise", "light-paper",
    } <= names


@pytest.mark.parametrize("raw", ["", "   ", "auto"])
def test_empty_or_auto_theme_falls_back_to_detection(monkeypatch, raw):
    """Empty/whitespace/auto THEME must not crash and must skip the env branch."""
    import manager.rich_console_manager as rcm

    monkeypatch.setenv("THEME", raw)
    # Force a deterministic auto-detection environment.
    monkeypatch.setattr(rcm.Console, "is_terminal", property(lambda self: True))
    monkeypatch.setenv("COLORFGBG", "15;0")  # bg=0 -> dark

    palette, source = rcm.resolve_theme()
    assert palette == dict(rcm.THEME_PRESETS["dark-default"])
    assert source == "auto (dark)"


def test_unknown_theme_falls_back_without_crashing(monkeypatch):
    """A typo in THEME logs a hint and falls back; it must not raise."""
    import manager.rich_console_manager as rcm

    monkeypatch.setenv("THEME", "pastel-dreams")
    monkeypatch.setattr(rcm.Console, "is_terminal", property(lambda self: True))
    monkeypatch.delenv("COLORFGBG", raising=False)  # default -> dark

    palette, source = rcm.resolve_theme()
    assert palette == dict(rcm.THEME_PRESETS["dark-default"])
    assert source.startswith("auto")


def test_light_env_hints_select_light_default(monkeypatch):
    """Auto-detection honors COLORFGBG light backgrounds."""
    import manager.rich_console_manager as rcm

    monkeypatch.delenv("THEME", raising=False)
    monkeypatch.setattr(rcm.Console, "is_terminal", property(lambda self: True))
    monkeypatch.setenv("COLORFGBG", "0;15")  # bg=15 -> light

    palette, source = rcm.resolve_theme()
    assert palette == dict(rcm.THEME_PRESETS["light-default"])
    assert source == "auto (light)"


def test_non_terminal_without_explicit_theme_returns_dark_default(monkeypatch):
    """No terminal + no THEME: deterministic dark default, unknown source."""
    import manager.rich_console_manager as rcm

    monkeypatch.delenv("THEME", raising=False)
    # ``is_terminal`` is a rich property; patch it with mock.PropertyMock.
    from unittest import mock

    with mock.patch.object(rcm.Console, "is_terminal", new=mock.PropertyMock(return_value=False)):
        palette, source = rcm.resolve_theme()

    assert palette == dict(rcm.THEME_PRESETS["dark-default"])
    assert source is None


def test_rich_console_manager_builds_with_each_preset(monkeypatch):
    """Instantiating the manager under every preset must not raise."""
    import manager.rich_console_manager as rcm

    for preset in sorted(rcm.THEME_PRESETS):
        monkeypatch.setenv("THEME", preset)
        mgr = rcm.RichConsoleManager()
        assert mgr.theme_source == f"env:THEME={preset}"
        # Rich Theme is a dict subclass; the palette keys must be present.
        for key in ("user_theme", "assistant_theme", "system_theme"):
            assert key in mgr.theme.styles
