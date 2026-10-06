"""``uv run create-env`` -- a standalone system check + `.env` setup helper.

Answers "am I ready to run JFI" without launching a real session: Python
version, whether `.env` exists (creating it from `JFI_ENV_TEMPLATE` if not,
the same copy `./JFI` itself does on first run -- see the launcher script),
and a best-effort live reachability check against whichever backend
`LLM_BACKEND` actually selects at runtime (see `JFI.llm.backend_select`):
an OpenAI-compatible `OPENAI_URL` (local server, ollama/llamacpp/lmstudio
defaults, or hosted OpenAI) for every backend except `anthropic`/`claude`,
which talks to Claude's own Messages API via `ANTHROPIC_API_KEY` instead
and needs no `OPENAI_URL` at all. Checking the wrong pair of settings for
the backend actually configured is exactly the bug this module used to
have -- see `backend_select.py` for the same two-family split at runtime.

When run in a real terminal (`sys.stdin.isatty()`) this always runs an
interactive setup wizard (a run that isn't interactive only checks): pick a backend, enter its
key, fetch and pick a model from that server/API's own `/models` list
(never hand-typed blind), and get a best-effort `CONTEXT_SIZE` default
guessed from the model's own name -- always shown before being accepted,
never written silently. See `run_setup_wizard`.

Stdlib only, deliberately -- this has to work before any dependency is
even installed. `JFI.llm.backend_select` and
`JFI.llm.base_llm_stream` are safe to import here despite that: both are
pure-stdlib themselves (the `anthropic` package is only imported lazily,
inside `AnthropicStream.__init__`, never at module import time).
"""

import getpass
import importlib.util
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from JFI import create_env_detect as detect
from JFI.llm.backend_select import _KNOWN_BACKENDS, _LLAMACPP_DEFAULTS, _LMSTUDIO_DEFAULTS, _OLLAMA_DEFAULTS

MIN_PYTHON = (3, 12)

# Project root: two levels up from this file (src/JFI/create_env.py -> repo root),
# same relationship the `./JFI` launcher's own `cd "$(dirname "$0")"` establishes.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
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
    """Creates `.env` from `JFI_ENV_TEMPLATE` if it doesn't exist yet (same
    behavior `./JFI` itself has on first run), and returns whatever's
    actually configured in it afterward."""
    if not ENV_PATH.exists():
        if not ENV_TEMPLATE_PATH.exists():
            print(f"{CROSS} .env is missing AND {ENV_TEMPLATE_PATH.name} is missing too -- cannot create one automatically.")
            return {}
        shutil.copy(ENV_TEMPLATE_PATH, ENV_PATH)
        print(f"{CHECK} Created .env from {ENV_TEMPLATE_PATH.name}.")
    else:
        print(f"{CHECK} .env already exists ({ENV_PATH}).")
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


def check_anthropic_extra_installed() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        _print_check(False, "`anthropic` package not installed -- run `uv sync --extra anthropic`")
        return False
    _print_check(True, "`anthropic` package installed")
    return True


