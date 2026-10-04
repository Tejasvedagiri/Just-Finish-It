"""PARALLEL_LLM: how many requests one role may have in flight at once --
used by the planner to run Lead and Task breakdown episodes side by side.

Asking isn't enough: the server has to serve them in parallel too. A local
server with one slot queues the extra requests, so N "parallel" episodes
run one after another anyway, each now also waiting on the others' turns.
So the setting is capped by what the role's model server reports:

- Anthropic, or an OpenAI-compatible API on a non-local host: hosted APIs
  serve concurrent requests; the setting is used as is.
- LM Studio: the loaded model's `parallel` from `lms ps --json` (set when
  the model is loaded: `lms load <model> --parallel N`, or the load
  dialog), and no more episodes than its loaded context holds whole
  (contextLength // CONTEXT_SIZE). Not loaded, or `lms` missing: one at a
  time.
- llama.cpp (or any local server that answers llama.cpp's `/props`): its
  `total_slots` (`llama-server --parallel N`), and no more than its context
  (`n_ctx`) holds whole.

The context cap is because the parallel slots share one context. Observed
on LM Studio (qwen3.8-27b, -c 40960 --parallel 4): one 11.5k-token request
and two at once both ran, four at once failed with "Context size has been
exceeded" -- and every episode is sized to grow to CONTEXT_SIZE on its own.
- Ollama, and any other local server: one at a time -- none of them reports
  how many requests it serves at once (OLLAMA_NUM_PARALLEL is server-side).
"""

import ipaddress
import os
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

from JFI.episode.budget import DEFAULT_CONTEXT_SIZE
from JFI.llm.base_llm_stream import phase_env
from JFI.llm.lmstudio_control import LMStudioControl

MAX_PARALLEL_LLM = 10
_PROBE_TIMEOUT_SECONDS = 5.0


def requested_parallel() -> int:
    """PARALLEL_LLM from .env, clamped to 1..MAX_PARALLEL_LLM; unset or not a
    number is 1 (off)."""
    try:
        value = int(os.environ.get("PARALLEL_LLM", "1").strip() or "1")
    except ValueError:
        return 1
    return max(1, min(MAX_PARALLEL_LLM, value))


def _is_local(host: Optional[str]) -> bool:
    if not host or host == "localhost" or "." not in host:
        return True  # a bare machine name ("gpu-box") is on the LAN, not a hosted API
    if host.endswith((".local", ".lan", ".internal", ".home")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _episode_window(prefix) -> int:
    try:
        return max(1, int(phase_env(prefix, "CONTEXT_SIZE", str(DEFAULT_CONTEXT_SIZE))))
    except ValueError:
        return DEFAULT_CONTEXT_SIZE


def _fits(slots: int, context: Optional[int], prefix) -> Tuple[int, str]:
    """`slots`, cut to how many CONTEXT_SIZE episodes `context` holds."""
    window = _episode_window(prefix)
    if not isinstance(context, int) or context <= 0 or context // window >= slots:
        return slots, ""
    whole = max(1, context // window)
    return whole, (f"; its {context}-token context holds {whole} episode(s) of CONTEXT_SIZE={window} -- load it "
                   f"with a context of {window * slots} (or lower CONTEXT_SIZE) for {slots}")


def _llamacpp_slots(base_url: str, prefix=()) -> Optional[Tuple[int, str]]:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    try:
        response = httpx.get(f"{root}/props", timeout=_PROBE_TIMEOUT_SECONDS)
        props = response.json() if response.status_code == 200 else {}
        slots = props.get("total_slots")
        context = (props.get("default_generation_settings") or {}).get("n_ctx")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    if not isinstance(slots, int) or slots < 1:
        return None
    workers, short = _fits(slots, context, prefix)
    return workers, f"the server reports {slots} slot(s){short}"


def _lmstudio_slots(model: str, control: Optional[LMStudioControl] = None, prefix=()) -> Tuple[int, str]:
    control = control or LMStudioControl()
    if not control.available():
        return 1, "LM Studio's `lms` CLI wasn't found, so its parallel setting can't be read"
    try:
        loaded = control.loaded_models()
    except Exception as e:
        return 1, f"`lms ps` failed ({e})"
    match = next((m for m in loaded if model in (m.get("identifier"), m.get("modelKey"))), None)
    if match is None:
        return 1, f"{model} isn't loaded in LM Studio"
    parallel = match.get("parallel")
    if not isinstance(parallel, int) or parallel < 1:
        return 1, f"LM Studio didn't report a parallel setting for {model}"
    workers, short = _fits(parallel, match.get("contextLength"), prefix)
    return workers, f"LM Studio serves {model} with parallel={parallel}{short}"


def server_parallel(llm, lmstudio: Optional[LMStudioControl] = None) -> Tuple[Optional[int], str]:
    """How many requests `llm`'s server runs at once (None: no limit to
    respect, a hosted API), and why -- for the planner's one-line notice."""
    prefix = getattr(llm, "prefix", "")
    backend = phase_env(prefix, "LLM_BACKEND", "").strip().lower()
    if backend in ("anthropic", "claude"):
        return None, "the Anthropic API serves concurrent requests"
    model = getattr(llm, "model", "")
    if backend in ("lmstudio", "lm-studio", "lm studio"):
        return _lmstudio_slots(model, lmstudio, prefix)
    url = phase_env(prefix, "OPENAI_URL")
    if backend == "ollama":
        return 1, "Ollama doesn't report how many requests it serves at once"
    slots = _llamacpp_slots(url, prefix) if url else None
    if slots is not None:
        return slots
    if backend in ("llamacpp", "llama.cpp", "llama-cpp"):
        return 1, "llama.cpp's /props didn't answer with total_slots"
    host = urlparse(url).hostname if url else None
    if not _is_local(host):
        return None, f"{host} is a hosted API"
    return 1, f"the local server at {url} doesn't report how many requests it serves at once"


def effective_parallel(llm, lmstudio: Optional[LMStudioControl] = None) -> Tuple[int, str]:
    """min(PARALLEL_LLM, the server's own limit), and why."""
    wanted = requested_parallel()
    if wanted <= 1:
        return 1, "PARALLEL_LLM is off"
    slots, why = server_parallel(llm, lmstudio)
    return (wanted if slots is None else min(wanted, slots)), why
