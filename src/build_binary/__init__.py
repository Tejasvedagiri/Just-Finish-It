"""Builds a standalone `jfi` binary with PyInstaller — no Python install
needed on the machine that runs it, unlike the `./JFI` launcher (which still
needs a system Python to bootstrap its own venv).

Usage: `uv run build` (wired up via [project.scripts] in pyproject.toml).
Output lands at dist/jfi (or dist/jfi.exe on Windows).
"""

import sys
from pathlib import Path

# src/build_binary/__init__.py -> src/build_binary -> src -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENTRY_POINT = PROJECT_ROOT / "src" / "JFI" / "runner.py"
BINARY_NAME = "jfi"


def main() -> None:
    try:
        import PyInstaller.__main__
    except ImportError:
        print(
            "PyInstaller is required to build the binary. Install the dev "
            "dependency group first: uv sync --group dev",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if not ENTRY_POINT.exists():
        print(f"Entry point not found: {ENTRY_POINT}", file=sys.stderr)
        raise SystemExit(1)

    build_dir = PROJECT_ROOT / "build" / "pyinstaller"
    PyInstaller.__main__.run([
        str(ENTRY_POINT),
        "--name", BINARY_NAME,
        "--onefile",
        "--console",
        "--noconfirm",
        "--paths", str(PROJECT_ROOT / "src"),
        # These two lazy-import parts of themselves in ways PyInstaller's
        # static analysis misses on its own — collect them fully rather than
        # chasing individual --hidden-import flags as new gaps show up.
        "--collect-all", "prompt_toolkit",
        "--collect-all", "openai",
        "--distpath", str(PROJECT_ROOT / "dist"),
        "--workpath", str(build_dir),
        "--specpath", str(build_dir),
    ])

    print(f"\nBuilt {PROJECT_ROOT / 'dist' / BINARY_NAME}")


if __name__ == "__main__":
    main()
