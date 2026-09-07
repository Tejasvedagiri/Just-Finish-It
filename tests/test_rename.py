"""
Rename verification: every source occurrence of ".just_finish_it" must be gone,
replaced by ".JFI".

These tests scan the real repository on disk (excluding .venv and this test
module itself), so they guard against regressions as soon as they pass once.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _source_files():
    for path in sorted(REPO_ROOT.rglob("*")):
        if any(part in {".venv", ".git"} or part.startswith(".") and part != ".JFI"
               for part in path.parts[:-1]):
            continue
        # The rename matters for code + config; docs/plans may still explain it.
        if not (path.is_file() and path.suffix in {".py", ".toml"}):
            continue
        if path.name == "test_rename.py":  # don't match this module's own literals
            continue
        yield path


def test_no_just_finish_it_occurrences_in_source():
    hits = []
    for path in _source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if ".just_finish_it" in text or "just_finish_it" in text.lower():
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert not hits, f"'just_finish_it' still present in: {hits}"


def test_jfi_folder_present_and_used_in_code():
    """The .JFI folder is the canonical session/plan location."""
    from session.simple_session_manager import DEFAULT_PLAN_PATH

    assert DEFAULT_PLAN_PATH == ".JFI/plan.md"
