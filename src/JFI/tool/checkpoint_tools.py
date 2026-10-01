"""Git checkpoints per leaf: JFI commits the project after every leaf that
passes mark_leaf_done, so `leaf_diff` can show what a leaf changed and which
leaves touched a file -- when a later leaf breaks earlier work, the reviewer
can see which one did it.

The commits live in a private repository, `.jfi/checkpoints.git`, with the
project as its work tree. The project's own git (if it has one) is never
touched: not its branch, its index nor its refs, and a project without git
gets checkpoints too. The project's .gitignore files still apply, and .jfi/
plus the usual dependency folders are excluded. Without a `git` executable
checkpoints are simply off."""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Dict, Optional

from JFI.models import Leaf, get_session
from JFI.tool.result_cap import cap_result

GIT_DIR = Path(".jfi") / "checkpoints.git"
EXCLUDES = (".jfi/", "node_modules/", ".venv/", "venv/", "__pycache__/", ".pytest_cache/", "dist/", "build/")
GIT_TIMEOUT_SECONDS = 120


def _ref(session_id: str) -> str:
    return "refs/jfi/" + re.sub(r"[^A-Za-z0-9._-]", "_", session_id)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_DIR": str(Path(root) / GIT_DIR), "GIT_WORK_TREE": str(root),
           "GIT_AUTHOR_NAME": "JFI", "GIT_AUTHOR_EMAIL": "jfi@localhost",
           "GIT_COMMITTER_NAME": "JFI", "GIT_COMMITTER_EMAIL": "jfi@localhost"}
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=off", *args], cwd=root,
                          env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=GIT_TIMEOUT_SECONDS)


def _ensure_repo(root: Path) -> bool:
    if shutil.which("git") is None:
        return False
    git_dir = Path(root) / GIT_DIR
    if not (git_dir / "HEAD").exists():
        git_dir.mkdir(parents=True, exist_ok=True)
        if _git(root, "init", "-q").returncode != 0:
            return False
        (git_dir / "info").mkdir(parents=True, exist_ok=True)
        (git_dir / "info" / "exclude").write_text("\n".join(EXCLUDES) + "\n", encoding="utf-8")
    return True


def _last(root: Path, session_id: str) -> Optional[str]:
    out = _git(root, "rev-parse", "-q", "--verify", _ref(session_id) + "^{commit}")
    return out.stdout.strip() if out.returncode == 0 else None


def checkpoint(root: Path, session_id: str, message: str) -> Optional[str]:
    """Commit the project's current files on this session's checkpoint chain;
    the new commit's sha, or None when git isn't available or fails."""
    root = Path(root)
    try:
        if not _ensure_repo(root) or _git(root, "add", "-A").returncode != 0:
            return None
        tree = _git(root, "write-tree")
        if tree.returncode != 0:
            return None
        parent = _last(root, session_id)
        commit = _git(root, "commit-tree", tree.stdout.strip(), *(["-p", parent] if parent else []), "-m", message)
        if commit.returncode != 0:
            return None
        sha = commit.stdout.strip()
        if _git(root, "update-ref", _ref(session_id), sha).returncode != 0:
            return None
        return sha
    except (OSError, subprocess.SubprocessError):
        return None


def ensure_baseline(root: Path, session_id: str) -> None:
    """The project as Dev found it, so the first leaf's diff is only its own."""
    if _ensure_repo(Path(root)) and _last(Path(root), session_id) is None:
        checkpoint(root, session_id, "baseline: the project before Dev")


