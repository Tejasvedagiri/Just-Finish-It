"""Builds a standalone `jfi` binary with PyInstaller — no Python or uv
needed on the machine that runs it.

It bundles everything, the optional parts included: the Streamlit dashboard
(`jfi-web`), Laya for the planner's judge (`LAYA=1`: torch + transformers,
several GB), and websockets for the fleet dashboard. The build refuses to run
without them rather than silently shipping a binary that lacks them.

Usage: `uv sync --extra web --extra laya --group dev`, then `uv run build`.
Output lands at dist/jfi (or dist/jfi.exe on Windows).
"""

import sys
from pathlib import Path

# src/build_binary/__init__.py -> src/build_binary -> src -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENTRY_POINT = PROJECT_ROOT / "src" / "JFI" / "runner.py"
DASHBOARD_SRC = PROJECT_ROOT / "src" / "JFI" / "web" / "dashboard.py"
BINARY_NAME = "jfi"
# Optional parts the binary must carry: module -> the extra that installs it.
# The user: "Make sure you bundle laya and websocket" / "I need streamlit too".
REQUIRED = {"streamlit": "web", "laya": "laya"}
# Laya's checkpoints are ModernBERT encoders; transformers imports a model's
# code by name when the checkpoint loads, which static analysis can't see.
LAYA_MODEL_PACKAGES = ("transformers.models.modernbert",)


def _missing_extras() -> list[str]:
    import importlib.util
    return [extra for module, extra in REQUIRED.items() if importlib.util.find_spec(module) is None]


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

    missing = _missing_extras()
    if missing:
        print(
            f"The binary bundles the {', '.join(missing)} extra(s), which aren't installed here. Run\n"
            "  uv sync --extra web --extra laya --group dev\n"
            "(every extra in one command: uv sync drops any extra it isn't told about), then build again.",
            file=sys.stderr,
        )
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
        # Imported lazily inside the fleet reporter's thread.
        "--collect-all", "websockets",
        # Laya and its model code; torch and transformers come in through
        # their own PyInstaller hooks.
        "--collect-all", "laya",
    ]
    for package in LAYA_MODEL_PACKAGES:
        args += ["--collect-submodules", package]

    # Bundling streamlit makes the binary self-sufficient for the web
    # dashboard: runner._launch_web_dashboard re-invokes this same binary with
    # a hidden --internal-web-dashboard flag when no separate `jfi-web` is on
    # PATH (see JFI.web.launcher._dashboard_path).
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
