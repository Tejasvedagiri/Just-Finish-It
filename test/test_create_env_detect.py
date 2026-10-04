"""create-env's detection (JFI.create_env_detect): the machine, the model
server and the model's speed, turned into computed settings. The user: "as
part of env create you need to add some logic to find the system model and
what is the right sizing by default. Do not take default values."

Every platform's readers run here on any OS, fed real output samples, so a
Linux or macOS path can't break unseen on a Windows dev machine (the user:
"whatever you add there should support Linux and Mac")."""

import json

from JFI import create_env_detect as detect

GB = 1024 ** 3


def _runner(outputs: dict):
    """A fake `run`: the output for a command, keyed by its first words."""
    def run(cmd, timeout=5.0):
        for prefix, out in outputs.items():
            if " ".join(cmd).startswith(prefix):
                return out
        return None
    return run


def _reader(files: dict):
    return lambda path: files.get(path)


# ------------------------------------------------------------------ the machine

class TestLinux:
    MEMINFO = "MemTotal:       65536000 kB\nMemFree:         1000000 kB\nMemAvailable:   20971520 kB\n"

    def test_ram_comes_from_proc_meminfo(self):
        s = detect.detect_system("Linux", run=_runner({}), read=_reader({"/proc/meminfo": self.MEMINFO}))
        assert round(s.ram_total_gb, 1) == 62.5 and round(s.ram_free_gb) == 20
        assert s.gpu is None and s.model_memory()[0] == "RAM"

    def test_nvidia_gpu(self):
        run = _runner({"nvidia-smi": "NVIDIA GeForce RTX 5090, 32607, 29696\n"})
        s = detect.detect_system("Linux", run=run, read=_reader({"/proc/meminfo": self.MEMINFO}))
        assert s.gpu.kind == "nvidia" and s.gpu.name == "NVIDIA GeForce RTX 5090"
        assert round(s.gpu.total_gb, 1) == 31.8 and round(s.gpu.free_gb, 1) == 2.8

    def test_amd_gpu_through_rocm_smi(self):
        rocm = json.dumps({"card0": {"VRAM Total Memory (B)": str(24 * GB), "VRAM Total Used Memory (B)": str(4 * GB)}})
        s = detect.detect_system("Linux", run=_runner({"rocm-smi": rocm}), read=_reader({"/proc/meminfo": self.MEMINFO}))
        assert (s.gpu.kind, s.gpu.total_gb, s.gpu.free_gb) == ("amd", 24, 20)

    def test_amd_gpu_without_rocm_reads_sysfs(self):
        files = {"/proc/meminfo": self.MEMINFO,
                 "/sys/class/drm/card0/device/mem_info_vram_total": str(16 * GB),
                 "/sys/class/drm/card0/device/mem_info_vram_used": str(1 * GB)}
        s = detect.detect_system("Linux", run=_runner({}), read=_reader(files))
        assert (s.gpu.kind, s.gpu.total_gb, s.gpu.free_gb) == ("amd", 16, 15)


class TestMacOS:
    VM_STAT = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
               "Pages free:                               65536.\nPages active:  100000.\n"
               "Pages inactive:                           65536.\nPages speculative:  65536.\n")

    def test_apple_silicon_uses_shared_memory(self):
        run = _runner({"sysctl -n hw.memsize": str(64 * GB), "vm_stat": self.VM_STAT,
                       "sysctl -n hw.optional.arm64": "1", "sysctl -n machdep.cpu.brand_string": "Apple M3 Max"})
        s = detect.detect_system("Darwin", run=run, read=_reader({}))
        assert s.ram_total_gb == 64 and s.ram_free_gb == 3  # 3 x 65536 pages x 16 KiB
        assert (s.gpu.kind, s.gpu.name, s.gpu.total_gb) == ("apple", "Apple M3 Max", 48)
        assert s.model_memory()[0] == "shared memory"

    def test_intel_mac_with_an_amd_card(self):
        profiler = json.dumps({"SPDisplaysDataType": [{"sppci_model": "AMD Radeon Pro 5500M", "spdisplays_vram": "8 GB"}]})
        run = _runner({"sysctl -n hw.memsize": str(32 * GB), "vm_stat": self.VM_STAT,
                       "sysctl -n hw.optional.arm64": "0", "system_profiler": profiler})
        s = detect.detect_system("Darwin", run=run, read=_reader({}))
        assert (s.gpu.name, s.gpu.total_gb, s.gpu.free_gb) == ("AMD Radeon Pro 5500M", 8, None)