def revert_leaf(root: Path, session_id: str, sha: str) -> str:
    """Undo one leaf's change: every file its checkpoint touched goes back to
    the state before it (a file it created is deleted), so a reopened leaf is
    rebuilt from scratch instead of patched on top of a wrong attempt. Refused
    when a later leaf, or an edit since the last checkpoint, changed one of
    those files -- restoring it would throw that work away."""
    root = Path(root)
    changed = _git(root, "diff-tree", "--root", "-r", "--no-renames", "--name-status", sha)
    if changed.returncode != 0:
        return f"Error: can't read checkpoint {sha[:10]}: {changed.stderr.strip()}"
    entries = [line.split("\t", 1) for line in changed.stdout.splitlines() if "\t" in line]
    files = [path for _, path in entries]
    if not files:
        return "Nothing to revert: the leaf's checkpoint changed no files."
    last = _last(root, session_id)
    later = _git(root, "log", "--format=%s", f"{sha}..{last}", "--", *files).stdout.strip()
    edited = _git(root, "diff", "--name-only", last, "--", *files).stdout.strip()
    if later or edited:
        why = (f"later leaves changed them too:\n{later}" if later
               else f"they were edited after the last checkpoint: {edited.replace(chr(10), ', ')}")
        return f"Error: not reverted -- {why}\nReopen without revert; the fix note tells Dev what to change."
    parent = f"{sha}^"
    has_parent = _git(root, "rev-parse", "-q", "--verify", parent).returncode == 0
    for status, path in entries:
        if status == "A" or not has_parent:
            (root / path).unlink(missing_ok=True)
        elif _git(root, "checkout", parent, "--", path).returncode != 0:
            return f"Error: couldn't restore {path} from before the leaf."
    return f"Reverted {len(files)} file(s) to before the leaf: {', '.join(files)}."


def leaf_diff(engine, session_id: str, root: Path, leaf_id: Optional[int] = None, path: Optional[str] = None) -> str:
    root = Path(root)
    if not (root / GIT_DIR / "HEAD").exists():
        return "Error: there are no checkpoints in this project (no leaf has finished, or git isn't installed)."
    if leaf_id is None:
        if not path:
            return "Error: pass leaf_id (what that leaf changed) or path (which leaves changed that file)."
        last = _last(root, session_id)
        if last is None:
            return "Error: no leaf of this session has finished yet."
        log = _git(root, "log", "--format=%h %s", last, "--", path)
        if log.returncode != 0:
            return f"Error: {log.stderr.strip()}"
        return (f"Leaves that changed {path} (newest first):\n{log.stdout.strip()}" if log.stdout.strip()
                else f"No finished leaf changed {path}.")
    with get_session(engine) as db:
        leaf = db.get(Leaf, int(leaf_id))
    if leaf is None or leaf.session_id != session_id:
        return f"Error: there is no leaf {leaf_id}. get_plan() lists them."
    if not leaf.checkpoint:
        return (f"Error: leaf {leaf_id} has no checkpoint: it hasn't passed mark_leaf_done yet (or git wasn't "
                f"available when it did).")
    shown = _git(root, "diff-tree", "--root", "-r", "-M", "--stat", "-p", leaf.checkpoint,
                 *(["--", path] if path else []))
    if shown.returncode != 0:
        return f"Error: {shown.stderr.strip()}"
    return cap_result(f"Leaf {leaf_id} ({leaf.description}), checkpoint {leaf.checkpoint[:10]}:\n"
                      f"{shown.stdout.strip() or '(no file changes)'}",
                      "Pass path to see one file's change.")


def make_checkpoint_tools(engine, session_id: str, root: Path) -> Dict[str, Callable]:
    return {"leaf_diff": lambda leaf_id=None, path=None: leaf_diff(engine, session_id, root, leaf_id, path)}


CHECKPOINT_TOOL_SCHEMAS = [{"type": "function", "function": {
    "name": "leaf_diff",
    "description": ("JFI commits the project after every leaf that passes. leaf_diff(leaf_id) shows what that leaf "
                    "changed (a diff); leaf_diff(path=...) lists the leaves that changed a file, newest first -- "
                    "to find which leaf broke something."),
    "parameters": {"type": "object", "properties": {
        "leaf_id": {"type": "integer"},
        "path": {"type": "string", "description": "a file; with leaf_id, limits the diff to it"},
    }},
}}]
