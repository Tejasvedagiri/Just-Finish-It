"""Builds a standalone `jfi` binary with PyInstaller — no Python or uv
needed on the machine that runs it.

Every binary carries the core agent, websockets (the fleet dashboard), the
`web` extra (Streamlit, for the dashboard) and Playwright's headless Chromium
(check_page, the browser tool, and the screenshot evidence of docs/old_new.md),
so nothing else is installed on the machine that runs it. Each other extra goes in only when
asked for, so a binary without Laya isn't several GB of torch:

    uv run build                       # core + web
    uv run build --laya --anthropic    # + Laya's judge and the Anthropic backend
    uv run build --all                 # every extra

Flags: --laya (`LAYA=1`: torch + transformers), --anthropic
(`LLM_BACKEND=anthropic`), --mysql / --postgres (`DB_BACKEND`). The web extra
and every requested one must be synced first (`uv sync --extra web --extra
laya --group dev`);
the build refuses rather than silently shipping a binary that lacks it. An
extra that is synced but not requested is excluded, so what's bundled is
exactly what was asked for, not whatever the venv happens to hold.
Output is always a single file, dist/jfi (dist/jfi.exe on Windows), that can
be copied anywhere on its own. A onedir build started faster (onefile unpacks
its 2.2 GB of torch on every launch: 18-35 s before `jfi --version` answered
on Windows), but its executable is useless without the `_internal/` directory
beside it, which is easy to leave behind when copying the binary out of
dist/jfi/. The user chose the single file on every platform.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# src/build_binary/__init__.py -> src/build_binary -> src -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENTRY_POINT = PROJECT_ROOT / "src" / "JFI" / "runner.py"
DASHBOARD_SRC = PROJECT_ROOT / "src" / "JFI" / "web" / "dashboard.py"
BINARY_NAME = "jfi"
# The user: "web must be included in all builds regardless."
ALWAYS = ("web",)
# Each optional extra (pyproject's [project.optional-dependencies]) -> the
# modules it installs. The first one is checked to see whether it's synced; all
# of them are excluded from a build that didn't ask for the extra.
EXTRAS = {
    "web": ("streamlit",),
    "laya": ("laya", "torch", "transformers"),
    "anthropic": ("anthropic",),
    "mysql": ("pymysql",),
    "postgres": ("psycopg", "psycopg_binary"),
}
EXTRA_HELP = {
    "laya": "Laya for the planner's judge (LAYA=1; torch + transformers, several GB)",
    "anthropic": "the Anthropic backend (LLM_BACKEND=anthropic)",
    "mysql": "the MySQL driver (DB_BACKEND=mysql)",
    "postgres": "the PostgreSQL driver (DB_BACKEND=postgres)",
}
# Laya's checkpoints are ModernBERT encoders; transformers imports a model's
# code by name when the checkpoint loads, which static analysis can't see.
LAYA_MODEL_PACKAGES = ("transformers.models.modernbert",)


def parse_args(argv: list[str] | None = None) -> tuple[list[str], bool]:
    """(the extras to bundle, in EXTRAS order: ALWAYS plus the ones asked for;
    whether to bundle the headless browser)."""
    # allow_abbrev=False: `--lay` must be refused, not silently read as --laya.
    parser = argparse.ArgumentParser(prog="uv run build", allow_abbrev=False,
                                     description="Build the standalone jfi binary (the web dashboard is always in).")
    for extra, help_text in EXTRA_HELP.items():
        parser.add_argument(f"--{extra}", action="store_true", help=f"bundle {help_text}")
    parser.add_argument("--all", action="store_true", help="bundle every extra above")
    parser.add_argument("--no-browser", action="store_true",
                        help="leave out Playwright's headless Chromium (~120 MB; the binary then needs "
                             "`playwright install chromium` where it runs for page checks and screenshots)")
    args = parser.parse_args(argv)
    return [extra for extra in EXTRAS if extra in ALWAYS or args.all or getattr(args, extra)], not args.no_browser


def _missing_extras(extras: list[str]) -> list[str]:
    import importlib.util
    return [extra for extra in extras if importlib.util.find_spec(EXTRAS[extra][0]) is None]


def extra_args(extras: list[str]) -> list[str]:
    """PyInstaller arguments: collect what each requested extra needs,
    exclude the rest."""
    args = []
    for extra, modules in EXTRAS.items():
        if extra not in extras:
            for module in modules:
                args += ["--exclude-module", module]
            continue
        if extra == "web":
            # runner._launch_web_dashboard re-invokes this same binary with a
            # hidden --internal-web-dashboard flag when no separate `jfi-web`
            # is on PATH (see JFI.web.launcher._dashboard_path).
            args += ["--collect-all", "streamlit", "--add-data", f"{DASHBOARD_SRC}:JFI/web"]
        elif extra == "laya":
            # torch and transformers come in through their own PyInstaller hooks.
            args += ["--collect-all", "laya"]
            for package in LAYA_MODEL_PACKAGES:
                args += ["--collect-submodules", package]
        else:
            # Imported lazily or by name (SQLAlchemy loads its DB driver from
            # the URL), which static analysis misses.
            for module in modules:
                args += ["--collect-all", module]
    return args


def remove_stale_build(dist: Path) -> None:
    """--onefile and --onedir both claim dist/jfi (dist/jfi.exe on Windows),
    one as a plain file and the other as a directory -- switching between
    them (a onefile build after an older onedir one) leaves the
    other kind behind, which PyInstaller then refuses to overwrite."""
    import shutil
    for name in (BINARY_NAME, f"{BINARY_NAME}.exe"):
        old = dist / name
        if old.is_dir():
            shutil.rmtree(old)
        elif old.is_file():
            old.unlink()


# Downloaded once by `playwright install` and reused by later builds.
BROWSERS_DIR = PROJECT_ROOT / "build" / "ms-playwright"
#: Where the binary finds them (JFI.tool.browser_tools._bundled_browsers).
BUNDLED_BROWSERS = "ms-playwright"


def install_browser(run=subprocess.run) -> str:
    """Playwright's headless Chromium, installed by the venv's own playwright
    so it's the exact build that playwright drives. Every browser JFI opens
    is headless, so the headless shell (~320 MB unpacked) is enough, not full
    Chromium (~600 MB). Returns "" or an error."""
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS_DIR)}
    proc = run([sys.executable, "-m", "playwright", "install", "--only-shell", "chromium"], env=env)
    if proc.returncode != 0:
        return (f"Downloading Playwright's Chromium into {BROWSERS_DIR} failed (exit {proc.returncode}). The "
                "binary bundles it so no install is needed where it runs; check the network (it comes from "
                "cdn.playwright.dev) and build again.")
    return ""


def main(argv: list[str] | None = None) -> None:
    extras, with_browser = parse_args(argv)
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

    missing = _missing_extras(extras)
    if missing:
        sync = " ".join(f"--extra {extra}" for extra in extras)
        print(
            f"The build needs the {', '.join(missing)} extra(s), which aren't installed here. Run\n"
            f"  uv sync {sync} --group dev\n"
            "(every extra in one command, plus any other you use: uv sync drops any extra it isn't told "
            "about), then build again.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    problem = install_browser() if with_browser else ""
    if problem:
        print(problem, file=sys.stderr)
        raise SystemExit(1)

    build_dir = PROJECT_ROOT / "build" / "pyinstaller"
    args = [
        str(ENTRY_POINT),
        "--name", BINARY_NAME,
        "--onefile",
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
        *(["--add-data", f"{BROWSERS_DIR}{os.pathsep}{BUNDLED_BROWSERS}"] if with_browser else []),
        *extra_args(extras),

        "--distpath", str(PROJECT_ROOT / "dist"),
        "--workpath", str(build_dir),
        "--specpath", str(build_dir),
    ]
    remove_stale_build(PROJECT_ROOT / "dist")
    PyInstaller.__main__.run(args)

    output_path = PROJECT_ROOT / "dist" / (BINARY_NAME + (".exe" if sys.platform == "win32" else ""))
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

    print(f"\nBuilt {output_path} with {', '.join(extras) if extras else 'no optional extras'}"
          + (" and the headless browser" if with_browser else ", without the browser"))


if __name__ == "__main__":
    main()
