"""Helpers for resolving the ``THEME`` environment variable.

Kept separate from the console manager itself so the rules for turning a raw
``THEME`` value into a preset lookup live in one small, easily-tested place:

1. strip surrounding whitespace;
2. if it looks like a JSON object (starts with ``{``), treat it as an inline
   custom theme — a style-class -> style-string mapping in the exact shape of
   one ``PT_THEME_PRESETS`` entry — instead of a preset name;
3. otherwise, lower-case and translate ``_`` to ``-`` (so ``Dark_Ocean`` ==
   ``dark-ocean``) and look it up as a preset name;
4. treat an empty result *and* the literal ``auto`` as "not set", i.e. fall
   back to terminal auto-detection;
5. unknown names, and malformed/invalid-shaped inline JSON, log a one-line
   hint and fall back too, so a typo (or a bad hand-written theme) can never
   crash startup.
"""

import json
import os
from typing import Dict, Optional


def normalize_theme_name(raw: str) -> str:
    """Normalize a raw ``THEME`` value for preset lookup (rule 1 + rule 3)."""
    return raw.strip().lower().replace("_", "-")


def is_auto(raw: str) -> bool:
    """True when the value means "auto-detect" (empty, whitespace, or 'auto')."""
    normalized = normalize_theme_name(raw)
    return normalized in ("", "auto")


def looks_like_custom_theme(raw: str) -> bool:
    """True when `raw` is meant to be parsed as inline JSON rather than looked
    up as a preset name — decided purely by shape (starts with '{'), so a
    malformed JSON blob is still reported as a broken custom theme rather than
    silently treated as an unknown preset name."""
    return raw.strip().startswith("{")


def parse_custom_theme(raw: str) -> Optional[Dict[str, str]]:
    """Parses `raw` as an inline custom theme: a JSON object mapping style
    classes to prompt_toolkit style strings, e.g.
    ``{"": "bg:#112233 fg:#eeeeee", "out.user": "bold #ff0000"}`` — the exact
    shape of one ``PT_THEME_PRESETS`` entry, so any subset of style classes
    (not just the four message-role keys) may be overridden. Returns the
    parsed dict on success, or ``None`` if it isn't valid JSON, isn't a flat
    object, or has any non-string key/value — never raises.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
        return None
    return parsed


def resolve_explicit_theme(
    presets: Dict[str, Dict[str, str]], env_var: str = "THEME"
) -> Optional[tuple[Dict[str, str], str]]:
    """Resolve an explicit ``env_var`` value — a known preset name OR an
    inline custom theme — or report that auto-detection applies.

    Returns ``(style_dict_copy, f"env:{env_var}=<name>")`` for a known preset,
    ``(custom_dict, f"env:{env_var}=<custom theme>")`` for a valid inline JSON
    theme (explicit values always win over auto-detection either way), and
    ``None`` when the value is empty/``auto`` — in which case the caller runs
    its terminal detection. An unknown preset name, or JSON-shaped text that
    fails to parse/validate, prints a system hint and also returns ``None`` so
    startup never crashes.
    """
    raw_theme = os.environ.get(env_var, "")

    if is_auto(raw_theme):
        return None

    if looks_like_custom_theme(raw_theme):
        custom = parse_custom_theme(raw_theme.strip())
        if custom is not None:
            return dict(custom), f"env:{env_var}=<custom theme>"
        print(
            f"[system] {env_var} looks like an inline custom theme (starts with '{{') but "
            f"is not valid JSON of style-name -> style-string pairs. Falling back to "
            f"auto-detection."
        )
        return None

    preset_name = normalize_theme_name(raw_theme)
    preset = presets.get(preset_name)
    if preset is not None:
        return dict(preset), f"env:{env_var}={preset_name}"

    print(
        f"[system] Unknown {env_var} '{raw_theme}' — expected one of "
        f"{', '.join(sorted(presets))}, or an inline custom theme as a JSON object. "
        f"Falling back to auto-detection."
    )
    return None


def theme_label(source: Optional[str]) -> str:
    """Human-readable label for the resolved theme source, e.g. ``env:THEME=dark-ocean``
    or ``auto (dark)``. Used in status lines so users can confirm their .env value took effect."""
    if source is None:
        return "theme: auto"
    return f"theme: {source}"
