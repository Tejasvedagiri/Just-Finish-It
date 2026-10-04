"""What `create-env` measures before it suggests settings: the machine (RAM,
GPU memory, cores), the model server (which model is loaded, at what
context, how big it is) and the model's speed -- then `compute()` turns that
into suggested values, each with the reason shown to the user.

Stdlib only, like create_env itself: it runs before dependencies are
installed. Every probe fails soft -- a missing tool, a server that's down, an
unknown OS all just mean "unknown", and the value falls back with that as its
reason. Linux, macOS and Windows each have their own readers; commands and
file reads go through `run`/`read` parameters so tests can feed any platform's
real output on any machine. Design and decisions: docs/create_env_sizing.md.
"""

import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

GB = 1024 ** 3


def _run(cmd: List[str], timeout: float = 5.0) -> Optional[str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _read(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _get_json(url: str, headers: Optional[dict] = None, body: Optional[dict] = None,
              timeout: float = 5.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 -- down, refused, bad JSON: all "unknown"
        return None


# ------------------------------------------------------------------ the machine

@dataclass
class Gpu:
    kind: str  # nvidia / amd / apple
    name: str
    total_gb: Optional[float]
    free_gb: Optional[float]


@dataclass
class System:
    os_name: str  # Linux / Darwin / Windows
    ram_total_gb: Optional[float] = None
    ram_free_gb: Optional[float] = None
    cores: Optional[int] = None
    gpu: Optional[Gpu] = None

    def model_memory(self) -> tuple[str, Optional[float], Optional[float]]:
        """Where a local model lives, and that memory's total/free (GB): a
        GPU's own VRAM, Apple Silicon's shared memory, or plain RAM."""
        if self.gpu and self.gpu.kind == "apple":
            return "shared memory", self.gpu.total_gb, self.gpu.free_gb
        if self.gpu and self.gpu.total_gb:
            return f"{self.gpu.name} VRAM", self.gpu.total_gb, self.gpu.free_gb
        return "RAM", self.ram_total_gb, self.ram_free_gb


def _ram_linux(read: Callable) -> tuple:
    text = read("/proc/meminfo") or ""
    kb = {m.group(1): int(m.group(2)) for m in re.finditer(r"^(\w+):\s+(\d+) kB", text, re.M)}
    total, free = kb.get("MemTotal"), kb.get("MemAvailable")
    return (total / 1024 ** 2 if total else None), (free / 1024 ** 2 if free else None)


def _ram_macos(run: Callable) -> tuple:
    total = run(["sysctl", "-n", "hw.memsize"])
    vm = run(["vm_stat"]) or ""
    page = re.search(r"page size of (\d+) bytes", vm)
    pages = {m.group(1): int(m.group(2)) for m in re.finditer(r"^Pages (\w+):\s+(\d+)\.", vm, re.M)}
    free = None
    if page and pages:
        free = sum(pages.get(k, 0) for k in ("free", "inactive", "speculative")) * int(page.group(1)) / GB
    return (int(total) / GB if total and total.strip().isdigit() else None), free


def _ram_windows() -> tuple:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None, None
    except (AttributeError, OSError):
        return None, None
    return status.ullTotalPhys / GB, status.ullAvailPhys / GB


def _gpu_nvidia(run: Callable) -> Optional[Gpu]:
    out = run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"])
    gpus = []
    for line in (out or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            total, used = int(parts[1]) / 1024, int(parts[2]) / 1024
            gpus.append(Gpu("nvidia", parts[0], total, total - used))
    return max(gpus, key=lambda g: g.total_gb) if gpus else None


def _gpu_amd_linux(run: Callable, read: Callable) -> Optional[Gpu]:
    data = None
    out = run(["rocm-smi", "--showmeminfo", "vram", "--json"])
    if out:
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = None
    cards = []
    for card in (data or {}).values():
        if isinstance(card, dict):
            total = card.get("VRAM Total Memory (B)")
            used = card.get("VRAM Total Used Memory (B)")
            if total and used:
                cards.append(Gpu("amd", "AMD GPU", int(total) / GB, (int(total) - int(used)) / GB))
    if not cards:
        for n in range(8):
            base = f"/sys/class/drm/card{n}/device/"
            total, used = read(base + "mem_info_vram_total"), read(base + "mem_info_vram_used")
            if total and used and total.strip().isdigit() and used.strip().isdigit():
                cards.append(Gpu("amd", "AMD GPU", int(total) / GB, (int(total) - int(used)) / GB))
    return max(cards, key=lambda g: g.total_gb) if cards else None


def _gpu_macos(run: Callable, ram_total: Optional[float], ram_free: Optional[float]) -> Optional[Gpu]:
    if (run(["sysctl", "-n", "hw.optional.arm64"]) or "").strip() == "1":
        chip = (run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Apple Silicon").strip()
        # Metal lets the GPU use about three quarters of unified memory by default.
        return Gpu("apple", chip, ram_total * 0.75 if ram_total else None, ram_free)
    out = run(["system_profiler", "SPDisplaysDataType", "-json"])
    try:
        displays = json.loads(out or "{}").get("SPDisplaysDataType", [])
    except json.JSONDecodeError:
        displays = []
    for d in displays:
        vram = str(d.get("spdisplays_vram") or d.get("spdisplays_vram_shared") or "")
        m = re.match(r"(\d+)\s*(GB|MB)", vram)
        if m:
            size = int(m.group(1)) / (1 if m.group(2) == "GB" else 1024)
            return Gpu("amd", d.get("sppci_model", "GPU"), size, None)
    return None


def detect_system(os_name: Optional[str] = None, run: Callable = _run, read: Callable = _read) -> System:
    os_name = os_name or platform.system()
    system = System(os_name=os_name, cores=os.cpu_count())
    if os_name == "Linux":
        system.ram_total_gb, system.ram_free_gb = _ram_linux(read)
        system.gpu = _gpu_nvidia(run) or _gpu_amd_linux(run, read)
    elif os_name == "Darwin":
        system.ram_total_gb, system.ram_free_gb = _ram_macos(run)
        system.gpu = _gpu_macos(run, system.ram_total_gb, system.ram_free_gb)
    elif os_name == "Windows":
        system.ram_total_gb, system.ram_free_gb = _ram_windows()
        system.gpu = _gpu_nvidia(run)
    return system


# ------------------------------------------------------------------ the server

@dataclass
class ServerModel:
    id: str
    loaded: Optional[bool] = None
    loaded_context: Optional[int] = None
    max_context: Optional[int] = None
    size_gb: Optional[float] = None
    params: str = ""
    quantization: str = ""
    arch: str = ""
    parallel: Optional[int] = None  # requests the server runs at once (LM Studio's `parallel`, llama.cpp's slots)


def _root(url: str) -> str:
    return re.sub(r"/v1/?$", "", url.rstrip("/"))


def lms_path(os_name: Optional[str] = None) -> Optional[str]:
    found = shutil.which("lms")
    if found:
        return found
    exe = "lms.exe" if (os_name or platform.system()) == "Windows" else "lms"
    candidate = Path.home() / ".lmstudio" / "bin" / exe
    return str(candidate) if candidate.exists() else None


def _lmstudio(url: str, get_json: Callable, run: Callable) -> Optional[List[ServerModel]]:
    data = get_json(_root(url) + "/api/v0/models")
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None
    models = [ServerModel(id=m["id"], loaded=m.get("state") == "loaded",
                          loaded_context=m.get("loaded_context_length"), max_context=m.get("max_context_length"),
                          quantization=str(m.get("quantization") or ""), arch=str(m.get("arch") or ""))
              for m in data["data"] if isinstance(m, dict) and m.get("id") and m.get("type") in (None, "llm", "vlm")]
    # `lms ps` describes exactly what's loaded. `lms ls` can list two builds
    # under one key (observed: a 4-bit MLX one and the Q6_K GGUF actually
    # loaded), so it only fills in models that aren't loaded.
    lms = lms_path()
    details = {}
    for command in (["ls", "--json"], ["ps", "--json"]):
        listed = run([lms, *command]) if lms else None
        try:
            details.update({e.get("modelKey"): e for e in json.loads(listed or "[]") if isinstance(e, dict)})
        except json.JSONDecodeError:
            pass
    for m in models:
        entry = details.get(m.id) or {}
        if entry.get("sizeBytes"):
            m.size_gb = entry["sizeBytes"] / GB
        m.params = str(entry.get("paramsString") or "")
        quant = entry.get("quantization")
        if isinstance(quant, dict) and quant.get("name"):
            m.quantization = quant["name"]
        if m.loaded and isinstance(entry.get("parallel"), int):
            m.parallel = entry["parallel"]
    return models


def _ollama(url: str, get_json: Callable) -> Optional[List[ServerModel]]:
    root = _root(url)
    tags = get_json(root + "/api/tags")
    if not isinstance(tags, dict):
        return None
    running = {m.get("name"): m for m in ((get_json(root + "/api/ps") or {}).get("models") or [])}
    models = []
    for m in tags.get("models") or []:
        name = m.get("name")
        info = (get_json(root + "/api/show", body={"model": name}) or {})
        details = info.get("details") or m.get("details") or {}
        max_ctx = next((v for k, v in (info.get("model_info") or {}).items() if k.endswith(".context_length")), None)
        live = running.get(name)
        models.append(ServerModel(id=name, loaded=live is not None,
                                  loaded_context=(live or {}).get("context_length"), max_context=max_ctx,
                                  size_gb=(m.get("size") or 0) / GB or None,
                                  params=str(details.get("parameter_size") or ""),
                                  quantization=str(details.get("quantization_level") or "")))
    return models


def _llamacpp(url: str, get_json: Callable) -> Optional[List[ServerModel]]:
    props = get_json(_root(url) + "/props")
    listed = get_json(url.rstrip("/") + "/models") or {}
    if not isinstance(props, dict):
        return None
    ctx = props.get("n_ctx") or (props.get("default_generation_settings") or {}).get("n_ctx")
    slots = props.get("total_slots") if isinstance(props.get("total_slots"), int) else None
    models = []
    for m in listed.get("data") or [{"id": "the loaded model"}]:
        meta = m.get("meta") or {}
        models.append(ServerModel(id=m.get("id", "?"), loaded=True, loaded_context=ctx,
                                  max_context=meta.get("n_ctx_train"),
                                  size_gb=(meta.get("size") or 0) / GB or None, parallel=slots))
    return models


def _openai_compatible(url: str, key: str, get_json: Callable) -> Optional[List[ServerModel]]:
    """vLLM (and other servers) report `max_model_len`: the window it serves."""
    data = get_json(url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"} if key else None)
    if not isinstance(data, dict):
        return None
    return [ServerModel(id=m["id"], loaded=True if m.get("max_model_len") else None,
                        loaded_context=m.get("max_model_len"), max_context=m.get("max_model_len"))
            for m in data.get("data") or [] if isinstance(m, dict) and m.get("id")]


def detect_models(backend: str, url: str, key: str = "", get_json: Callable = _get_json,
                  run: Callable = _run) -> Optional[List[ServerModel]]:
    """What the server says about its models, or None for hosted APIs and
    servers that report nothing useful."""
    if backend in ("anthropic", "claude", "openai") or not url:
        return None
    if backend in ("lmstudio", "lm-studio", "lm studio"):
        return _lmstudio(url, get_json, run)
    if backend == "ollama":
        return _ollama(url, get_json)
    if backend in ("llamacpp", "llama.cpp", "llama-cpp"):
        return _llamacpp(url, get_json)
    return _openai_compatible(url, key, get_json)


# ------------------------------------------------------------------ the speed

@dataclass
class Speed:
    tokens_per_second: Optional[float]
    tool_calls_work: Optional[bool]
    note: str = ""


_READY_TOOL = {"type": "function", "function": {
    "name": "ready", "description": "Say you're ready.",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}


def measure_speed(url: str, key: str, model: str, post_json: Callable = _get_json,
                  clock: Callable = time.monotonic) -> Speed:
    """One short request that must end in a tool call: tokens per second
    from the server's own usage count, and proof the model can call tools
    (JFI can't run on a model that can't)."""
    body = {"model": model, "max_tokens": 2000, "stream": False, "tools": [_READY_TOOL],
            "messages": [{"role": "user", "content": "Call the `ready` tool with a one-sentence greeting."}]}
    start = clock()
    reply = post_json(url.rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {key}"} if key else None,
                      body=body, timeout=180)
    elapsed = clock() - start
    if not isinstance(reply, dict) or not reply.get("choices"):
        return Speed(None, None, "the test request failed")
    message = reply["choices"][0].get("message") or {}
    tokens = (reply.get("usage") or {}).get("completion_tokens")
    speed = tokens / elapsed if tokens and elapsed > 0 else None
    called = bool(message.get("tool_calls"))
    finished_early = reply["choices"][0].get("finish_reason") == "length"
    return Speed(speed, called if (called or not finished_early) else None,
                 "" if called or not finished_early else "it hit the token limit before calling the tool")


# ------------------------------------------------------------------ computing

@dataclass
class Suggestion:
    name: str
    value: str
    reason: str


@dataclass
class Detected:
    system: Optional[System] = None
    model: Optional[ServerModel] = None
    speed: Optional[Speed] = None
    hosted: bool = False
    laya_installed: bool = False
    warnings: List[str] = field(default_factory=list)


# Names that mark a reasoning ("thinking") model: its replies carry long
# reasoning before the answer, so it needs a much bigger reply reserve. The
# calc runs saw single qwen3.8 replies of up to 8,023 tokens.
_REASONING_HINTS = ("qwen3", "qwq", "deepseek-r1", "-r1", "gpt-oss", "thinking", "reason", "magistral",
                    "o1", "o3", "o4")
REASONING_RESERVE = 10_000
PLAIN_RESERVE = 4_000
FALLBACK_CONTEXT = 32_768


def is_reasoning_model(model_id: str, arch: str = "") -> bool:
    text = f"{model_id} {arch}".lower()
    return any(hint in text for hint in _REASONING_HINTS)


def load_command(model: ServerModel, context: int) -> str:
    return f"lms load {model.id} --context-length {context}"


def compute(model_id: str, detected: Detected, guess_context: Callable[[str], int]) -> List[Suggestion]:
    """Suggested values with the reason for each. Pure: everything it knows
    is in `detected`."""
    out: List[Suggestion] = []
    m = detected.model

    if m and m.loaded and m.loaded_context:
        context, why = m.loaded_context, f"the server has {m.id} loaded with a {m.loaded_context:,}-token context"
    elif m and m.max_context:
        context = min(m.max_context, FALLBACK_CONTEXT)
        why = (f"{m.id} isn't loaded, so its context isn't known yet -- load it with "
               f"`{load_command(m, context)}` and rerun create-env")
        detected.warnings.append(f"{m.id} isn't loaded. The first request loads it at the server's default "
                                 f"context, which can be small. Load it first: {load_command(m, context)}")
    else:
        context = guess_context(model_id)
        why = ("the hosted model's known context window" if detected.hosted
               else "the server didn't report one; guessed from the model's name")
    out.append(Suggestion("CONTEXT_SIZE", str(context), why))

    reasoning = is_reasoning_model(model_id, m.arch if m else "")
    reserve = REASONING_RESERVE if reasoning else PLAIN_RESERVE
    reserve = min(reserve, int(context * 0.4))
    if detected.hosted:
        ratio, ratio_why = 0.8, "hosted model: a large window, 20% kept for the reply"
    else:
        ratio = max(0.5, min(0.9, int((context - reserve) / context * 100) / 100))
        ratio_why = (f"keeps ~{context - int(context * ratio):,} tokens for the reply "
                     f"({'a reasoning model: long thinking before each answer' if reasoning else 'a non-reasoning model'})")
    out.append(Suggestion("CONTEXT_COMPRESSION_RATIO", f"{ratio:.2f}", ratio_why))
    budget = int(context * ratio)

    stream_cap = reserve
    out.append(Suggestion("STREAM_OUTPUT_CAP", str(stream_cap), "the reply reserve: the longest one reply may run"))
    reasoning_cap = max(1_000, reserve - 2_000) if reasoning else 2_000
    out.append(Suggestion("REASONING_OUTPUT_CAP", str(reasoning_cap),
                          "the reply reserve minus room for the tool call after the thinking" if reasoning
                          else "a non-reasoning model shouldn't think at length"))

    speed = detected.speed.tokens_per_second if detected.speed else None
    if speed:
        timeout = max(120, int(2 * stream_cap / speed + 60 + 9) // 10 * 10)
        out.append(Suggestion("LLM_REQUEST_TIMEOUT", str(timeout),
                              f"measured {speed:.0f} tokens/s: twice the longest reply, plus a minute to read the prompt"))
    else:
        out.append(Suggestion("LLM_REQUEST_TIMEOUT", "300", "speed not measured; a safe default"))

    tool_cap = max(2_000, min(8_000, round(budget * 0.1 / 500) * 500))
    out.append(Suggestion("TOOL_RESULT_MAX_TOKENS", str(tool_cap),
                          f"about 10% of the {budget:,}-token episode budget"))
    out.append(Suggestion("MAX_EPISODE_TURNS", "25",
                          "the token budget was never the limit on real runs; 15 turns ended leaves that were nearly done"))

    s = detected.system
    if s and s.gpu and s.gpu.kind == "nvidia" and (s.gpu.free_gb or 0) >= 4:
        out.append(Suggestion("LAYA_DEVICE", "cuda", f"{s.gpu.free_gb:.1f} GB free on {s.gpu.name}"))
    else:
        free = f"{s.gpu.free_gb:.1f} GB free on the GPU" if s and s.gpu and s.gpu.free_gb is not None else "no NVIDIA GPU"
        out.append(Suggestion("LAYA_DEVICE", "cpu", f"{free}; Laya needs about 4 GB of GPU memory"))
    ram_free = s.ram_free_gb if s else None
    low = ram_free is not None and ram_free < 6
    out.append(Suggestion("UNLOAD_LLM_BEFORE_LAYA", "1" if low else "0",
                          f"{ram_free:.1f} GB RAM free; Laya needs ~3.5 GB" if ram_free is not None
                          else "RAM not measured"))

    if m and m.size_gb and s:
        where, total, _ = s.model_memory()
        if total and m.size_gb > total:
            detected.warnings.append(f"{m.id} is {m.size_gb:.1f} GB but the {where} is {total:.1f} GB: it will "
                                     f"run partly from RAM, much slower.")
    return out


# JFI.llm.parallel.MAX_PARALLEL_LLM -- not imported: that module needs httpx,
# and create-env runs before dependencies are installed.
MAX_PARALLEL_LLM = 10
HOSTED_PARALLEL = 4


def suggest_parallel(backend: str, detected: Detected, context_size: int) -> Suggestion:
    """PARALLEL_LLM the way the planner will cap it at runtime
    (JFI.llm.parallel.effective_parallel): what the server serves at once, and
    no more episodes than its loaded context holds whole -- the slots share
    one context, and four 11.5k-token requests on a 40k LM Studio load
    failed with "Context size has been exceeded". A value above that cap
    only costs a notice at runtime, but it's no faster either."""
    if detected.hosted:
        return Suggestion("PARALLEL_LLM", str(HOSTED_PARALLEL),
                          "a hosted API serves concurrent requests; raise it if your rate limits allow")
    if backend == "ollama":
        return Suggestion("PARALLEL_LLM", "1", "Ollama doesn't report how many requests it serves at once, "
                                               "so JFI runs one at a time whatever this says")
    m = detected.model
    if not m or not m.parallel:
        how = (f"load it with `lms load {m.id} --parallel N` and rerun create-env" if m and backend == "lmstudio"
               else "start it with `--parallel N` (llama-server) for more")
        return Suggestion("PARALLEL_LLM", "1", f"the server didn't report how many requests it serves at once; {how}")
    if m.parallel <= 1:
        return Suggestion("PARALLEL_LLM", "1", f"the server serves {m.id} one request at a time; "
                                               "reload it with `--parallel N` for more")
    slots = min(m.parallel, MAX_PARALLEL_LLM)
    whole = max(1, m.loaded_context // context_size) if m.loaded_context and context_size > 0 else slots
    if whole >= slots:
        return Suggestion("PARALLEL_LLM", str(slots), f"the server serves {m.id} with parallel={m.parallel}")
    return Suggestion("PARALLEL_LLM", str(whole),
                      f"the server serves {m.id} with parallel={m.parallel}, but its {m.loaded_context:,}-token "
                      f"context holds {whole} episode(s) of CONTEXT_SIZE={context_size:,} -- lower CONTEXT_SIZE "
                      f"to {m.loaded_context // slots} or reload it with a {context_size * slots}-token context "
                      f"for {slots}")


def parallel_shortfall(env_parallel: Optional[str], env_context: Optional[str], backend: str,
                       model: Optional[ServerModel]) -> Optional[str]:
    """A note when .env's PARALLEL_LLM is more than the server reports it
    serves -- the planner caps it at runtime, so this says why it'll be
    slower than asked. Silent when the server reports nothing."""
    try:
        wanted, context = int(env_parallel or ""), int(env_context or "")
    except ValueError:
        return None
    if wanted <= 1 or not model or not model.loaded or not (model.parallel or backend == "ollama"):
        return None
    cap = suggest_parallel(backend, Detected(model=model), context)
    if int(cap.value) >= min(wanted, MAX_PARALLEL_LLM):
        return None
    return f"PARALLEL_LLM is {wanted} but the planner will run {cap.value} at a time: {cap.reason}."


def context_mismatch(env_context: Optional[str], model: Optional[ServerModel]) -> Optional[str]:
    """A warning when .env asks for more context than the server loaded --
    what killed the first calc run (LM Studio at 4,096, .env at 38,000)."""
    if not model or not model.loaded or not model.loaded_context or not env_context:
        return None
    try:
        wanted = int(env_context)
    except ValueError:
        return None
    if wanted <= model.loaded_context:
        return None
    return (f"CONTEXT_SIZE is {wanted:,} but the server loaded {model.id} with {model.loaded_context:,} "
            f"tokens: replies will be cut off. Set CONTEXT_SIZE={model.loaded_context} or reload the model "
            f"with a bigger context.")


def find_model(models: Optional[List[ServerModel]], model_id: str) -> Optional[ServerModel]:
    for m in models or []:
        if m.id == model_id:
            return m
    return None


_LOCAL_URLS = {"lmstudio": "http://127.0.0.1:1234/v1", "ollama": "http://127.0.0.1:11434/v1",
               "llamacpp": "http://127.0.0.1:8080/v1"}


def startup_warning(env, get_json: Callable = lambda url, **k: _get_json(url, timeout=2.0, **k)) -> Optional[str]:
    """The loaded-context check, run when a JFI session starts: the server
    can reload a model after create-env ran. One quick query (2 s timeout,
    no `lms` calls); silent when anything is unknown."""
    backend = (env.get("LLM_BACKEND") or "openai").strip().lower().replace("lm-studio", "lmstudio")
    backend = backend.replace("lm studio", "lmstudio").replace("llama.cpp", "llamacpp").replace("llama-cpp", "llamacpp")
    model, context = env.get("MODEL"), env.get("CONTEXT_SIZE")
    url = env.get("OPENAI_URL") or _LOCAL_URLS.get(backend, "")
    if not model or not context or not url:
        return None
    models = detect_models(backend, url, env.get("OPENAI_API_KEY", ""), get_json=get_json,
                           run=lambda *a, **k: None)
    return context_mismatch(context, find_model(models, model))
