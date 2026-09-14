"""`jfi-web` console script: a thin wrapper so the dashboard can be launched
the same way `jfi` itself is, without anyone needing to know or type the
package's internal path to dashboard.py."""

import os
import sys
from pathlib import Path

DEFAULT_PORT = 7777


def main() -> None:
    try:
        from streamlit.web import cli as stcli
    except ImportError:
        print(
            "Error: the web dashboard requires the 'streamlit' package, which is not "
            "installed. Install it with: pip install just-finish-it[web]",
            file=sys.stderr,
        )
        sys.exit(1)

    dashboard_path = str(Path(__file__).with_name("dashboard.py"))
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
