"""
Tests for the JFI plan-file location and related helpers:

- SimpleSessionManager._resolve_plan_path always points into the flat
  `.jfi/` folder (cwd-relative when possible) -- one folder per PROJECT,
  not per session, see SimpleSessionManager.__init__'s own note on why.
- get_system_message: the plan itself is DB-backed now (see
  JFI.tool.plan_db_tools) -- most phases work through get_plan()/add_leaf/
  mark_leaf_done rather than a literal plan.md path; cleanup is the
  exception, since it tidies the real bookkeeping folder those DB-backed
  phases' session-scoped files (review.md, NotesForReviewer.md, ...) still
  live in.
- _pending_items parses GitHub task-list items per section (the plan.md
  FALLBACK path, used only when a session's DB has no leaves yet -- see
  has_leaves() in JFI.tool.plan_db_tools), ignoring "- [x]" lines and items
  from other sections; a missing plan yields [].
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from JFI.session.simple_session_manager import (
    DEFAULT_PLAN_PATH,
    SimpleSessionManager,
    get_system_message,
)


# ---------------------------------------------------------------------------
# _resolve_plan_path
# ---------------------------------------------------------------------------

def test_resolve_plan_path_is_inside_jfi_folder(console, tmp_path):
    with patch("os.environ", {"SESSION_PATH": str(tmp_path)}), \
         patch("pathlib.Path.cwd", return_value=Path.cwd()):
        mgr = SimpleSessionManager(console, "Demo Session")

    # `.jfi/` is flat -- one folder per PROJECT, not per session_id.
    assert mgr.session_path == Path(str(tmp_path)) / ".jfi"

    with patch("os.environ", {"SESSION_PATH": str(tmp_path)}):
        resolved = mgr._resolve_plan_path()

    # The path must be cwd-relative (no leading slash) and end in the plan.
    assert not Path(resolved).is_absolute(), f"path should be cwd-relative: {resolved}"
    assert resolved.endswith("plan.md")
    # And it must route through the flat .jfi/ folder.
    assert "/.jfi/" in resolved or resolved.startswith(".jfi/")


def test_resolve_plan_path_relative_to_cwd(console, tmp_path):
    """When SESSION_PATH is the cwd itself, the plan path is '.jfi/plan.md'."""
    with patch("os.environ", {"SESSION_PATH": str(tmp_path)}), \
         patch("pathlib.Path.cwd", return_value=Path(str(tmp_path))):
        mgr = SimpleSessionManager(console, "abc")
        resolved = mgr._resolve_plan_path()

    assert resolved == ".jfi/plan.md"


def test_resolve_plan_path_fallback_outside_cwd(console, tmp_path):
    """When the session dir is outside the tool sandbox cwd, fall back to '.jfi/plan.md'."""
    with patch("os.environ", {"SESSION_PATH": str(tmp_path)}), \
         patch("pathlib.Path.cwd", return_value=Path("/nonexistent-cwd-for-test")):
        mgr = SimpleSessionManager(console, "xyz")
        resolved = mgr._resolve_plan_path()

    assert resolved == ".jfi/plan.md"


def test_session_id_is_lowercased_and_underscored(console, tmp_path):
    with patch("os.environ", {"SESSION_PATH": str(tmp_path)}):
        mgr = SimpleSessionManager(console, "My Cool Goal")
    assert mgr.session_id == "my_cool_goal"


# ---------------------------------------------------------------------------
# get_system_message — plan path present in every phase prompt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "phase,marker",
    [
        ("planner", "PLANNER_COMPLETE"),
        ("imp", "IMP_COMPLETE"),
        ("testing", "TESTING_COMPLETE"),
        ("reviewer", "REVIEWER_COMPLETE"),
        ("cleanup", "CLEANUP_COMPLETE"),
    ],
)
def test_system_message_contains_completion_marker(phase, marker):
    """The plan is DB-backed now (see JFI.tool.plan_db_tools) -- most phase
    prompts no longer embed a literal plan.md path at all (planner/imp/
    testing/reviewer work through get_plan()/add_leaf/mark_leaf_done
    instead); cleanup is the one phase that still names session-scoped
    file paths, since it's tidying the actual bookkeeping folder. Every
    phase prompt still ends with its own completion marker regardless."""
    msg = get_system_message(phase, plan_path=".jfi/plan.md")
    assert marker in msg


def test_cleanup_still_names_session_scoped_paths():
    """Cleanup is the one phase that genuinely still deals in real
    filesystem paths (tidying the flat .jfi/ bookkeeping folder)."""
    msg = get_system_message("cleanup", plan_path=".jfi/plan.md")
    assert ".jfi" in msg


def test_system_message_default_plan_path():
    """The default fallback path is the flat .jfi/ folder (not the old
    project root, and not a per-session subfolder) -- still used by
    cleanup and by session-scoped file paths (review.md,
    NotesForReviewer.md, ...) other phases derive from it."""
    assert DEFAULT_PLAN_PATH == ".jfi/plan.md"
    msg = get_system_message("cleanup")
    assert ".jfi" in msg


def test_system_message_unknown_phase_returns_empty():
    assert get_system_message("unknown") == ""


# ---------------------------------------------------------------------------
# _pending_items
# ---------------------------------------------------------------------------

PLAN_TEXT = """\
# Demo Project

