import json
from pathlib import Path
from typing import Callable, Dict

# Keys that are internal bookkeeping, not facts the model wrote itself — kept
# out of context_lookup's index/search results so they don't clutter what is
# meant to be the model's own scratchpad. cmd_tools.APPROVED_CMD_KEY lives
# here too (import would be circular the other way: cmd_tools imports the
# load/save helpers below).
_INTERNAL_KEYS = {"approved-cmd"}


def load_context_cache(cache_path: str) -> dict:
    """Reads the whole context-cache JSON object at `cache_path`. Never
    raises: a missing file, unreadable file, malformed JSON, or a JSON value
    that isn't an object all come back as `{}` — the cache is meant to be
    disposable scratch state, not something a read failure should ever crash
    a phase over."""
    path = Path(cache_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_context_cache(data: dict, cache_path: str) -> None:
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# context_save / context_lookup
#
# CONTEXT_CACHE_RULES (simple_session_manager.py) used to tell the model to
# read_file the whole cache, merge its new fact in by hand, then write_file
# the entire object back — two calls per fact, and a real correctness trap:
# any write_file that didn't faithfully round-trip every existing key (easy
# on a long turn) silently dropped them, "approved-cmd" (see cmd_tools.py)
# included. context_save replaces that with one atomic read-modify-write
# call per fact, so a dropped key is no longer possible.
#
# context_lookup replaces the matching read_file-the-whole-thing habit for
# *retrieval*. The cache is meant to stay "a handful of high-value facts,
# not a transcript" (CONTEXT_CACHE_RULES), but nothing enforces that, and on
# a long-running session with many iterations it can grow past what's worth
# spending context tokens to dump wholesale every time it's needed. Rather
# than have the model maintain a second, separate keyword -> fact index
# (extra bookkeeping that can silently drift out of sync with the facts
# themselves — the exact failure mode this module exists to avoid), lookup
# is computed live over the cache on every call: cheap at this scale (a
# "handful" of entries), and structurally unable to go stale. A blank
# keyword returns the index itself — every key plus a short value preview —
# so the model can browse what exists before spending a call on any one
# fact's full text.
# ---------------------------------------------------------------------------

_PREVIEW_LEN = 80


def _preview(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    text = " ".join(text.split())
    return text[:_PREVIEW_LEN] + ("…" if len(text) > _PREVIEW_LEN else "")


def _facts(cache_path: str) -> dict:
    data = load_context_cache(cache_path)
    return {k: v for k, v in data.items() if k not in _INTERNAL_KEYS}


def context_save(key: str, value: str, cache_path: str) -> str:
    """
    Merges one fact into the context cache at `cache_path`, preserving every
    other key already there (including ones other tools own, like
    cmd_tools's "approved-cmd") — the single-call replacement for
    read_file + hand-merge + write_file.
    """
    key = (key or "").strip()
    if not key:
        return "Error: context_save needs a non-empty key."
    try:
        data = load_context_cache(cache_path)
        data[key] = value
        save_context_cache(data, cache_path)
    except Exception as e:
        return f"Error saving context key '{key}': {e}"
    return f"Success: Saved context key '{key}'."


def context_lookup(keyword: str, cache_path: str) -> str:
    """
    Searches the context cache instead of dumping it wholesale.

    A blank `keyword` lists every fact's key plus a short preview — use this
    first to see what's already known. A non-blank `keyword` is matched
    case-insensitively against both keys and values; matches are returned in
    full (not previewed), since a filtered result is expected to already be
    small.
    """
    try:
        facts = _facts(cache_path)
    except Exception as e:
        return f"Error reading context cache: {e}"

    if not facts:
        return "Context cache is empty — nothing has been saved yet."

    keyword = (keyword or "").strip()
    if not keyword:
        lines = [f"- {k}: {_preview(v)}" for k, v in sorted(facts.items())]
        return "Context cache index (" + str(len(facts)) + " key(s)):\n" + "\n".join(lines)

    needle = keyword.lower()
    hits = {
        k: v for k, v in facts.items()
        if needle in k.lower() or needle in (v if isinstance(v, str) else json.dumps(v)).lower()
    }
    if not hits:
        keys = ", ".join(sorted(facts)) or "(none)"
        return f"No context entries match '{keyword}'. Existing keys: {keys}"

    lines = [f"- {k}: {v if isinstance(v, str) else json.dumps(v)}" for k, v in sorted(hits.items())]
    return f"{len(hits)} match(es) for '{keyword}':\n" + "\n".join(lines)


def make_context_save(cache_path: str) -> Callable[[str, str], str]:
    """Binds context_save to one session's own context.json — mirrors
    cmd_tools.make_gated_execute_command's per-session binding pattern."""
    def bound(key: str, value: str) -> str:
        return context_save(key, value, cache_path)
    return bound


def make_context_lookup(cache_path: str) -> Callable[[str], str]:
    def bound(keyword: str = "") -> str:
        return context_lookup(keyword, cache_path)
    return bound


def make_context_tools(cache_path: str) -> Dict[str, Callable]:
    """{"context_save": ..., "context_lookup": ...}, both bound to one
    session's context.json — what runner.py wires into TOOL_MAP per session,
    the same way execute_command is rebound via make_gated_execute_command."""
    return {
        "context_save": make_context_save(cache_path),
        "context_lookup": make_context_lookup(cache_path),
    }
