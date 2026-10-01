"""Git checkpoints per leaf (JFI.tool.checkpoint_tools), against real git and
a real SQLite DB. The checkpoints live in .jfi/checkpoints.git; the project's
own repository must never be touched."""
import shutil
import subprocess

import pytest

from JFI.models import Leaf, get_engine, get_session
from JFI.models.enums import Phase
from JFI.tool.checkpoint_tools import GIT_DIR, checkpoint, ensure_baseline, leaf_diff, revert_leaf

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _leaf(engine, description, sha=None):
    with get_session(engine) as db:
        leaf = Leaf(session_id="s", phase=Phase.IMP, description=description, checkpoint=sha)
        db.add(leaf)
        db.commit()
        db.refresh(leaf)
        return leaf.id


def test_each_leaf_diff_is_only_its_own_change_and_a_files_leaves_are_listed(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    raise NotImplementedError\n")
    engine = get_engine(tmp_path)
    ensure_baseline(tmp_path, "s")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    first = _leaf(engine, "implement add()", checkpoint(tmp_path, "s", "leaf 1: implement add()"))
    (tmp_path / "test_calc.py").write_text("from calc import add\n")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    second = _leaf(engine, "write the tests", checkpoint(tmp_path, "s", "leaf 2: write the tests"))

    one = leaf_diff(engine, "s", tmp_path, first)
    assert "+    return a + b" in one and "-    raise NotImplementedError" in one and "test_calc.py" not in one
    two = leaf_diff(engine, "s", tmp_path, second)
    assert "test_calc.py" in two and "+    return a - b" in two, "the later leaf that broke add() shows up"
    history = leaf_diff(engine, "s", tmp_path, path="calc.py")
    assert history.index("leaf 2:") < history.index("leaf 1:") and "baseline" in history
    assert "JFI.db" not in two, ".jfi/ is never checkpointed"


def test_the_projects_own_git_is_untouched(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x\n")
    ensure_baseline(tmp_path, "s")
    (tmp_path / "a.txt").write_text("y\n")
    assert checkpoint(tmp_path, "s", "leaf 1: change a")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "?? a.txt" in status, "the user's index is as it was: nothing staged"
    refs = subprocess.run(["git", "for-each-ref"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert refs == "" and (tmp_path / GIT_DIR / "HEAD").exists()


def test_a_leaf_without_a_checkpoint_says_why(tmp_path):
    engine = get_engine(tmp_path)
    assert leaf_diff(engine, "s", tmp_path, 1).startswith("Error: there are no checkpoints")
    ensure_baseline(tmp_path, "s")
    assert "hasn't passed mark_leaf_done" in leaf_diff(engine, "s", tmp_path, _leaf(engine, "todo"))
    assert leaf_diff(engine, "s", tmp_path).startswith("Error: pass leaf_id")


def test_revert_puts_a_leafs_files_back_and_deletes_what_it_created(tmp_path):
    """A reopened leaf whose approach is wrong is rebuilt from the state before
    it, not patched on top."""
    (tmp_path / "calc.py").write_text("def add(a, b):\n    raise NotImplementedError\n")
    ensure_baseline(tmp_path, "s")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "helpers.py").write_text("WRONG = True\n")
    sha = checkpoint(tmp_path, "s", "leaf 1: add")

    assert revert_leaf(tmp_path, "s", sha) == "Reverted 2 file(s) to before the leaf: calc.py, helpers.py."
    assert "raise NotImplementedError" in (tmp_path / "calc.py").read_text()
    assert not (tmp_path / "helpers.py").exists()


def test_revert_is_refused_when_later_work_touched_the_same_files(tmp_path):
    (tmp_path / "calc.py").write_text("x = 1\n")
    ensure_baseline(tmp_path, "s")
    (tmp_path / "calc.py").write_text("x = 2\n")
    first = checkpoint(tmp_path, "s", "leaf 1: two")
    (tmp_path / "calc.py").write_text("x = 3\n")
    checkpoint(tmp_path, "s", "leaf 2: three")
    refused = revert_leaf(tmp_path, "s", first)
    assert refused.startswith("Error: not reverted -- later leaves changed them too") and "leaf 2: three" in refused
    assert (tmp_path / "calc.py").read_text() == "x = 3\n"

    (tmp_path / "other.py").write_text("y = 1\n")
    second = checkpoint(tmp_path, "s", "leaf 3: other")
    (tmp_path / "other.py").write_text("y = 2\n")
    assert "edited after the last checkpoint: other.py" in revert_leaf(tmp_path, "s", second)
