"""Shared helpers for resolving the ``THEME`` environment variable.

Both console renderers (Rich and prompt_toolkit) keep their own preset tables
because they don't share a color-name vocabulary, but the *rules* for turning
a raw ``THEME`` value into a lookup must stay identical — this module is the
single place those rules live:

1. strip surrounding whitespace;
2. lower-case and translate ``_`` to ``-`` (so ``Dark_Ocean`` == ``dark-ocean``);
3. treat an empty result *and* the literal ``auto`` as "not set", i.e. fall
   back to terminal auto-detection;
4. unknown names log a one-line hint and fall back too, so a typo can never
   crash startup.
"""

import os
from typing import Dict, Optional


def normalize_theme_name(raw: str) -> str:
    """Normalize a raw ``THEME`` value for preset lookup (rule 1 + rule 2)."""
    return raw.strip().lower().replace("_", "-")


def is_auto(raw: str) -> bool:
    """True when the value means "auto-detect" (empty, whitespace, or 'auto')."""
    normalized = normalize_theme_name(raw)
    return normalized in ("", "auto")


def resolve_explicit_theme(
    presets: Dict[str, Dict[str, str]], env_var: str = "THEME"
) -> Optional[tuple[Dict[str, str], str]]:
    """Resolve an explicit ``env_var`` preset, or report that auto-detection applies.

    Returns ``(preset_copy, f"env:{env_var}=<name>")`` when the user named a
    known preset (explicit presets always win over auto-detection), and
    ``None`` when the value is empty/``auto`` — in which case the caller runs
    its terminal detection. An unknown non-auto name prints a system hint with
    the valid preset list and also returns ``None`` so startup never crashes.
    """
    raw_theme = os.environ.get(env_var, "")

    if is_auto(raw_theme):
        return None

    preset_name = normalize_theme_name(raw_theme)
    preset = presets.get(preset_name)
    if preset is not None:
        return dict(preset), f"env:{env_var}={preset_name}"

    print(
        f"[system] Unknown {env_var} '{raw_theme}' — expected one of "
        f"{', '.join(sorted(presets))}. Falling back to auto-detection."
    )
    return None


def theme_label(source: Optional[str]) -> str:
    """Human-readable label for the resolved theme source, e.g. ``env:THEME=dark-ocean``
    or ``auto (dark)``. Used in status lines so users can confirm their .env value took effect."""
    if source is None:
        return "theme: auto"
    return f"theme: {source}"
