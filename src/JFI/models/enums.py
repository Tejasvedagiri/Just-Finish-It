"""Shared enums for the DB-backed plan/session models (see models/__init__.py).

PHASES/LeafStatus values are plain strings (not a Python-only IntEnum) so
they round-trip identically through SQLite/MySQL/Postgres and through the
JSON sent over the master websocket -- a caller on either side can compare
against the literal string without importing this module.
"""

from enum import Enum


class Phase(str, Enum):
    """Every phase a session has ever had. runner.PHASES (and PHASES in
    Just-Finish-It-Fleet's src/main.js) are the v2 four; PRODUCT_OWNER and
    TESTING stay so export-db can still read v1 sessions."""

    PLANNER = "planner"
    PRODUCT_OWNER = "product_owner"
    IMP = "imp"
    TESTING = "testing"
    REVIEWER = "reviewer"
    CLEANUP = "cleanup"


class LeafStatus(str, Enum):
    """A leaf's progress, rendered as the checklist's three markers:
    "- [ ]" (TODO), "- [x]" (DONE), "- [○]" (SKIPPED)."""

    TODO = "todo"
    DONE = "done"
    SKIPPED = "skipped"
