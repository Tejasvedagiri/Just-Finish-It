"""context_save / context_lookup -- the model's own persistent scratchpad,
DB-backed via JFI.models.ContextEntry (full cutover replacement for
context.json; see /todo.md's Live validation section).

Pulled, never pushed: the model decides what it needs via these two tools
as ordinary calls in the normal loop -- LLM call -> tool call -> context
comes back -> the action -- rather than every saved fact being force-fed
into every system message whether relevant to this turn or not (the old
context.json era auto-loaded everything into every prompt; that's gone).

context_save replaces the old "read_file the whole cache, hand-merge,
write_file it back" habit with one atomic upsert per fact -- no risk of a
write dropping a key it didn't retype, which a whole-file rewrite always
risked. context_lookup replaces the matching whole-cache-dump habit for
retrieval: a blank keyword lists every key plus a short preview, a real
keyword searches keys and values case-insensitively.

get_context_value/set_context_value are the raw single-key accessors
underneath the two tools above -- also what cmd_tools.py uses directly to
store its own internal "approved-cmd" bookkeeping (see _INTERNAL_KEYS),
without going through the model-facing tool functions.
"""

from typing import Callable, Dict, Optional

from sqlmodel import select

from JFI.models import ContextEntry, get_session
from JFI.models._util import utcnow

# Keys that are internal bookkeeping, not facts the model wrote itself --
# kept out of context_lookup's index/search results so they don't clutter
# what is meant to be the model's own scratchpad. cmd_tools.APPROVED_CMD_KEY
# lives here too (import would be circular the other way: cmd_tools imports
# the accessors below).
_INTERNAL_KEYS = {"approved-cmd"}

_PREVIEW_LEN = 80


def get_context_value(engine, session_id: str, key: str) -> Optional[str]:
    with get_session(engine) as db:
        row = db.exec(
            select(ContextEntry).where(ContextEntry.session_id == session_id, ContextEntry.key == key)
        ).first()
        return row.value if row else None


def set_context_value(engine, session_id: str, key: str, value: str) -> None:
    with get_session(engine) as db:
        row = db.exec(
            select(ContextEntry).where(ContextEntry.session_id == session_id, ContextEntry.key == key)
        ).first()
        if row is None:
            db.add(ContextEntry(session_id=session_id, key=key, value=value))
        else:
            row.value = value
            row.updated_at = utcnow()
            db.add(row)
        db.commit()


def _all_facts(engine, session_id: str) -> Dict[str, str]:
    with get_session(engine) as db:
        rows = list(db.exec(select(ContextEntry).where(ContextEntry.session_id == session_id)))
    return {row.key: row.value for row in rows if row.key not in _INTERNAL_KEYS}


def _preview(value: str) -> str:
    text = " ".join(value.split())
    return text[:_PREVIEW_LEN] + ("…" if len(text) > _PREVIEW_LEN else "")


def context_save(engine, session_id: str, key: str, value: str) -> str:
    key = (key or "").strip()
    if not key:
        return "Error: context_save needs a non-empty key."
    try:
        set_context_value(engine, session_id, key, value)
    except Exception as e:
        return f"Error saving context key '{key}': {e}"
    return f"Success: Saved context key '{key}'."


def context_lookup(engine, session_id: str, keyword: str) -> str:
    try:
        facts = _all_facts(engine, session_id)
    except Exception as e:
        return f"Error reading context cache: {e}"

    if not facts:
        return "Context cache is empty — nothing has been saved yet."

    keyword = (keyword or "").strip()
    if not keyword:
        lines = [f"- {k}: {_preview(v)}" for k, v in sorted(facts.items())]
        return "Context cache index (" + str(len(facts)) + " key(s)):\n" + "\n".join(lines)

    needle = keyword.lower()
    hits = {k: v for k, v in facts.items() if needle in k.lower() or needle in v.lower()}
    if not hits:
        keys = ", ".join(sorted(facts)) or "(none)"
        return f"No context entries match '{keyword}'. Existing keys: {keys}"

    lines = [f"- {k}: {v}" for k, v in sorted(hits.items())]
    return f"{len(hits)} match(es) for '{keyword}':\n" + "\n".join(lines)


def make_context_tools(engine, session_id: str) -> Dict[str, Callable]:
    """{"context_save": ..., "context_lookup": ...} bound to one session's
    own DB engine -- what runner.py wires into TOOL_MAP, the same
    per-session rebinding pattern execute_command/plan_db_tools use."""
    return {
        "context_save": lambda key, value: context_save(engine, session_id, key, value),
        "context_lookup": lambda keyword="": context_lookup(engine, session_id, keyword),
    }
