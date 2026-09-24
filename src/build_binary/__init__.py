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
DASHBOARD_SRC = PROJECT_ROOT / "src" / "JFI" / "web" / "dashboard.py"
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
    args = [
        str(ENTRY_POINT),
        "--name", BINARY_NAME,
        "--onefile",
        "--console",
        "--noconfirm",
        "--paths", str(PROJECT_ROOT / "src"),
        # These lazy-import parts of themselves in ways PyInstaller's static
        # analysis misses on its own — collect them fully rather than
        # chasing individual --hidden-import flags as new gaps show up.
        # playwright specifically also ships a non-Python driver (a bundled
        # Node.js binary + JS files under playwright/driver/) that it spawns
        # as a subprocess to actually control the browser — without
        # --collect-all pulling those data files in too, a frozen build can
        # launch playwright's Python wrapper fine but fail to actually start
        # a browser (a confusing "browser binary not installed"-shaped error
        # even though the real cause is the missing driver, not the browser
        # in ~/.cache/ms-playwright/, which is a separate, correctly-found
        # download either way).
        "--collect-all", "prompt_toolkit",
        "--collect-all", "openai",
        "--collect-all", "playwright",
    ]

    # Bundling streamlit makes the standalone binary self-sufficient for the
    # web dashboard too: runner._launch_web_dashboard re-invokes this same
    # binary with a hidden --internal-web-dashboard flag when no separate
    # `jfi-web` is on PATH (see JFI.web.launcher._dashboard_path), so
    # JFI_WEB_BRIDGE=1 "just works" with nothing else installed. Only done
    # when streamlit is actually present in THIS build environment (the
    # `web` extra is optional -- `uv sync --extra web` before building);
    # skipped otherwise so a build without it still succeeds, just without
    # that self-contained dashboard (falls back to needing `jfi-web`
    # installed separately, exactly as before this existed).
    try:
        import streamlit  # noqa: F401
    except ImportError:
        print(
            "Note: 'streamlit' isn't installed in this environment, so the built binary "
            "won't be able to serve the web dashboard on its own (JFI_WEB_BRIDGE=1 will still "
            "write status files, but auto-launching jfi-web needs it installed separately). "
            "Run 'uv sync --extra web' first to bundle it in.",
        )
    else:
        args += ["--collect-all", "streamlit", "--add-data", f"{DASHBOARD_SRC}:JFI/web"]

    args += [
        "--distpath", str(PROJECT_ROOT / "dist"),
        "--workpath", str(build_dir),
        "--specpath", str(build_dir),
    ]
    PyInstaller.__main__.run(args)

    output_path = PROJECT_ROOT / "dist" / BINARY_NAME
    if sys.platform == "darwin":
        # PyInstaller's own re-sign step (see its "Re-signing the EXE" log
        # line above) leaves arm64 builds with a signature the OS's launch
        # policy rejects outright -- observed in practice as an instant
        # SIGKILL (exit 137) on the very first run, before Python even
        # starts, with no error output at all (`spctl -a -vvv` reports
        # "rejected"). Apple Silicon requires a VALID signature just to
        # load a Mach-O binary, ad-hoc is fine -- re-sign here so a fresh
        # build/copy always runs immediately rather than needing this
        # tracked down again after every build.
        import subprocess
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(output_path)], check=True)

    print(f"\nBuilt {output_path}")


if __name__ == "__main__":
    main()
