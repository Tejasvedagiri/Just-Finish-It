"""`uv run build` must never bundle Laya's torch/transformers stack into
dist/jfi (laya_plan.md D22) -- checked on the PyInstaller arguments, without
running a build."""
from __future__ import annotations

import sys
import types

import build_binary


def test_build_excludes_laya_torch_and_transformers(monkeypatch):
    captured = {}
    fake_main = types.ModuleType("PyInstaller.__main__")
    fake_main.run = lambda args: captured.setdefault("args", args)
    fake_pkg = types.ModuleType("PyInstaller")
    fake_pkg.__main__ = fake_main
    monkeypatch.setitem(sys.modules, "PyInstaller", fake_pkg)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", fake_main)
    monkeypatch.setattr(sys, "platform", "linux")  # skip the macOS codesign step

    build_binary.main()

    args = captured["args"]
    excluded = {args[i + 1] for i, a in enumerate(args) if a == "--exclude-module"}
    assert {"laya", "torch", "transformers"} <= excluded
