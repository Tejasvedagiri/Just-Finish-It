"""JFI.llm.lmstudio_control -- the `lms` CLI is faked (the real one would
unload the user's models), but the snapshot it parses is a real
`lms ps --json` shape captured from LM Studio on the dev machine."""
from __future__ import annotations

import json
import subprocess

import pytest

from JFI.llm.lmstudio_control import LMStudioControl

# Trimmed from a real `lms ps --json` (2026-09-28): one GGUF model with no TTL,
# one MLX model with a selected variant and a TTL.
PS_JSON = [
    {"type": "llm", "modelKey": "qwen/qwen3.8-27b", "identifier": "qwen/qwen3.8-27b",
     "ttlMs": None, "contextLength": 40192, "parallel": 4},
    {"type": "llm", "modelKey": "google/gemma-4-12b", "identifier": "google/gemma-4-12b",
     "selectedVariant": "google/gemma-4-12b@4bit", "ttlMs": 3600000, "contextLength": 42496, "parallel": 2},
    {"type": "embedding", "modelKey": "nomic-embed", "identifier": "nomic-embed"},
]


class FakeLms:
    def __init__(self, fail_load_for=()):
        self.calls = []
        self.fail_load_for = set(fail_load_for)

    def __call__(self, args):
        self.calls.append(args[1:])
        if args[1] == "ps":
            return subprocess.CompletedProcess(args, 0, json.dumps(PS_JSON), "")
        if args[1] == "load" and args[2] in self.fail_load_for:
            return subprocess.CompletedProcess(args, 1, "", "not enough memory")
        return subprocess.CompletedProcess(args, 0, "", "")


def test_unload_then_reload_with_the_same_settings():
    lms = FakeLms()
    control = LMStudioControl(lms_path="lms", runner=lms)
    with control.models_unloaded(log=lambda _: None):
        assert lms.calls == [["ps", "--json"], ["unload", "--all"]]

    loads = [c for c in lms.calls if c[0] == "load"]
    assert loads == [
        ["load", "qwen/qwen3.8-27b", "-y", "--context-length", "40192", "--parallel", "4"],
        # modelKey, not selectedVariant: `lms load <modelKey>@4bit` didn't resolve in practice.
        ["load", "google/gemma-4-12b", "-y", "--context-length", "42496", "--parallel", "2", "--ttl", "3600"],
    ], "embedding models aren't touched; LLMs come back with their context, parallelism and TTL"


def test_reload_happens_even_if_the_block_raises():
    lms = FakeLms()
    with pytest.raises(RuntimeError):
        with LMStudioControl(lms_path="lms", runner=lms).models_unloaded(log=lambda _: None):
            raise RuntimeError("laya crashed")
    assert sum(1 for c in lms.calls if c[0] == "load") == 2


def test_one_failed_reload_does_not_stop_the_others():
    lms = FakeLms(fail_load_for={"qwen/qwen3.8-27b"})
    logs = []
    with LMStudioControl(lms_path="lms", runner=lms).models_unloaded(log=logs.append):
        pass
    assert sum(1 for c in lms.calls if c[0] == "load") == 2
    assert any(line.startswith("Error: could not reload qwen/qwen3.8-27b") for line in logs)


def test_missing_lms_runs_the_block_without_unloading():
    logs = []
    control = LMStudioControl(runner=FakeLms())
    control.lms = None
    ran = []
    with control.models_unloaded(log=logs.append):
        ran.append(True)
    assert ran and "wasn't found" in logs[0]


def test_nothing_loaded_means_nothing_to_unload():
    class Empty(FakeLms):
        def __call__(self, args):
            self.calls.append(args[1:])
            return subprocess.CompletedProcess(args, 0, "[]", "")

    lms = Empty()
    with LMStudioControl(lms_path="lms", runner=lms).models_unloaded(log=lambda _: None):
        pass
    assert lms.calls == [["ps", "--json"]]