def test_a_machine_that_reports_nothing_is_unknown_not_an_error():
    s = detect.detect_system("Linux", run=_runner({}), read=_reader({}))
    assert (s.ram_total_gb, s.gpu) == (None, None)


# ------------------------------------------------------------------ the server

LMSTUDIO_MODELS = {"data": [
    {"id": "qwen/qwen3.8-27b", "type": "vlm", "arch": "qwen3_5", "quantization": "4bit", "state": "loaded",
     "max_context_length": 262144, "loaded_context_length": 38144},
    {"id": "meta/muse-glimmer", "type": "vlm", "state": "not-loaded", "max_context_length": 131072},
    {"id": "text-embedding-nomic", "type": "embeddings", "state": "not-loaded"},
]}
# `lms ls` listed a second (MLX) build under the loaded model's key; `lms ps`
# is what's really loaded (observed on this machine).
LMS_LS = json.dumps([{"modelKey": "qwen/qwen3.8-27b", "sizeBytes": 16 * GB, "paramsString": "27B"},
                     {"modelKey": "meta/muse-glimmer", "sizeBytes": 20 * GB, "paramsString": "30B"}])
LMS_PS = json.dumps([{"modelKey": "qwen/qwen3.8-27b", "sizeBytes": 23362325904, "paramsString": "27B",
                      "quantization": {"name": "Q6_K", "bits": 6}}])


def test_lm_studio_reports_the_loaded_context_and_lms_the_real_size(monkeypatch):
    monkeypatch.setattr(detect, "lms_path", lambda os_name=None: "lms")
    get = lambda url, **k: LMSTUDIO_MODELS if url == "http://127.0.0.1:1234/api/v0/models" else None  # noqa: E731
    models = detect.detect_models("lmstudio", "http://127.0.0.1:1234/v1", get_json=get,
                                  run=_runner({"lms ls": LMS_LS, "lms ps": LMS_PS}))
    assert [m.id for m in models] == ["qwen/qwen3.8-27b", "meta/muse-glimmer"]  # the embedding model is skipped
    qwen = models[0]
    assert (qwen.loaded, qwen.loaded_context, qwen.max_context) == (True, 38144, 262144)
    assert (round(qwen.size_gb, 1), qwen.quantization) == (21.8, "Q6_K")
    assert models[1].loaded is False and models[1].size_gb == 20


def test_ollama():
    def get(url, body=None, **k):
        if url.endswith("/api/tags"):
            return {"models": [{"name": "qwen3:32b", "size": 20 * GB}]}
        if url.endswith("/api/ps"):
            return {"models": [{"name": "qwen3:32b", "context_length": 16384}]}
        if url.endswith("/api/show"):
            return {"details": {"parameter_size": "32.8B", "quantization_level": "Q4_K_M"},
                    "model_info": {"qwen3.context_length": 40960}}
    [m] = detect.detect_models("ollama", "http://127.0.0.1:11434/v1", get_json=get)
    assert (m.loaded, m.loaded_context, m.max_context, m.params, m.size_gb) == (True, 16384, 40960, "32.8B", 20)


def test_llama_cpp():
    def get(url, **k):
        if url.endswith("/props"):
            return {"default_generation_settings": {"n_ctx": 32768}}
        return {"data": [{"id": "gemma-4-31b.gguf", "meta": {"n_ctx_train": 131072, "size": 18 * GB}}]}
    [m] = detect.detect_models("llamacpp", "http://127.0.0.1:8080/v1", get_json=get)
    assert (m.id, m.loaded_context, m.max_context, m.size_gb) == ("gemma-4-31b.gguf", 32768, 131072, 18)


def test_vllm_reports_max_model_len():
    get = lambda url, **k: {"data": [{"id": "Qwen/Qwen3-32B", "max_model_len": 40960}]}  # noqa: E731
    [m] = detect.detect_models("custom", "http://gpu-box:8000/v1", get_json=get)
    assert (m.loaded_context, m.max_context) == (40960, 40960)


def test_hosted_and_down_servers_report_nothing():
    assert detect.detect_models("openai", "https://api.openai.com/v1") is None
    assert detect.detect_models("lmstudio", "http://127.0.0.1:1234/v1", get_json=lambda url, **k: None) is None


# ------------------------------------------------------------------ the speed

