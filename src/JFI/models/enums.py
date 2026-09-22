"""Shared enums for the DB-backed plan/session models (see models/__init__.py).

PHASES/LeafStatus values are plain strings (not a Python-only IntEnum) so
they round-trip identically through SQLite/MySQL/Postgres and through the
JSON sent over the master websocket -- a caller on either side can compare
against the literal string without importing this module.
"""

from enum import Enum


class Phase(str, Enum):
    """Mirrors PHASES in frontend/src/main.js and PHASE_SECTION's keys in
    simple_session_manager.py -- keep all three in sync if this ever changes."""

    PLANNER = "planner"
    PRODUCT_OWNER = "product_owner"
    IMP = "imp"
    TESTING = "testing"
    REVIEWER = "reviewer"
    CLEANUP = "cleanup"


class LeafStatus(str, Enum):
    """Mirrors plan.md's three checkbox markers today: "- [ ]" (TODO),
    "- [x]" (DONE), "- [○]" (SKIPPED, Ctrl+K) -- see PLAN_FORMAT_RULES in
    simple_session_manager.py for the markdown-era equivalents this replaces."""

    TODO = "todo"
    DONE = "done"
    SKIPPED = "skipped"