## Context and Prerequisites
Some background.

## Implementation
- [x] 1.1 Done step
- [ ] 1.2 Pending step A
- [ ] 1.3 Pending step B

## Testing (iteration 2)
- [ ] 2.1 First test
- [x] 2.2 Second test done
"""


def _manager_with_plan(console, tmp_path):
    """Build a manager whose plan file contains PLAN_TEXT."""
    with patch("os.environ", {"SESSION_PATH": str(tmp_path)}), \
         patch("pathlib.Path.cwd", return_value=Path(str(tmp_path))):
        mgr = SimpleSessionManager(console, "plan-test")

    # Write the plan into the flat .jfi/ folder.
    plan_file = Path(str(tmp_path)) / ".jfi" / "plan.md"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(PLAN_TEXT, encoding="utf-8")
    return mgr


def test_pending_items_implementation(console, tmp_path):
    mgr = _manager_with_plan(console, tmp_path)
    pending = mgr._pending_items("Implementation")
    assert "- [ ] 1.2 Pending step A" in pending
    assert "- [ ] 1.3 Pending step B" in pending
    # Completed items are excluded.
    assert not any("[x]" in line for line in pending)


def test_pending_items_testing_with_suffix(console, tmp_path):
    mgr = _manager_with_plan(console, tmp_path)
    pending = mgr._pending_items("Testing")
    assert "- [ ] 2.1 First test" in pending
    # The completed 2.2 item is excluded even though it's in the section.
    assert not any("2.2" in line for line in pending)


def test_pending_items_excludes_other_sections(console, tmp_path):
    mgr = _manager_with_plan(console, tmp_path)
    impl = mgr._pending_items("Implementation")
    testing = mgr._pending_items("Testing")

    assert all("1." in line or "Pending" in line for line in impl)
    assert not any("2.1 First test" in line for line in impl)
    assert "- [ ] 2.1 First test" in testing


def test_pending_items_missing_plan_returns_empty(console, tmp_path):
    with patch("os.environ", {"SESSION_PATH": str(tmp_path)}), \
         patch("pathlib.Path.cwd", return_value=Path(str(tmp_path))):
        mgr = SimpleSessionManager(console, "no-plan")

    # No plan file written for this session.
    assert mgr._pending_items("Implementation") == []


# ---------------------------------------------------------------------------
# plan_progress + ensure_plan_file (plan tracking helpers)
# ---------------------------------------------------------------------------

def test_plan_progress_counts_checkboxes(console, tmp_path):
    mgr = _manager_with_plan(console, tmp_path)
    done, total = mgr.plan_progress()
    # Two "- [x]" lines and three "- [ ]" lines.
    assert (done, total) == (2, 5)


def test_ensure_plan_file_reports_ready(console, tmp_path):
    mgr = _manager_with_plan(console, tmp_path)
    result = mgr.ensure_plan_file()
    assert result is True
    # The status message should reference the plan path and progress.
    joined = " ".join(mgr.console.system_messages)
    assert "Plan ready" in joined
    assert "2/5 items ticked" in joined