def test_speed_comes_from_the_servers_token_count_and_the_tool_call_is_checked():
    times = iter([100.0, 102.0])
    reply = {"choices": [{"message": {"tool_calls": [{"function": {"name": "ready"}}]}, "finish_reason": "tool_calls"}],
             "usage": {"completion_tokens": 200}}
    speed = detect.measure_speed("http://x/v1", "", "m", post_json=lambda url, **k: reply, clock=lambda: next(times))
    assert (speed.tokens_per_second, speed.tool_calls_work) == (100, True)


def test_a_model_that_answers_without_the_tool_is_flagged():
    reply = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}], "usage": {"completion_tokens": 5}}
    speed = detect.measure_speed("http://x/v1", "", "m", post_json=lambda url, **k: reply)
    assert speed.tool_calls_work is False


# ------------------------------------------------------------------ computing

def _values(suggestions):
    return {s.name: s.value for s in suggestions}


def _this_machine():
    system = detect.System("Windows", 61.6, 16.0, 12, detect.Gpu("nvidia", "RTX 5090", 31.8, 2.8))
    model = detect.ServerModel("qwen/qwen3.8-27b", True, 38144, 262144, 21.8, "27B", "Q6_K", "qwen35")
    return detect.Detected(system=system, model=model, speed=detect.Speed(87.0, True))


def test_computed_values_on_the_machine_the_design_was_written_on():
    """The worked example in docs/create_env_sizing.md, measured for real."""
    values = _values(detect.compute("qwen/qwen3.8-27b", _this_machine(), lambda m: 32768))
    assert values == {"CONTEXT_SIZE": "38144", "CONTEXT_COMPRESSION_RATIO": "0.73", "STREAM_OUTPUT_CAP": "10000",
                      "REASONING_OUTPUT_CAP": "8000", "LLM_REQUEST_TIMEOUT": "290", "TOOL_RESULT_MAX_TOKENS": "3000",
                      "MAX_EPISODE_TURNS": "25", "LAYA_DEVICE": "cpu", "UNLOAD_LLM_BEFORE_LAYA": "0"}


def test_a_model_that_isnt_loaded_says_how_to_load_it():
    found = detect.Detected(model=detect.ServerModel("meta/muse-glimmer", False, None, 131072))
    [context] = [s for s in detect.compute("meta/muse-glimmer", found, lambda m: 8192) if s.name == "CONTEXT_SIZE"]
    assert context.value == "32768"
    assert "lms load meta/muse-glimmer --context-length 32768" in context.reason
    assert any("isn't loaded" in w for w in found.warnings)


def test_a_small_context_keeps_a_proportionate_reply_reserve():
    found = detect.Detected(model=detect.ServerModel("qwen3:8b", True, 8192, 40960))
    values = _values(detect.compute("qwen3:8b", found, lambda m: 8192))
    assert values["CONTEXT_COMPRESSION_RATIO"] == "0.60" and values["STREAM_OUTPUT_CAP"] == "3276"


def test_a_free_nvidia_gpu_runs_laya_and_low_ram_unloads_the_model():
    found = detect.Detected(system=detect.System("Linux", 16.0, 4.0, 8, detect.Gpu("nvidia", "RTX 4090", 24.0, 8.0)))
    values = _values(detect.compute("llama-3.1-8b", found, lambda m: 32768))
    assert (values["LAYA_DEVICE"], values["UNLOAD_LLM_BEFORE_LAYA"]) == ("cuda", "1")


def test_a_model_bigger_than_its_memory_is_warned_about():
    found = detect.Detected(system=detect.System("Linux", 32.0, 20.0, 8, detect.Gpu("nvidia", "RTX 4070", 12.0, 11.0)),
                            model=detect.ServerModel("big", True, 32768, 32768, 40.0))
    detect.compute("big", found, lambda m: 32768)
    assert any("40.0 GB" in w and "12.0 GB" in w for w in found.warnings)


def test_the_loaded_context_is_checked_against_context_size():
    """The first calc run died because LM Studio had reloaded the model at
    4,096 tokens while .env said 38,000; nothing compared the two."""
    loaded = detect.ServerModel("qwen/qwen3.8-27b", True, 4096, 262144)
    warning = detect.context_mismatch("38000", loaded)
    assert "38,000" in warning and "4,096" in warning and "CONTEXT_SIZE=4096" in warning
    assert detect.context_mismatch("4096", loaded) is None
    assert detect.context_mismatch("38000", detect.ServerModel("m", False, None, 8192)) is None


