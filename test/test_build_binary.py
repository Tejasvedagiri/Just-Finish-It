"""`uv run build` bundles every optional part -- the Streamlit dashboard,
Laya (torch + transformers) and websockets -- and refuses to build without
them. The user: "Make sure you bundle laya and websocket" / "I need streamlit
too". Checked on the PyInstaller arguments, without running a build."""
from __future__ import annotations

import sys
import types

import pytest

import build_binary


def _capture_build(monkeypatch):
    captured = {}
    fake_main = types.ModuleType("PyInstaller.__main__")
    fake_main.run = lambda args: captured.setdefault("args", args)
    fake_pkg = types.ModuleType("PyInstaller")
    fake_pkg.__main__ = fake_main
    monkeypatch.setitem(sys.modules, "PyInstaller", fake_pkg)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", fake_main)
    monkeypatch.setattr(sys, "platform", "linux")  # skip the macOS codesign step
    return captured


def test_the_binary_bundles_streamlit_laya_and_websockets(monkeypatch):
    monkeypatch.setattr(build_binary, "_missing_extras", lambda: [])
    captured = _capture_build(monkeypatch)

    build_binary.main()

    args = captured["args"]
    collected = {args[i + 1] for i, a in enumerate(args) if a in ("--collect-all", "--collect-submodules")}
    assert {"streamlit", "laya", "websockets", "transformers.models.modernbert"} <= collected
    assert "--exclude-module" not in args


def test_a_missing_extra_stops_the_build_with_the_command_to_fix_it(monkeypatch, capsys):
    """A plain `uv sync` drops the extras; the build used to ship without
    them silently (observed: "No module named 'websockets'" from dist/jfi)."""
    monkeypatch.setattr(build_binary, "_missing_extras", lambda: ["laya"])
    captured = _capture_build(monkeypatch)

    with pytest.raises(SystemExit):
        build_binary.main()

    assert "args" not in captured
    assert "uv sync --extra web --extra laya --group dev" in capsys.readouterr().err
