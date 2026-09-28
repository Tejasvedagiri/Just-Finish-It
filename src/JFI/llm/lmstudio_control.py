"""Unloads LM Studio's loaded models around a memory-hungry step, then puts
them back exactly as they were.

Laya's two checkpoints cost ~3.5 GiB resident (laya_plan.md §5.1.1), on top
of a local LLM that already fills most of the machine. UNLOAD_LLM_BEFORE_LAYA
lets a user trade reload time for memory: snapshot `lms ps --json`, `lms
unload --all`, run the step, then `lms load` each model back with the same
key, context length, parallelism, identifier and TTL -- the settings JFI's
own requests depend on (a model reloaded at LM Studio's default context
would silently shrink the window the 20k episode budget is sized against).

Reload keys are `modelKey`, never `selectedVariant`: observed with lms on
this project's dev machine, `lms load qwen/qwen3.8-27b@4bit` fails to
resolve while `lms load qwen/qwen3.8-27b` loads the same selected variant.
"""

import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, List, Optional

Runner = Callable[[List[str]], subprocess.CompletedProcess]

LOAD_TIMEOUT_SECONDS = 900  # a 35B model can take minutes to load from disk


def _run(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=LOAD_TIMEOUT_SECONDS)


def find_lms() -> Optional[str]:
    """`lms` on PATH, else LM Studio's own install location (it only adds
    itself to PATH when the user runs its bootstrap step)."""
    found = shutil.which("lms")
    if found:
        return found
    exe = "lms.exe" if os.name == "nt" else "lms"
    candidate = Path.home() / ".lmstudio" / "bin" / exe
    return str(candidate) if candidate.exists() else None


class LMStudioControl:
    def __init__(self, lms_path: Optional[str] = None, runner: Runner = _run):
        self.lms = lms_path or find_lms()
        self._runner = runner

    def available(self) -> bool:
        return self.lms is not None

    def loaded_models(self) -> List[dict]:
        result = self._runner([self.lms, "ps", "--json"])
        if result.returncode != 0:
            raise RuntimeError(f"lms ps failed: {result.stderr.strip() or result.stdout.strip()}")
        return [m for m in json.loads(result.stdout or "[]") if m.get("type", "llm") == "llm"]

    def unload_all(self) -> None:
        result = self._runner([self.lms, "unload", "--all"])
        if result.returncode != 0:
            raise RuntimeError(f"lms unload --all failed: {result.stderr.strip() or result.stdout.strip()}")

    def reload(self, model: dict) -> None:
        args = [self.lms, "load", model["modelKey"], "-y"]
        if model.get("contextLength"):
            args += ["--context-length", str(model["contextLength"])]
        if model.get("parallel"):
            args += ["--parallel", str(model["parallel"])]
        if model.get("identifier") and model["identifier"] != model["modelKey"]:
            args += ["--identifier", model["identifier"]]
        if model.get("ttlMs"):
            args += ["--ttl", str(model["ttlMs"] // 1000)]
        result = self._runner(args)
        if result.returncode != 0:
            raise RuntimeError(
                f"lms load {model['modelKey']} failed: {result.stderr.strip() or result.stdout.strip()}"
            )

    @contextmanager
    def models_unloaded(self, log: Callable[[str], None] = print) -> Iterator[None]:
        """Unloads every loaded LLM for the duration of the block and reloads
        them afterwards -- also when the block raises, so a failed Laya step
        never leaves the user without their LLM. Reload failures are
        reported per model and don't stop the others from reloading."""
        if not self.available():
            log("UNLOAD_LLM_BEFORE_LAYA is set but LM Studio's `lms` CLI wasn't found -- "
                "running Laya without unloading.")
            yield
            return

        snapshot = self.loaded_models()
        if snapshot:
            log(f"Unloading {len(snapshot)} LM Studio model(s) for Laya: "
                + ", ".join(m.get("identifier") or m["modelKey"] for m in snapshot))
            self.unload_all()
        try:
            yield
        finally:
            for model in snapshot:
                try:
                    self.reload(model)
                    log(f"Reloaded {model.get('identifier') or model['modelKey']} "
                        f"(context {model.get('contextLength')}).")
                except Exception as e:
                    log(f"Error: could not reload {model['modelKey']} into LM Studio: {e}")
