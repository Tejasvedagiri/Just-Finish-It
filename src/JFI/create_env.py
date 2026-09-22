"""``uv run create-env`` -- a standalone system check + `.env_bk` setup helper.

Answers "am I ready to run JFI" without launching a real session: Python
version, whether `.env_bk` exists (creating it from `JFI_ENV_TEMPLATE` if not,
the same copy `./JFI` itself does on first run -- see the launcher script),
and a best-effort live reachability check against whatever OPENAI_URL is
currently configured, so a dead/misconfigured local LLM server is caught
here instead of several turns into a real session.

Stdlib only, deliberately -- this has to work before any dependency is
even installed, the same reasoning `plan_renumber.py` follows for staying
a standalone script rather than an LLM tool.
"""

import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

MIN_PYTHON = (3, 12)

# Project root: two levels up from this file (src/JFI/create_env.py -> repo root),
# same relationship the `./JFI` launcher's own `cd "$(dirname "$0")"` establishes.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = PROJECT_ROOT / ".env_bk"
ENV_TEMPLATE_PATH = PROJECT_ROOT / "JFI_ENV_TEMPLATE"

CHECK = "✅"
CROSS = "❌"
WARN = "⚠️ "


def _print_check(ok: bool, message: str) -> None:
    print(f"{CHECK if ok else CROSS} {message}")


def check_python_version() -> bool:
    ok = sys.version_info[:2] >= MIN_PYTHON
    have = f"{sys.version_info.major}.{sys.version_info.minor}"
    want = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    _print_check(ok, f"Python {have} (need >= {want})")
    return ok


def check_uv_available() -> bool:
    found = shutil.which("uv") is not None
    _print_check(found, "uv on PATH" if found else "uv not found on PATH (optional -- only needed for `uv run ...`)")
    return found


def _parse_env_file(path: Path) -> dict:
    """Minimal KEY=VALUE parser -- good enough to read back what's already
    configured without pulling in python-dotenv as a hard dependency here."""
    values = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def ensure_env_file() -> dict:
    """Creates `.env_bk` from `JFI_ENV_TEMPLATE` if it doesn't exist yet (same
    behavior `./JFI` itself has on first run), and returns whatever's
    actually configured in it afterward."""
    if not ENV_PATH.exists():
        if not ENV_TEMPLATE_PATH.exists():
            print(f"{CROSS} .env_bk is missing AND {ENV_TEMPLATE_PATH.name} is missing too -- cannot create one automatically.")
            return {}
        shutil.copy(ENV_TEMPLATE_PATH, ENV_PATH)
        print(f"{CHECK} Created .env_bk from {ENV_TEMPLATE_PATH.name} -- fill in OPENAI_URL/OPENAI_API_KEY/MODEL, then run this again.")
    else:
        print(f"{CHECK} .env_bk already exists ({ENV_PATH}).")
    return _parse_env_file(ENV_PATH)


def check_llm_reachable(openai_url: str, timeout: float = 4.0) -> bool:
    """Best-effort reachability check -- an OpenAI-compatible server's own
    /models endpoint, not a real chat completion (cheap, no API key parsing
    needed for most local servers, and doesn't burn tokens/context). Any
    failure just reports unreachable; this never raises, since a genuinely
    slow-to-start local server (still loading a model) shouldn't look like
    a hard error here."""
    url = openai_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
            _print_check(ok, f"{openai_url} responded (HTTP {resp.status})")
            return ok
    except urllib.error.HTTPError as e:
        # A server that's up but rejects this exact path/auth still proves
        # something is listening -- treat 4xx as "reachable", not a failure.
        ok = e.code < 500
        _print_check(ok, f"{openai_url} responded (HTTP {e.code})")
        return ok
    except Exception as e:  # noqa: BLE001 -- connection refused/timeout/DNS, all "not reachable"
        _print_check(False, f"{openai_url} not reachable ({e.__class__.__name__}: {e})")
        return False


def main() -> None:
    print(f"JFI system check -- {PROJECT_ROOT}\n")

    print("Python & tooling:")
    python_ok = check_python_version()
    check_uv_available()

    print("\nConfiguration (.env_bk):")
    values = ensure_env_file()
    openai_url = values.get("OPENAI_URL", "")
    model = values.get("MODEL", "")
    if openai_url:
        print(f"  OPENAI_URL = {openai_url}")
    if model:
        print(f"  MODEL      = {model}")

    llm_ok = True
    if openai_url:
        print("\nLLM server reachability:")
        llm_ok = check_llm_reachable(openai_url)
    else:
        print(f"\n{WARN}OPENAI_URL not set in .env_bk -- skipping the reachability check.")

    print()
    if python_ok and values and openai_url and llm_ok:
        print(f"{CHECK} Looks ready. Run ./JFI (or `jfi` once installed) to start a session.")
    else:
        print(f"{WARN}Not fully ready yet -- fix whichever check above failed, then run `uv run create-env` again.")


if __name__ == "__main__":
    main()