def check_anthropic_reachable(api_key: str, timeout: float = 4.0) -> bool:
    """Best-effort reachability + auth check against Claude's own API --
    a raw HTTPS request rather than the `anthropic` package, same reasoning
    as check_llm_reachable: this needs to work even before `uv sync --extra
    anthropic` has been run. Unlike check_llm_reachable's local server case,
    a 401 here is a real, specific failure (the key itself is wrong) rather
    than just "something rejected this path" -- worth calling out
    separately instead of folding it into the generic 4xx-is-reachable
    bucket."""
    url = "https://api.anthropic.com/v1/models"
    req = urllib.request.Request(url, headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
            _print_check(ok, f"Anthropic API responded (HTTP {resp.status})")
            return ok
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _print_check(False, f"Anthropic API rejected ANTHROPIC_API_KEY (HTTP {e.code})")
            return False
        ok = e.code < 500
        _print_check(ok, f"Anthropic API responded (HTTP {e.code})")
        return ok
    except Exception as e:  # noqa: BLE001 -- connection refused/timeout/DNS, all "not reachable"
        _print_check(False, f"Anthropic API not reachable ({e.__class__.__name__}: {e})")
        return False


def _fetch_model_ids(url: str, headers: dict, timeout: float = 6.0) -> list | None:
    """GET `url` and pull `.data[].id` out of the response -- the one shape
    both an OpenAI-compatible `/models` endpoint and Anthropic's own
    `/v1/models` share, despite their auth headers differing. Returns None
    on any failure (bad key, server down, unexpected shape, ...) so callers
    can fall back to asking the user to type a model id by hand."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 -- any failure here just means "can't list models", not a hard error
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    ids = [str(item["id"]) for item in data if isinstance(item, dict) and item.get("id")]
    return ids or None


def fetch_openai_compatible_models(base_url: str, api_key: str) -> list | None:
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return _fetch_model_ids(url, headers)


def fetch_anthropic_models(api_key: str) -> list | None:
    return _fetch_model_ids(
        "https://api.anthropic.com/v1/models",
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )


# Best-effort CONTEXT_SIZE hints for hosted models, checked as substrings of
# the (lowercased) model id -- order matters, first match wins, so more
# specific ids (gpt-4.1, gpt-4o) are listed before the bare "gpt-4" they'd
# otherwise also match.
_HOSTED_CONTEXT_HINTS = [
    ("claude", 200000),
    ("gpt-4.1", 1000000),
    ("gpt-4o", 128000),
    ("gpt-4-turbo", 128000),
    ("o1", 200000),
    ("o3", 200000),
    ("gpt-3.5", 16385),
    ("gpt-4", 8192),
]

# Matches a parameter-count suffix like the "8b"/"31b"/"70b" in
# "qwen3:8b"/"gemma-4:31b"/"llama-3.1-70b-instruct" -- \b on both sides so
# it doesn't false-match the "3b" inside a MoE tag like "35b-a3b"'s "a3b".
_LOCAL_PARAM_RE = re.compile(r"\b(\d+(?:\.\d+)?)b\b", re.IGNORECASE)


def guess_context_size(backend: str, model: str, default: int = 32768) -> int:
    """Best-effort CONTEXT_SIZE default from the model's own name alone --
    never authoritative (no server/API here reliably exposes its real
    context window through /models), always shown to the user as an
    editable suggestion by the setup wizard, never written silently."""
    if not model:
        return default
    lowered = model.lower()
    if backend in ("anthropic", "claude", "openai"):
        for needle, size in _HOSTED_CONTEXT_HINTS:
            if needle in lowered:
                return size
        return default
    matches = _LOCAL_PARAM_RE.findall(lowered)
    if not matches:
        return default
    params_billion = max(float(m) for m in matches)
    if params_billion <= 9:
        return 8192
    if params_billion <= 20:
        return 16384
    if params_billion <= 40:
        return 32768
    return 65536


def _write_env_values(path: Path, updates: dict) -> None:
    """Sets each KEY=value in `updates` inside `path`, in place: replaces an
    existing (possibly commented-out, e.g. a template's `# MODEL=...`)
    `KEY=` line so hand-written comments/ordering survive, and only appends
    a fresh line for a key that isn't in the file at all."""
    remaining = dict(updates)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    out = []
    for line in lines:
        stripped = line.strip()
        bare = stripped.lstrip("#").strip() if stripped.startswith("#") else stripped
        key = bare.split("=", 1)[0].strip() if "=" in bare else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}\n")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}\n")
    path.write_text("".join(out), encoding="utf-8")


_BACKEND_MENU = [
    ("ollama", "Ollama (local, http://127.0.0.1:11434)"),
    ("llamacpp", "llama.cpp server (local, http://127.0.0.1:8080)"),
    ("lmstudio", "LM Studio (local, http://127.0.0.1:1234)"),
    ("openai", "OpenAI (hosted)"),
    ("anthropic", "Anthropic / Claude (hosted)"),
    ("custom", "Custom OpenAI-compatible URL"),
]

_BACKEND_DEFAULTS = {
    "ollama": _OLLAMA_DEFAULTS,
    "llamacpp": _LLAMACPP_DEFAULTS,
    "lmstudio": _LMSTUDIO_DEFAULTS,
    "openai": {"OPENAI_URL": "https://api.openai.com/v1", "OPENAI_API_KEY": ""},
}


def _is_configured(values: dict, backend: str) -> bool:
    """Whether .env already has everything its own LLM_BACKEND needs --
    used to decide whether the setup wizard should run at all, so a
    already-working config isn't re-prompted on every single invocation."""
    if backend in ("anthropic", "claude"):
        return bool(values.get("ANTHROPIC_API_KEY")) and bool(values.get("MODEL"))
    if backend in ("ollama", "llamacpp", "llama.cpp", "llama-cpp", "lmstudio", "lm-studio", "lm studio"):
        return bool(values.get("MODEL"))
    return bool(values.get("OPENAI_URL")) and bool(values.get("OPENAI_API_KEY")) and bool(values.get("MODEL"))


