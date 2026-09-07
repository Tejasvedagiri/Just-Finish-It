"""Project file discovery for shbuild builds.

Collects the entry script plus every local module/package it (transitively)
references, using a lightweight AST scan of ``import`` / ``from ... import``
statements. Only *local* modules are collected: those resolvable to files or
packages next to the entry script (or inside its package). Well-known standard
library and third-party top-level names are left alone — they come from the
baked venv instead.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Top-level names that never resolve to local files: the Python standard
# library (as of 3.12) plus a few extremely common third-party packages we do
# not want to accidentally "discover" as local modules when they are missing.
STDLIB_TOP_LEVELS = frozenset(
    sys.stdlib_module_names  # type: ignore[attr-defined]
)

_COMMON_THIRD_PARTY = {
    "requests",
    "httpx",
    "openai",
    "rich",
    "typer",
    "dotenv",
    "yaml",
    "numpy",
    "pandas",
}


@dataclass
class DiscoveredProject:
    """Result of scanning the entry script and its local imports."""

    root: Path  # Directory containing the entry script (the build root)
    files: dict[str, Path] = field(default_factory=dict)  # rel path -> abs path

    def add(self, abs_path: Path) -> None:
        """Register a file under its root-relative POSIX path."""
        try:
            rel = abs_path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return
        self.files[rel] = abs_path


def _iter_imports(tree: ast.AST) -> list[tuple[str, str | None]]:
    """Yield (module_name, is_relative_level0_flag) for every import in tree.

    Returns a list of ``(dotted_module, level)`` where ``level`` is the number
    of leading dots for relative imports (0 = absolute).
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, 0))
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                # ``from . import x`` — handled via the level alone.
                found.append(("", node.level or 1))
            else:
                found.append((node.module, node.level or 0))
    return found


def _local_module_candidates(root: Path, dotted: str) -> list[Path]:
    """Map a dotted module name to candidate local file paths under root."""
    parts = dotted.split(".")
    base = root.joinpath(*parts)
    candidates = [base / "__init__.py", Path(str(base) + ".py")]
    return candidates


def discover(entry: Path, extra_files: list[Path] | None = None) -> DiscoveredProject:
    """Collect the entry script and all local modules it references.

    Args:
        entry: Absolute path to the entry script.
        extra_files: Optional additional files/packages to force-include
            (e.g. data files or packages not statically importable).

    Returns:
        A :class:`DiscoveredProject` whose ``files`` maps root-relative POSIX
        paths to absolute paths. The entry script itself is always included.
    """
    entry = entry.resolve()
    root = entry.parent
    project = DiscoveredProject(root=root)
    project.add(entry)

    if extra_files:
        for path in extra_files:
            resolved = Path(path).resolve()
            if resolved.is_file():
                project.add(resolved)
            elif (resolved / "__init__.py").is_file():
                _add_package(project, root, resolved)

    # BFS over locally-imported modules.
    seen_modules: set[str] = {entry.name[:-3]}  # entry's own module name
    queue: list[Path] = [entry]
    while queue:
        current = queue.pop(0)
        try:
            tree = ast.parse(current.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue

        for dotted, level in _iter_imports(tree):
            if level == 0:
                # Absolute import: local only if resolvable under root.
                top = dotted.split(".")[0]
                if top in seen_modules or top in STDLIB_TOP_LEVELS:
                    continue
                candidates = [c for c in _local_module_candidates(root, dotted) if c.is_file()]
            else:
                # Relative import: resolve against the package of `current`.
                pkg_dir = current.parent
                up = level - 1
                for _ in range(up):
                    pkg_dir = pkg_dir.parent
                if not dotted:
                    module_path: Path | None = pkg_dir / "__init__.py"
                    if module_path.is_file():
                        candidates = [module_path]
                    else:
                        continue
                else:
                    target = pkg_dir.joinpath(*dotted.split("."))
                    candidates = [
                        c
                        for c in (target / "__init__.py", Path(str(target) + ".py"))
                        if c.is_file()
                    ]

            for cand in candidates:
                module_name = _module_key(cand)
                if module_name in seen_modules:
                    continue
                seen_modules.add(module_name)
                if cand.name == "__init__.py":
                    # Pull in the whole package (its __init__ and submodules).
                    pkg_dir = cand.parent
                    for member in sorted(pkg_dir.rglob("*.py")):
                        project.add(member)
                        _enqueue_member(project, root, queue, seen_modules, member)
                else:
                    project.add(cand)
                    if module_name not in {m.name[:-3] for m in queue}:
                        queue.append(cand)

    return project


def _module_key(path: Path) -> str:
    """Stable key identifying a module file (its relative stem path)."""
    p = path
    if p.name == "__init__.py":
        p = p.parent
    else:
        p = p.with_suffix("")
    return "/".join(p.parts[-2:]) if len(p.parts) >= 2 else p.name


def _add_package(project: DiscoveredProject, root: Path, pkg_dir: Path) -> None:
    for member in sorted(pkg_dir.rglob("*.py")):
        project.add(member)


def _enqueue_member(
    project: DiscoveredProject,
    root: Path,
    queue: list[Path],
    seen_modules: set[str],
    member: Path,
) -> None:
    key = _module_key(member)
    if key not in seen_modules:
        seen_modules.add(key)
        queue.append(member)


def to_file_list(project: DiscoveredProject) -> list[tuple[str, Path]]:
    """Return the discovered files as (root-relative posix path, abs path)."""
    return sorted(project.files.items())
