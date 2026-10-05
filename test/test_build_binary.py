"""`uv run build` always bundles the web dashboard, bundles the other
optional extras only when asked for (--laya, --anthropic, --mysql,
--postgres, --all), refuses to build without one it needs, and keeps the rest
out even when they're synced. The user: "when i do uv run build. I should be
able give args --laya --anthropic etc. to add it to the build" / "web must be
included in all builds regardless." Checked on the PyInstaller arguments,
without running a build."""
from __future__ import annotations

import sys
import types

import pytest

import build_binary


def _capture_build(monkeypatch, missing=()):
    captured = {}
    fake_main = types.ModuleType("PyInstaller.__main__")
    fake_main.run = lambda args: captured.setdefault("args", args)
    fake_pkg = types.ModuleType("PyInstaller")
    fake_pkg.__main__ = fake_main
    monkeypatch.setitem(sys.modules, "PyInstaller", fake_pkg)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", fake_main)
    monkeypatch.setattr(sys, "platform", "linux")  # skip the macOS codesign step
    monkeypatch.setattr(build_binary, "_missing_extras", lambda extras: [e for e in extras if e in missing])
    monkeypatch.setattr(build_binary, "remove_stale_build", lambda dist: None)
    return captured


def _flag_values(args, *flags):
    return {args[i + 1] for i, a in enumerate(args) if a in flags}


def test_a_plain_build_is_the_core_and_web_with_every_other_extra_excluded(monkeypatch):
    """Excluded, not just not collected: torch in the venv would otherwise
    be pulled in through any stray import and make the binary several GB."""
    captured = _capture_build(monkeypatch)

    build_binary.main([])

    args = captured["args"]
    collected = _flag_values(args, "--collect-all", "--collect-submodules")
    assert {"websockets", "streamlit"} <= collected
    assert any(a.endswith("dashboard.py:JFI/web") for a in args)
    assert not collected & {"laya", "anthropic", "pymysql", "psycopg"}
    excluded = _flag_values(args, "--exclude-module")
    assert {"laya", "torch", "transformers", "anthropic", "pymysql", "psycopg"} <= excluded
    assert "streamlit" not in excluded
    # One file on every platform: a onedir executable is useless without its
    # _internal/ folder, which got left behind when the binary was copied out.
    assert "--onefile" in args and "--onedir" not in args
    # `jfi --version` printed "unknown" without the package's metadata.
    assert args[args.index("--copy-metadata") + 1] == "just-finish-it"


def test_requested_extras_are_bundled_and_the_rest_stay_excluded(monkeypatch):
    captured = _capture_build(monkeypatch)

    build_binary.main(["--laya", "--anthropic"])

    args = captured["args"]
    collected = _flag_values(args, "--collect-all", "--collect-submodules")
    assert {"laya", "transformers.models.modernbert", "streamlit", "anthropic", "websockets"} <= collected
    excluded = _flag_values(args, "--exclude-module")
    assert not excluded & {"laya", "torch", "transformers", "streamlit", "anthropic"}
    assert {"pymysql", "psycopg"} <= excluded


def test_all_bundles_every_extra(monkeypatch):
    captured = _capture_build(monkeypatch)

    build_binary.main(["--all"])

    args = captured["args"]
    assert "--exclude-module" not in args
    assert {"streamlit", "laya", "anthropic", "pymysql", "psycopg", "psycopg_binary"} <= _flag_values(
        args, "--collect-all")


def test_a_requested_extra_that_isnt_synced_stops_the_build_with_the_command_to_fix_it(monkeypatch, capsys):
    """A plain `uv sync` drops the extras; the build used to ship without
    them silently (observed: "No module named 'websockets'" from dist/jfi)."""
    captured = _capture_build(monkeypatch, missing=("laya",))

    with pytest.raises(SystemExit):
        build_binary.main(["--laya"])

    assert "args" not in captured
    err = capsys.readouterr().err
    assert "the laya extra(s)" in err and "uv sync --extra web --extra laya --group dev" in err


def test_web_is_required_even_when_not_asked_for(monkeypatch, capsys):
    captured = _capture_build(monkeypatch, missing=("web",))

    with pytest.raises(SystemExit):
        build_binary.main([])

    assert "args" not in captured
    assert "uv sync --extra web --group dev" in capsys.readouterr().err


def test_an_unknown_or_abbreviated_flag_is_refused(monkeypatch):
    """argparse's default would read `--lay` as --laya; `--web` is no flag
    at all since web is always in."""
    _capture_build(monkeypatch)
    for flag in ("--lay", "--web"):
        with pytest.raises(SystemExit):
            build_binary.main([flag])


def test_an_old_build_of_either_kind_is_removed_before_building(tmp_path):
    """Observed 2026-09-30: switching between folder and onefile builds left
    the other kind behind (the Windows onefile dist/jfi.exe, 2.2 GB, beside
    dist/jfi/), and PyInstaller won't write a file over a directory."""
    (tmp_path / "jfi").mkdir()
    (tmp_path / "jfi" / "jfi.exe").write_bytes(b"old onedir")
    (tmp_path / "jfi.exe").write_bytes(b"old onefile")
    build_binary.remove_stale_build(tmp_path)
    assert not (tmp_path / "jfi").exists() and not (tmp_path / "jfi.exe").exists()

    (tmp_path / "jfi").write_bytes(b"old posix onefile")
    build_binary.remove_stale_build(tmp_path)
    assert not (tmp_path / "jfi").exists()
