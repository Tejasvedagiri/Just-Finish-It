"""Builds a standalone `jfi` binary with PyInstaller — no Python or uv
needed on the machine that runs it.

It bundles everything, the optional parts included: the Streamlit dashboard
(`jfi-web`), Laya for the planner's judge (`LAYA=1`: torch + transformers,
several GB), and websockets for the fleet dashboard. The build refuses to run
without them rather than silently shipping a binary that lacks them.

Usage: `uv sync --extra web --extra laya --group dev`, then `uv run build`.
On Windows/Linux, output is a folder, dist/jfi/, with the executable
dist/jfi/jfi (jfi.exe on Windows) inside -- a onefile build unpacked its
2.2 GB (torch) on every launch there: 18-35 s before `jfi --version` even
answered. On macOS the output is a single file, dist/jfi, that can be copied
anywhere on its own -- requested over the onedir folder despite that same
per-launch unpack cost, because a onedir build's executable is useless
without its `_internal/` directory alongside it, and that's easy to leave
behind when copying just the binary out of dist/jfi/.
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


def remove_stale_build(dist: Path) -> None:
    """--onefile and --onedir both claim dist/jfi (dist/jfi.exe on Windows),
    one as a plain file and the other as a directory -- switching between
    them (e.g. a macOS onefile build after an older onedir one) leaves the
    other kind behind, which PyInstaller then refuses to overwrite."""
    import shutil
    for name in (BINARY_NAME, f"{BINARY_NAME}.exe"):
        old = dist / name
        if old.is_dir():
            shutil.rmtree(old)
        elif old.is_file():
            old.unlink()


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

    onefile = sys.platform == "darwin"
    build_dir = PROJECT_ROOT / "build" / "pyinstaller"
    args = [
        str(ENTRY_POINT),
        "--name", BINARY_NAME,
        "--onefile" if onefile else "--onedir",
        # `jfi --version` reads the installed package's metadata; without it
        # the binary printed "unknown".
        "--copy-metadata", "just-finish-it",
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
    remove_stale_build(PROJECT_ROOT / "dist")
    PyInstaller.__main__.run(args)

    output_path = (
        PROJECT_ROOT / "dist" / BINARY_NAME
        if onefile
        else PROJECT_ROOT / "dist" / BINARY_NAME / (BINARY_NAME + (".exe" if sys.platform == "win32" else ""))
    )
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