def _prompt_choice(prompt: str, options: list, default_index: int = 0) -> str:
    print(f"\n{prompt}")
    for i, (key, label) in enumerate(options, 1):
        marker = "  <- default" if i - 1 == default_index else ""
        print(f"  {i}. {label}{marker}")
    raw = input(f"> [{default_index + 1}] ").strip()
    if not raw:
        return options[default_index][0]
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return options[idx][0]
    except ValueError:
        pass
    for key, _ in options:
        if raw.lower() == key.lower():
            return key
    print(f"{WARN}Didn't understand {raw!r} -- using the default.")
    return options[default_index][0]


def _prompt_text(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    return raw or default


def _prompt_secret(prompt: str, default: str = "") -> str:
    hint = (default[:4] + "...") if len(default) > 8 else ("set" if default else "")
    suffix = f" [keep existing, {hint}]" if default else ""
    raw = getpass.getpass(f"{prompt}{suffix}: ").strip()
    return raw or default


def _prompt_model_choice(fetch, fallback_prompt: str, notes: dict | None = None) -> str:
    """`notes` (model id -> "loaded, 38,144-token context, 21.8 GB") puts the
    loaded models first and says what's loaded and how big each one is."""
    print("\nFetching available models...")
    ids = fetch()
    if not ids:
        print(f"{WARN}Couldn't list models automatically -- enter the model id yourself.")
        return input(f"{fallback_prompt}: ").strip()
    notes = notes or {}
    ids = sorted(ids, key=lambda i: not notes.get(i, "").startswith("loaded"))
    shown = ids[:40]
    for i, model_id in enumerate(shown, 1):
        print(f"  {i}. {model_id}" + (f"  ({notes[model_id]})" if model_id in notes else ""))
    if len(ids) > 40:
        print(f"  ... and {len(ids) - 40} more -- type the model id directly if yours isn't shown above.")
    raw = input("> [1] (or type a model id): ").strip()
    if not raw:
        return shown[0]
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(shown):
            return shown[idx]
    except ValueError:
        pass
    return raw


def _prompt_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{prompt} {suffix} ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


PHASE_PREFIXES = ("PLANNER", "IMP", "REVIEWER", "CLEANUP")


# Detection goes through these so tests can replace them -- the wizard's tests
# must never probe a real GPU or model server.
def _detect_system() -> "detect.System":
    return detect.detect_system()


def _detect_models(backend: str, url: str, key: str):
    return detect.detect_models(backend, url, key)


def _measure_speed(url: str, key: str, model: str) -> "detect.Speed":
    print(f"\nTiming one short request to {model} (and checking it can call tools)...")
    return detect.measure_speed(url, key, model)


def _laya_installed() -> bool:
    return importlib.util.find_spec("laya") is not None


def _print_system(system: "detect.System") -> None:
    print("\n--- Detected ---")
    ram = (f"{system.ram_total_gb:.1f} GB RAM ({system.ram_free_gb:.1f} GB free)"
           if system.ram_total_gb and system.ram_free_gb is not None else "RAM unknown")
    print(f"  {system.os_name}, {system.cores or '?'} cores, {ram}")
    gpu = system.gpu
    if gpu and gpu.kind == "apple":
        print(f"  {gpu.name}: shared memory, ~{gpu.total_gb:.0f} GB usable by the GPU" if gpu.total_gb
              else f"  {gpu.name}: shared memory")
    elif gpu:
        free = f", {gpu.free_gb:.1f} GB free" if gpu.free_gb is not None else ""
        print(f"  {gpu.name}: {gpu.total_gb:.1f} GB VRAM{free}" if gpu.total_gb else f"  {gpu.name}")
    else:
        print("  no GPU detected: local models run from RAM")


def _ask(values: dict, updates: dict, suggestion: "detect.Suggestion", key: str | None = None) -> str:
    """A computed value as the default, with why -- and the .env value when
    it differs, since an old value is how a stale setting survives."""
    key = key or suggestion.name
    current = (values.get(key) or "").strip()
    note = f" (currently {current} in .env)" if current and current != suggestion.value else ""
    print(f"   why: {suggestion.reason}{note}")
    updates[key] = _prompt_text(key, suggestion.value)
    return updates[key]


def _model_notes(models) -> dict:
    notes = {}
    for m in models or []:
        bits = []
        if m.loaded:
            bits.append(f"loaded, {m.loaded_context:,}-token context" if m.loaded_context else "loaded")
        if m.size_gb:
            bits.append(f"{m.size_gb:.1f} GB")
        if bits:
            notes[m.id] = ", ".join(bits)
    return notes


def _configure_llm(values: dict, updates: dict, prefix: str = "",
                   system: "detect.System | None" = None) -> tuple:
    """Runs the backend/key/model/CONTEXT_SIZE sub-flow, writing `{prefix}_
    {KEY}` into `updates` for a phase override (e.g. PLANNER_MODEL) or the
    bare `{KEY}` for the shared/default settings when `prefix` is empty --
    exactly the naming `phase_env` resolves at runtime. CONTEXT_SIZE is
    computed from what the server reports (see create_env_detect). Returns
    (backend, model, {name: Suggestion}) -- the other computed values, for
    the caller's later prompts."""

    def key(name: str) -> str:
        return f"{prefix}_{name}" if prefix else name

    label = f"the {prefix} phase" if prefix else "the shared/default settings"
    backend = _prompt_choice(f"Which LLM backend for {label}?", _BACKEND_MENU, default_index=0)

    # Whatever's already in .env is only a sensible prefill for {key}_URL/
    # {key}_API_KEY if it was written for THIS SAME backend -- otherwise it's
    # a leftover from a different backend (e.g. Ollama's :11434 URL) and
    # would silently override the one just picked (e.g. LM Studio's :1234)
    # if carried forward.
    previous_backend = (values.get(key("LLM_BACKEND")) or "").strip().lower()
    if previous_backend in ("", "openai"):
        previous_backend = "openai"
    same_backend_as_before = backend == previous_backend

    if backend == "anthropic":
        updates[key("LLM_BACKEND")] = "anthropic"
        prior_key = values.get(key("ANTHROPIC_API_KEY"), "") if same_backend_as_before else ""
        api_key = _prompt_secret(key("ANTHROPIC_API_KEY"), prior_key)
        updates[key("ANTHROPIC_API_KEY")] = api_key
        model = _prompt_model_choice(
            lambda: fetch_anthropic_models(api_key) if api_key else None,
            "Claude model id (e.g. claude-sonnet-5)",
        )
        openai_url, models = "", None
    else:
        defaults = _BACKEND_DEFAULTS.get(backend, {})
        if backend == "custom":
            openai_url = _prompt_text(key("OPENAI_URL"), values.get(key("OPENAI_URL"), ""))
            default_key = values.get(key("OPENAI_API_KEY"), "")
        else:
            updates[key("LLM_BACKEND")] = backend
            prior_url = values.get(key("OPENAI_URL"), "") if same_backend_as_before else ""
            openai_url = _prompt_text(key("OPENAI_URL"), prior_url or defaults.get("OPENAI_URL", ""))
            prior_key = values.get(key("OPENAI_API_KEY"), "") if same_backend_as_before else ""
            default_key = prior_key or defaults.get("OPENAI_API_KEY", "")
        if backend in ("ollama", "llamacpp", "lmstudio"):
            api_key = _prompt_text(f"{key('OPENAI_API_KEY')} (usually ignored by local servers)", default_key)
        else:
            api_key = _prompt_secret(key("OPENAI_API_KEY"), default_key)
        updates[key("OPENAI_URL")] = openai_url
        updates[key("OPENAI_API_KEY")] = api_key
        models = _detect_models(backend, openai_url, api_key) if openai_url else None
        model = _prompt_model_choice(
            lambda: fetch_openai_compatible_models(openai_url, api_key) if openai_url else None,
            "Model id",
            notes=_model_notes(models),
        )

    updates[key("MODEL")] = model
    hosted = backend in ("openai", "anthropic", "claude")
    speed = _measure_speed(openai_url, api_key, model) if openai_url and model else None
    if speed:
        if speed.tokens_per_second:
            print(f"   {speed.tokens_per_second:.0f} tokens/s")
        if speed.tool_calls_work is False:
            print(f"{CROSS} {model} answered without calling the tool -- JFI needs a model that can call tools.")
        elif speed.note:
            print(f"{WARN}{speed.note}")
    found = detect.Detected(system=system, model=detect.find_model(models, model), speed=speed, hosted=hosted,
                            laya_installed=_laya_installed())
    suggestions = {s.name: s for s in detect.compute(model, found, lambda m: guess_context_size(backend, m))}
    for warning in found.warnings:
        print(f"{WARN}{warning}")
    context = _ask(values, updates, suggestions["CONTEXT_SIZE"], key=key("CONTEXT_SIZE"))
    # PARALLEL_LLM has no per-phase form: the shared server's answer sizes it,
    # and the planner caps it again per role's own server at runtime.
    if not prefix:
        try:
            context_size = int(context)
        except ValueError:
            context_size = int(suggestions["CONTEXT_SIZE"].value)
        suggestions["PARALLEL_LLM"] = detect.suggest_parallel(backend, found, context_size)
    return backend, model, suggestions


# name -> (default value shown/written, prompt suffix)
_TUNING_KNOBS = [
    ("TEMPERATURE", "0.7", ""),
    ("FREQUENCY_PENALTY", "0.0", ""),
    ("CONTEXT_COMPRESSION_RATIO", "0.7", ""),
    ("STREAM_OUTPUT_CAP", "10000", ""),
    ("REASONING_OUTPUT_CAP", "3000", ""),
    ("LLM_REQUEST_TIMEOUT", "120", " (seconds)"),
]


# The episode pipeline's knobs, with the code's own defaults (budget.py,
# result_cap.py, planner/loop.py, planner/nodes.py, imp/dev.py). The user:
# "There is a lot of env variables not asked."
_EPISODE_KNOBS = [
    ("MAX_EPISODE_TURNS", "25", " (model replies inside ONE episode before it's stopped)"),
    ("TOOL_RESULT_MAX_TOKENS", "3000", " (longest single tool result; the rest is cut with a hint)"),
]
_PLANNER_KNOBS = [
    ("PLANNER_ITEM_MAX_CHARS", "200", " (longest plan-node description, in characters)"),
    ("PLANNER_REDO_CAP", "2", " (rewrites of one node before it's accepted as-is)"),
    ("PLANNER_ESCALATION_CAP", "1", " (times a node may send its parent back to be redone)"),
    ("MAX_PLANNER_EPISODES", "200",
     " (safety stop: most planner episodes in a whole planning round -- calc used 8, "
     "a 97-node app 47; not a size to tune)"),
    ("MAX_DEV_ATTEMPTS", "3", " (Dev episodes on one leaf before it's split)"),
]
_FLAGS = [
    ("AUTO_APPROVE_COMMANDS", "Run every shell command without asking (AUTO_APPROVE_COMMANDS)?"),
    ("REVIEW_LOOP_APPROVAL", "Ask before each fix iteration after a failed review (REVIEW_LOOP_APPROVAL)?"),
    ("LOG_LLM_CALL_DEBUG", "Log every LLM request/response to .jfi/llm_debug.jsonl (LOG_LLM_CALL_DEBUG)?"),
    ("SHOW_STREAM_PROMPTS", "Print every prompt sent to the LLM (SHOW_STREAM_PROMPTS)?"),
    ("EVIDENCE_REVIEW", "Wait for you to accept the ground-truth evidence in evidences/ before building "
                        "(EVIDENCE_REVIEW)?"),
]


def _master_ws_url(answer: str) -> str:
    """A full ws:// URL from a URL, `host:port` or a bare port. Observed: the
    user answered `8765`, .env got MASTER_WS_URL=8765, and the session could
    never reach the fleet master."""
    answer = answer.strip()
    if "://" in answer:
        return answer
    host, _, port = answer.rpartition(":") if ":" in answer else ("127.0.0.1", "", answer)
    return f"ws://{host or '127.0.0.1'}:{port}/report"


def _is_on(values: dict, name: str) -> bool:
    return values.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _flag(values: dict, updates: dict, name: str, prompt: str, default: bool | None = None) -> bool:
    """Writes 1 or 0 -- 0 overwrites an old 1, so answering no turns it off."""
    on = _prompt_yes_no(prompt, default=_is_on(values, name) if default is None else default)
    updates[name] = "1" if on else "0"
    return on


def _effective_episode_budget(values: dict, updates: dict) -> int:
    """What budget.episode_token_budget will use: CONTEXT_SIZE x
    CONTEXT_COMPRESSION_RATIO."""
    def number(name, default, kind):
        try:
            return kind(updates.get(name) or values.get(name) or default)
        except ValueError:
            return kind(default)
    return max(1_000, int(number("CONTEXT_SIZE", "32768", int) * number("CONTEXT_COMPRESSION_RATIO", "0.7", float)))


def _configure_pipeline(values: dict, updates: dict, sizing: dict | None = None) -> None:
    sizing = sizing or {}
    print("\n--- Episodes (every LLM call is one short episode) ---")
    for name, default, suffix in _EPISODE_KNOBS:
        if name in sizing:
            _ask(values, updates, sizing[name])
        else:
            updates[name] = _prompt_text(f"{name}{suffix}", values.get(name, default))
    print(f"   -> each episode gets {_effective_episode_budget(values, updates):,} tokens "
          "(CONTEXT_SIZE x CONTEXT_COMPRESSION_RATIO).")

    print("\n--- Planner and Dev ---")
    for name, default, suffix in _PLANNER_KNOBS:
        updates[name] = _prompt_text(f"{name}{suffix}", values.get(name, default))
    print("   PARALLEL_LLM: how many Lead / Task breakdown episodes run at once (1-10).")
    if "PARALLEL_LLM" in sizing:
        _ask(values, updates, sizing["PARALLEL_LLM"])
    else:
        updates["PARALLEL_LLM"] = _prompt_text("PARALLEL_LLM", values.get("PARALLEL_LLM", "1"))

    print("\n--- Planner judge ---")
    if not _laya_installed():
        print("   (the laya extra isn't installed: LAYA=1 does nothing until `uv sync --extra laya`)")
    if _flag(values, updates, "LAYA",
             "Add Laya's score to the rule judge (LAYA; from source needs `uv sync --extra laya`)?"):
        if "LAYA_DEVICE" in sizing:
            _ask(values, updates, sizing["LAYA_DEVICE"])
        else:
            updates["LAYA_DEVICE"] = _prompt_text("LAYA_DEVICE (cpu or cuda)", values.get("LAYA_DEVICE", "cpu"))
        updates["LAYA_MIN_CONFIDENCE"] = _prompt_text(
            "LAYA_MIN_CONFIDENCE (how sure Laya must be to send a disagreement to the LLM)",
            values.get("LAYA_MIN_CONFIDENCE", "0.75"))
        unload = sizing.get("UNLOAD_LLM_BEFORE_LAYA")
        if unload:
            print(f"   why: {unload.reason}")
        _flag(values, updates, "UNLOAD_LLM_BEFORE_LAYA",
              "Unload the LM Studio model while Laya runs (UNLOAD_LLM_BEFORE_LAYA; low-memory machines)?",
              default=unload.value == "1" if unload else None)

    print("\n--- Approvals and debugging ---")
    for name, prompt in _FLAGS:
        _flag(values, updates, name, prompt)
    flaresolverr = _prompt_text("FLARESOLVERR_URL (blank = none; for fetching Cloudflare-protected pages)",
                                values.get("FLARESOLVERR_URL", ""))
    if flaresolverr:
        updates["FLARESOLVERR_URL"] = flaresolverr


def _next_versioned_env_path() -> Path:
    """The first `.env_v{N}` (N=1, 2, 3, ...) that doesn't exist yet next to
    ENV_PATH -- used so a wizard save never overwrites a `.env` that was
    already there before this run started (see `main`'s `env_existed_before`).
    JFI's own runtime (`load_dotenv(find_dotenv())`) only ever loads the
    literal `.env`, so a versioned file is a saved draft the user promotes
    (rename/copy over `.env`) when they want it live, never auto-picked-up."""
    n = 1
    while True:
        candidate = ENV_PATH.parent / f"{ENV_PATH.name}_v{n}"
        if not candidate.exists():
            return candidate
        n += 1


def run_setup_wizard(values: dict, write_target: Path | None = None) -> dict:
    """Interactively fills in .env: the shared LLM backend/key/model/
    CONTEXT_SIZE, every tuning knob (TEMPERATURE,
    FREQUENCY_PENALTY, CONTEXT_COMPRESSION_RATIO, STREAM_OUTPUT_CAP,
    REASONING_OUTPUT_CAP, LLM_REQUEST_TIMEOUT, THEME), the web dashboard
    bridge (JFI_WEB_BRIDGE/JFI_WEB_PORT), the fleet dashboard connection
    (MASTER_WS_URL), the episode/planner/Dev knobs, the judge (LAYA and its
    settings), the approval and debug flags, and -- opt-in, per phase -- a completely separate
    backend/key/model/CONTEXT_SIZE for PLANNER/IMP/REVIEWER/CLEANUP. See the module docstring. Writes to `write_target` (defaults
    to ENV_PATH itself); `main` passes a versioned path instead whenever
    `.env` already existed before this run, so a save here never
    overwrites a config that predates it -- see `_next_versioned_env_path`.
    Returns that file's contents re-read from disk after writing, so
    callers see exactly what ended up there."""
    print("\nLet's set up .env -- press Enter on any prompt to accept the default shown.")

    system = _detect_system()
    _print_system(system)

    print("\n--- Shared / default LLM settings ---")
    updates = {}
    _, _, sizing = _configure_llm(values, updates, prefix="", system=system)

    print("\n--- Tuning ---")
    for name, default, suffix in _TUNING_KNOBS:
        if name in sizing:
            _ask(values, updates, sizing[name])
        else:
            updates[name] = _prompt_text(f"{name}{suffix}", values.get(name, default))
    theme = _prompt_text("THEME (blank = auto-detect from your terminal)", values.get("THEME", ""))
    if theme:
        updates["THEME"] = theme

    print("\n--- Web dashboard bridge ---")
    if _prompt_yes_no(
        "Enable JFI_WEB_BRIDGE (mirror live status to a browser dashboard via jfi-web)?",
        default=values.get("JFI_WEB_BRIDGE", "").strip().lower() in ("1", "true", "yes", "on"),
    ):
        updates["JFI_WEB_BRIDGE"] = "1"
        updates["JFI_WEB_PORT"] = _prompt_text("JFI_WEB_PORT", values.get("JFI_WEB_PORT", "7777"))

    print("\n--- Fleet dashboard (jfi-master) ---")
    if _prompt_yes_no(
        "Report this session to a fleet dashboard (MASTER_WS_URL, e.g. jfi-master running elsewhere)?",
        default=bool(values.get("MASTER_WS_URL", "").strip()),
    ):
        updates["MASTER_WS_URL"] = _master_ws_url(_prompt_text(
            "MASTER_WS_URL (a URL, host:port or just the port)",
            values.get("MASTER_WS_URL", "") or "ws://127.0.0.1:8765/report",
        ))

    _configure_pipeline(values, updates, sizing)

    print("\n--- Per-phase overrides (optional) ---")
    if _prompt_yes_no(
        "Configure a completely separate backend/model for any phase (PLANNER/IMP/REVIEWER/CLEANUP)?",
        default=False,
    ):
        for phase in PHASE_PREFIXES:
            if _prompt_yes_no(f"Override the {phase} phase's backend/model?", default=False):
                print(f"\n--- {phase} phase ---")
                _configure_llm(values, updates, prefix=phase, system=system)

    write_target = write_target or ENV_PATH
    _write_env_values(write_target, updates)
    print(f"\n{CHECK} Saved to {write_target}")
    if write_target != ENV_PATH:
        print(
            f"{WARN}{ENV_PATH.name} already existed, so this was saved as a new version instead of "
            f"overwriting it. JFI's own runtime only ever loads {ENV_PATH.name} -- rename or copy "
            f"{write_target.name} over it when you want these settings live."
        )
    return _parse_env_file(write_target)


def _check_anthropic_backend(values: dict, backend: str, model: str) -> bool:
    print(f"  LLM_BACKEND = {backend}")
    if model:
        print(f"  MODEL       = {model}")
    api_key = values.get("ANTHROPIC_API_KEY", "")

    extra_ok = check_anthropic_extra_installed()
    if not model:
        print(f"{CROSS} MODEL not set in .env (needs a Claude model id, e.g. claude-sonnet-5)")
    if not api_key:
        print(f"{CROSS} ANTHROPIC_API_KEY not set in .env")

    print("\nLLM server reachability:")
    if api_key:
        llm_ok = check_anthropic_reachable(api_key)
    else:
        _print_check(False, "skipped (no ANTHROPIC_API_KEY)")
        llm_ok = False

    return extra_ok and bool(model) and bool(api_key) and llm_ok


def _check_openai_compatible_backend(values: dict, backend: str) -> bool:
    openai_url = values.get("OPENAI_URL", "")
    openai_key = values.get("OPENAI_API_KEY", "")
    model = values.get("MODEL", "")

    # Mirrors backend_select.make_llm_stream: ollama/llamacpp fill in their
    # usual localhost defaults at runtime whenever OPENAI_URL/OPENAI_API_KEY
    # aren't already set -- checking the bare .env values here would
    # otherwise flag a perfectly valid ollama/llamacpp config as "not set".
    defaulted = False
    if backend == "ollama":
        defaulted = not openai_url and not openai_key
        openai_url = openai_url or _OLLAMA_DEFAULTS["OPENAI_URL"]
        openai_key = openai_key or _OLLAMA_DEFAULTS["OPENAI_API_KEY"]
    elif backend in ("llamacpp", "llama.cpp", "llama-cpp"):
        defaulted = not openai_url and not openai_key
        openai_url = openai_url or _LLAMACPP_DEFAULTS["OPENAI_URL"]
        openai_key = openai_key or _LLAMACPP_DEFAULTS["OPENAI_API_KEY"]
    elif backend in ("lmstudio", "lm-studio", "lm studio"):
        defaulted = not openai_url and not openai_key
        openai_url = openai_url or _LMSTUDIO_DEFAULTS["OPENAI_URL"]
        openai_key = openai_key or _LMSTUDIO_DEFAULTS["OPENAI_API_KEY"]
    elif backend and backend not in _KNOWN_BACKENDS:
        print(f"{WARN}Unrecognized LLM_BACKEND={backend!r} -- JFI falls back to plain OpenAI-compatible at runtime.")

    if openai_url:
        suffix = f" (LLM_BACKEND={backend} default)" if defaulted else ""
        print(f"  OPENAI_URL = {openai_url}{suffix}")
    if model:
        print(f"  MODEL      = {model}")

    if openai_url:
        print("\nLLM server reachability:")
        llm_ok = check_llm_reachable(openai_url)
    else:
        print(f"\n{WARN}OPENAI_URL not set in .env -- skipping the reachability check.")
        llm_ok = False

    return bool(openai_url) and bool(openai_key) and bool(model) and llm_ok


def main() -> None:
    print(f"JFI system check -- {PROJECT_ROOT}\n")

    print("Python & tooling:")
    python_ok = check_python_version()
    check_uv_available()

    print("\nConfiguration (.env):")
    # Whether `.env` was already sitting there BEFORE this run touched anything --
    # decides whether a wizard save below writes .env itself (first-ever setup) or
    # a new .env_v{N} that leaves that pre-existing file completely alone.
    env_existed_before = ENV_PATH.exists()
    values = ensure_env_file()
    backend = values.get("LLM_BACKEND", "").strip().lower()

    # Interactive runs always open the wizard (the user's call: it used to be
    # skipped once .env was configured, unless --setup was passed).
    wizard_write_target = None
    if sys.stdin.isatty():
        wizard_write_target = ENV_PATH if not env_existed_before else _next_versioned_env_path()
        try:
            values = run_setup_wizard(values, wizard_write_target)
        except (EOFError, KeyboardInterrupt):
            # Observed with redirected input in Git Bash, which still reports a
            # terminal: the first prompt raised EOFError with a traceback. Nothing
            # is written until the wizard's last step, so stopping here is safe.
            print(f"\n{WARN}Setup stopped before it finished; nothing was written. "
                  "Run `uv run create-env` in a terminal to set up .env.")
            raise SystemExit(1)
        backend = values.get("LLM_BACKEND", "").strip().lower()
    elif not _is_configured(values, backend):
        print(f"{WARN}Non-interactive session -- skipping the setup wizard, just checking what's already in .env.")

    model = values.get("MODEL", "")
    url = values.get("OPENAI_URL", "") or _BACKEND_DEFAULTS.get(backend, {}).get("OPENAI_URL", "")
    mismatch = None
    if url and model and backend not in ("anthropic", "claude", "openai"):
        loaded = detect.find_model(_detect_models(backend, url, values.get("OPENAI_API_KEY", "")), model)
        mismatch = detect.context_mismatch(values.get("CONTEXT_SIZE"), loaded)
        if mismatch:
            print(f"{CROSS} {mismatch}")
        too_parallel = detect.parallel_shortfall(values.get("PARALLEL_LLM"), values.get("CONTEXT_SIZE"), backend,
                                                 loaded)
        if too_parallel:
            print(f"{WARN}{too_parallel}")

    if not values:
        config_ok = False
    elif backend in ("anthropic", "claude"):
        config_ok = _check_anthropic_backend(values, backend, model)
    else:
        config_ok = _check_openai_compatible_backend(values, backend)

    print()
    # A context mismatch isn't "ready": every long reply gets cut off.
    if python_ok and config_ok and not mismatch:
        if wizard_write_target and wizard_write_target != ENV_PATH:
            print(
                f"{CHECK} Looks ready in {wizard_write_target.name} -- rename or copy it over "
                f"{ENV_PATH.name} (JFI's real config file) when you want to use it, then run `uv run jfi`."
            )
        else:
            print(f"{CHECK} Looks ready. Run `uv run jfi` (or `jfi` once installed) in your project folder.")
    else:
        print(f"{WARN}Not fully ready yet -- fix whichever check above failed, then run `uv run create-env` again.")


if __name__ == "__main__":
    main()
