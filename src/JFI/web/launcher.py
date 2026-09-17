"""`jfi-web` console script: a thin wrapper so the dashboard can be launched
the same way `jfi` itself is, without anyone needing to know or type the
package's internal path to dashboard.py."""

import os
import sys
from pathlib import Path

DEFAULT_PORT = 7777


def _skip_first_run_email_prompt() -> None:
    """
    Streamlit's very first launch on a machine blocks on an interactive
    "Welcome to Streamlit! ... Email:" activation prompt on stdin -- before
    it ever binds the server port. Non-interactively (no TTY on stdin, e.g.
    launched from a script or a background/nohup'd process, exactly how a
    JFI_WEB_BRIDGE-paired dashboard tends to get started) that prompt either
    hangs forever or the process dies silently on EOF, so `jfi-web` looks
    "not working" with zero error output and nothing listening on the port.

    Pre-creating ~/.streamlit/credentials.toml with an empty email is
    Streamlit's own documented way to skip this (used by every headless/CI
    Streamlit deployment) -- so do it here rather than making every JFI user
    discover and work around this themselves. Never touches an existing
    file: if the user already activated Streamlit for something else, their
    real credentials stay exactly as they are.
    """
    conf_path = Path.home() / ".streamlit" / "credentials.toml"
    if conf_path.exists():
        return
    try:
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        conf_path.write_text('[general]\nemail = ""\n', encoding="utf-8")
    except OSError:
        pass  # worst case, the user still sees Streamlit's own prompt


def _force_production_mode() -> None:
    """
    Streamlit decides its own "development mode" (global.developmentMode)
    by checking whether "site-packages" appears in its own config.py's
    __file__ path -- true for a normal pip/uv install, but false inside a
    frozen PyInstaller bundle, where that file is extracted to a bare
    sys._MEIPASS temp directory instead. Left alone, that flips
    developmentMode on, which then makes Streamlit refuse --server.port
    outright ("server.port does not work when global.developmentMode is
    true"), so the dashboard never binds the port at all -- while `jfi-web`
    run from a normal install (site-packages) never hits this.

    Forcing it off via Streamlit's own env-var override
    (ConfigOption.env_var: STREAMLIT_<SECTION>_<KEY>) fixes both cases
    identically. setdefault so an explicit override some caller already
    set (rare, but possible) still wins.
    """
    os.environ.setdefault("STREAMLIT_GLOBAL_DEVELOPMENT_MODE", "false")


def _dashboard_path() -> str:
    """
    Location of dashboard.py to hand to `streamlit run`.

    Inside a frozen PyInstaller onefile binary, `Path(__file__)` for a
    module compiled into the bundle's own archive isn't a real file on disk
    -- streamlit needs an actual path to exec as a script. build_binary
    adds dashboard.py as a raw --add-data entry instead (at JFI/web/
    dashboard.py), which PyInstaller's bootloader extracts to sys._MEIPASS
    at startup, so that's where it lives when frozen.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return str(Path(sys._MEIPASS) / "JFI" / "web" / "dashboard.py")
    return str(Path(__file__).with_name("dashboard.py"))


def main(extra_args: "list[str] | None" = None) -> None:
    """
    `extra_args` defaults to this process's own argv (the normal `jfi-web`
    CLI case). runner.py's `--internal-web-dashboard` re-invocation path
    passes `[]` explicitly instead -- that flag is consumed by runner.py's
    own arg parser and must never be forwarded to Streamlit's.
    """
    try:
        from streamlit.web import cli as stcli
    except ImportError:
        print(
            "Error: the web dashboard requires the 'streamlit' package, which is not "
            "installed. Install it with: pip install just-finish-it[web]",
            file=sys.stderr,
        )
        sys.exit(1)

    _skip_first_run_email_prompt()
    _force_production_mode()

    dashboard_path = _dashboard_path()
    if extra_args is None:
        extra_args = list(sys.argv[1:])

    # JFI_WEB_PORT picks the port jfi-web listens on (default 7777, not
    # Streamlit's own 8501 default) -- skipped if the caller already passed
    # --server.port themselves, so an explicit flag always wins.
    if not any(a == "--server.port" or a.startswith("--server.port=") for a in extra_args):
        port = os.environ.get("JFI_WEB_PORT", "").strip() or str(DEFAULT_PORT)
        extra_args += ["--server.port", port]

    sys.argv = ["streamlit", "run", dashboard_path, *extra_args]
    sys.exit(stcli.main())


if __name__ == "__main__":
    main()
