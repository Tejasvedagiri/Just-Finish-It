"""
Tests for the .JFI plan-file location and related helpers:

- SimpleSessionManager._resolve_plan_path always points into this session's
  .JFI folder (cwd-relative when possible).
- get_system_message embeds that path in every phase prompt.
- _pending_items parses GitHub task-list items per section, ignoring "- [x]"
  lines and items from other sections; a missing plan yields [].
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

    # session folder is .JFI/<session_id>; plan lives inside it.
    assert mgr.session_path == Path(str(tmp_path)) / ".JFI" / "demo_session"

    with patch("os.environ", {"SESSION_PATH": str(tmp_path)}):
        resolved = mgr._resolve_plan_path()

    # The path must be cwd-relative (no leading slash) and end in the plan.
    assert not Path(resolved).is_absolute(), f"path should be cwd-relative: {resolved}"
    assert resolved.endswith("plan.md")
    # And it must route through this session's .JFI folder.
    assert "/.JFI/" in resolved or resolved.startswith(".JFI/")


def test_resolve_plan_path_relative_to_cwd(console, tmp_path):
    """When SESSION_PATH is the cwd itself, the plan path is '.JFI/<id>/plan.md'."""
    with patch("os.environ", {"SESSION_PATH": str(tmp_path)}), \
         patch("pathlib.Path.cwd", return_value=Path(str(tmp_path))):
        mgr = SimpleSessionManager(console, "abc")
        resolved = mgr._resolve_plan_path()

    assert resolved == ".JFI/abc/plan.md"


def test_resolve_plan_path_fallback_outside_cwd(console, tmp_path):
    """When the session dir is outside the tool sandbox cwd, fall back to '.JFI/<id>/plan.md'."""
    with patch("os.environ", {"SESSION_PATH": str(tmp_path)}), \
         patch("pathlib.Path.cwd", return_value=Path("/nonexistent-cwd-for-test")):
        mgr = SimpleSessionManager(console, "xyz")
        resolved = mgr._resolve_plan_path()

    assert resolved == ".JFI/xyz/plan.md"


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
    ],
)
def test_system_message_contains_plan_path_and_marker(phase, marker):
    msg = get_system_message(phase, plan_path=".JFI/session1/plan.md")
    assert ".JFI/session1/plan.md" in msg
    assert marker in msg


def test_system_message_default_plan_path():
    """The default fallback path is the .JFI folder (not the old project root)."""
    assert DEFAULT_PLAN_PATH == ".JFI/plan.md"
    # Every phase prompt defaults to that location.
    for phase in ("planner", "imp", "testing", "reviewer"):
        msg = get_system_message(phase)
        assert DEFAULT_PLAN_PATH in msg


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

    # Write the plan into the session's .JFI folder.
    plan_file = Path(str(tmp_path)) / ".JFI" / "plan-test" / "plan.md"
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