def test_a_session_start_warns_when_the_server_reloaded_the_model_smaller():
    """create-env's check can go stale: the server can reload a model at any
    time. A session start repeats it, with one quick query."""
    small = {"data": [{"id": "qwen/qwen3.8-27b", "state": "loaded", "loaded_context_length": 4096,
                       "max_context_length": 262144}]}
    env = {"LLM_BACKEND": "lmstudio", "MODEL": "qwen/qwen3.8-27b", "CONTEXT_SIZE": "38000"}
    warning = detect.startup_warning(env, get_json=lambda url, **k: small)
    assert "4,096" in warning
    assert detect.startup_warning({**env, "CONTEXT_SIZE": "4096"}, get_json=lambda url, **k: small) is None
    assert detect.startup_warning({**env, "LLM_BACKEND": "anthropic"}, get_json=lambda url, **k: small) is None
    assert detect.startup_warning(env, get_json=lambda url, **k: None) is None  # server down: silent


# ------------------------------------------------------------------ PARALLEL_LLM

def test_lm_studio_and_llama_cpp_report_how_many_requests_they_serve_at_once(monkeypatch):
    """The planner caps PARALLEL_LLM by these (JFI.llm.parallel); create-env
    reads the same numbers so its suggestion matches what a run will use."""
    monkeypatch.setattr(detect, "lms_path", lambda os_name=None: "lms")
    ps = json.dumps([{"modelKey": "qwen/qwen3.8-27b", "contextLength": 40960, "parallel": 4}])
    get = lambda url, **k: LMSTUDIO_MODELS if url.endswith("/api/v0/models") else None  # noqa: E731
    qwen, glimmer = detect.detect_models("lmstudio", "http://127.0.0.1:1234/v1", get_json=get,
                                         run=_runner({"lms ls": LMS_LS, "lms ps": ps}))
    assert (qwen.parallel, glimmer.parallel) == (4, None)

    def llama(url, **k):
        if url.endswith("/props"):
            return {"total_slots": 2, "default_generation_settings": {"n_ctx": 65536}}
        return {"data": [{"id": "gemma-4-31b.gguf"}]}
    [m] = detect.detect_models("llamacpp", "http://127.0.0.1:8080/v1", get_json=llama)
    assert m.parallel == 2


def test_parallel_is_capped_by_how_many_episodes_the_loaded_context_holds():
    """Observed on LM Studio (qwen3.8-27b, 40,960 context, parallel=4): two
    requests at once ran, four failed with "Context size has been exceeded"
    -- the slots share one context. At CONTEXT_SIZE=20480 that's 2."""
    loaded = detect.Detected(model=detect.ServerModel("qwen/qwen3.8-27b", True, 40960, 262144, parallel=4))
    capped = detect.suggest_parallel("lmstudio", loaded, 20480)
    assert capped.value == "2"
    assert "CONTEXT_SIZE to 10240" in capped.reason and "81920-token context for 4" in capped.reason
    assert detect.suggest_parallel("lmstudio", loaded, 10240).value == "4"


def test_parallel_is_one_when_the_server_reports_nothing_and_as_is_when_hosted():
    not_loaded = detect.Detected(model=detect.ServerModel("meta/muse-glimmer", False, None, 131072))
    suggestion = detect.suggest_parallel("lmstudio", not_loaded, 32768)
    assert suggestion.value == "1" and "lms load meta/muse-glimmer --parallel N" in suggestion.reason
    ollama = detect.Detected(model=detect.ServerModel("qwen3:32b", True, 16384, 40960))
    assert detect.suggest_parallel("ollama", ollama, 16384).value == "1"
    one_slot = detect.Detected(model=detect.ServerModel("m", True, 65536, parallel=1))
    assert detect.suggest_parallel("llamacpp", one_slot, 16384).value == "1"
    hosted = detect.Detected(hosted=True)
    assert detect.suggest_parallel("anthropic", hosted, 200000).value == str(detect.HOSTED_PARALLEL)


def test_a_parallel_setting_the_server_cant_serve_is_noted():
    loaded = detect.ServerModel("qwen/qwen3.8-27b", True, 40960, 262144, parallel=4)
    note = detect.parallel_shortfall("4", "20480", "lmstudio", loaded)
    assert "PARALLEL_LLM is 4" in note and "run 2 at a time" in note
    assert detect.parallel_shortfall("2", "20480", "lmstudio", loaded) is None
    assert detect.parallel_shortfall("1", "40960", "lmstudio", loaded) is None
    # Nothing reported (a vLLM box, LM Studio without `lms`): the runtime explains it, not create-env.
    assert detect.parallel_shortfall("4", "20480", "custom", detect.ServerModel("m", True, 40960)) is None
