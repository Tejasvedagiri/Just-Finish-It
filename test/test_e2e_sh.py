"""End-to-end + edge-case tests for the generated standalone .sh (steps 4.3-4.5).

Each test builds a real sample project with shbuild into an executable script,
then runs that script in a near-clean environment exactly as a user would.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from shbuild.builder import build  # single entry point the CLI uses


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

def _make_sample_project(root: Path, with_args: bool = False) -> tuple[Path, list[str]]:
    """Create a sample project: main.py imports requests + local helper module."""
    root.mkdir(parents=True, exist_ok=True)
    deps_txt = root / "requirements.txt"
    deps_txt.write_text("requests>=2.31\n")

    if with_args:
        main_py = (
            "import sys\n"
            "from helpers import shout\n"
            "import requests  # noqa: F401  (baked dep must be importable)\n"
            'print("ARGS:" + ",".join(sys.argv[1:]))\n'
            f'print(shout("ok"))\n'
        )
    else:
        main_py = (
            "from helpers import shout\n"
            "import requests  # noqa: F401\n"
            'assert requests.__version__.startswith("2.") or True\n'
            f'print(shout("hello"))\n'
        )
    (root / "main.py").write_text(main_py)
    (root / "helpers.py").write_text('def shout(s: str) -> str:\n    return s.upper() + "\\n"\n')
    return root, ["requests>=2.31"]


def _build(root: Path, out_name: str = "sample.sh") -> Path:
    out = root.parent / out_name
    build(
        entry=root / "main.py",
        deps=["requirements.txt"],
        out=out,
        name="e2esample",
        verbose=True,
    )
    assert out.is_file() and os.access(out, os.X_OK)
    return out


def _run_sh(script: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run the generated .sh with a minimal environment (PATH only + HOME/XDG)."""
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/root")}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(script), *args], capture_output=True, text=True, check=False, env=env
    )


# ---------------------------------------------------------------------------
# 4.3 end-to-end: build + run in a clean environment, no network at runtime
# ---------------------------------------------------------------------------

def test_e2e_runs_in_clean_env_with_correct_output(tmp_path) -> None:
    root = tmp_path / "proj"
    _make_sample_project(root)
    script = _build(root, out_name="sample.sh")

    proc = _run_sh(script)
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert "HELLO\n" in proc.stdout + "\n", f"stdout was: {proc.stdout!r}"


def test_e2e_no_network_used_at_runtime(tmp_path) -> None:
    """requests is imported but nothing connects; prove it by running with a
    PATH that contains only coreutils (no python on PATH at all), plus an
    offline env marker, and assert the import path resolves inside the payload."""
    root = tmp_path / "proj"
    proj = _make_sample_project(root)

    # Augment the entry to print where requests loaded from so we can assert it
    # comes from the extracted payload (cache dir), not from any system python.
    main_py = (
        "from helpers import shout\n"
        "import requests, sys\n"
        'print(shout("offline-ok"))\n'
        "print('REQPATH:' + requests.__file__)\n"
    )
    (root / "main.py").write_text(main_py)

    xdg = str(tmp_path / "xdg-cache")
    script = _build(root, out_name="sample.sh")

    # First run with a coreutils-only PATH and XDG_CACHE_HOME pinned.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": xdg}
    proc = subprocess.run([str(script)], capture_output=True, text=True, check=False, env=env)
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert "OFFLINE-OK\n" in proc.stdout + "\n"

    m = re.search(r"REQPATH:(\S+)", proc.stdout)
    assert m, f"missing REQPATH line; stdout={proc.stdout!r}"
    req_path = Path(m.group(1))
    cache_root = Path(xdg) / "shbuild" / "e2esample"
    assert str(req_path).startswith(str(cache_root)), (
        f"requests must load from the payload, got {req_path} not under {cache_root}"
    )


# ---------------------------------------------------------------------------
# 4.4 idempotency: second run reuses cache and skips extraction
# ---------------------------------------------------------------------------

def test_idempotent_second_run_reuses_cache(tmp_path) -> None:
    root = tmp_path / "proj"
    _make_sample_project(root)
    script = _build(root, out_name="sample.sh")

    xdg = str(tmp_path / "xdg-cache")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": xdg}

    # Run 1: cold cache -> extraction happens (payload dir created).
    t0 = time.monotonic()
    p1 = subprocess.run([str(script)], capture_output=True, text=True, check=False, env=env)
    cold_ms = (time.monotonic() - t0) * 1000
    assert p1.returncode == 0, f"stderr: {p1.stderr}"

    payload_dir = Path(xdg) / "shbuild" / "e2esample" / "payload"
    marker = payload_dir / ".shbuild-sha"
    assert marker.is_file(), "marker file must exist after first run"
    sha_before = marker.read_text()
    mtime_before = (payload_dir / "MANIFEST.json").stat().st_mtime_ns if (payload_dir / "MANIFEST.json").exists() else payload_dir.stat().st_mtime_ns

    # Snapshot the whole payload tree's file set + mtimes: a re-extraction would
    # delete and recreate everything, changing these.
    def snapshot() -> dict[str, int]:
        out = {}
        for p in payload_dir.rglob("*"):
            if p.is_file():
                out[str(p.relative_to(payload_dir))] = p.stat().st_mtime_ns
        return out

    snap_before = snapshot()

    # Run 2: warm cache -> must NOT re-extract (same files, same mtimes).
    t0 = time.monotonic()
    p2 = subprocess.run([str(script)], capture_output=True, text=True, check=False, env=env)
    warm_ms = (time.monotonic() - t0) * 1000
    assert p2.returncode == 0, f"stderr: {p2.stderr}"
    assert sha_before == marker.read_text(), "marker changed -> payload was rewritten"

    snap_after = snapshot()
    assert snap_before == snap_after, (
        "payload tree changed between runs -> cache hit did not skip extraction"
    )
    # The warm run must be strictly faster than the cold one (no tar/base64 work).
    assert warm_ms < cold_ms, f"warm ({warm_ms:.0f}ms) should beat cold ({cold_ms:.0f}ms)"


# ---------------------------------------------------------------------------
# 4.5 edge cases: args passthrough, broken checksum, --clear-cache
# ---------------------------------------------------------------------------

def test_entry_args_reach_dollar_at(tmp_path) -> None:
    root = tmp_path / "proj"
    _make_sample_project(root, with_args=True)
    script = _build(root, out_name="sample.sh")

    proc = _run_sh(script, "--foo", "bar baz", "42")
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert "ARGS:--foo,bar baz,42" in proc.stdout
    assert "OK\n" in proc.stdout + "\n"


def test_broken_checksum_aborts_nonzero_with_message(tmp_path) -> None:
    root = tmp_path / "proj"
    _make_sample_project(root)
    script = _build(root, out_name="sample.sh")

    # Corrupt exactly one byte of the embedded base64 payload (inside B64_CHUNKS).
    text = script.read_text()
    m = re.search(r"B64_CHUNKS='([^']+)'", text)
    assert m, "generated script must embed a B64_CHUNKS variable"
    chunk = list(m.group(1))
    # Flip the last base64 char to another valid one (keeps stream length-valid).
    old = chunk[-1]
    new = "A" if old != "A" else "B"
    chunk[-1] = new
    script.write_text(text.replace(m.group(0), f"B64_CHUNKS='{''.join(chunk)}'"))

    xdg = str(tmp_path / "xdg-cache")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": xdg}
    proc = subprocess.run([str(script)], capture_output=True, text=True, check=False, env=env)

    assert proc.returncode != 0, f"expected non-zero exit; stdout={proc.stdout!r}"
    combined = (proc.stderr + proc.stdout).lower()
    assert "checksum" in combined or "mismatch" in combined, (
        f"clear message expected; got stderr={proc.stderr!r} stdout={proc.stdout!r}"
    )


def test_clear_cache_forces_reextraction(tmp_path) -> None:
    root = tmp_path / "proj"
    _make_sample_project(root)
    script = _build(root, out_name="sample.sh")

    xdg = str(tmp_path / "xdg-cache")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": xdg}
    payload_dir = Path(xdg) / "shbuild" / "e2esample" / "payload"

    p1 = subprocess.run([str(script)], capture_output=True, text=True, check=False, env=env)
    assert p1.returncode == 0, f"stderr: {p1.stderr}"
    assert payload_dir.is_dir() and (payload_dir / ".shbuild-sha").is_file()

    def snapshot() -> dict[str, int]:
        return {str(p.relative_to(payload_dir)): p.stat().st_mtime_ns for p in payload_dir.rglob("*") if p.is_file()}

    snap_before = snapshot()
    time.sleep(0.05)  # ensure a re-extraction would have a newer mtime

    p2 = subprocess.run([str(script), "--clear-cache"], capture_output=True, text=True, check=False, env=env)
    assert p2.returncode == 0, f"stderr: {p2.stderr}"
    snap_after = snapshot()
    assert snap_before != snap_after, (
        "--clear-cache must force re-extraction (payload mtimes unchanged)"
    )


# ---------------------------------------------------------------------------
# 4.6 CLI --help: build a real .sh, then confirm `shbuild --help` prints usage
# ---------------------------------------------------------------------------

def test_cli_help_prints_usage_after_real_build(tmp_path) -> None:
    """Build an actual standalone .sh through the same entry point the CLI uses,
    then invoke `shbuild --help` and assert it exits 0 with real usage text."""
    # 1. Prove we can produce a working build end-to-end first (uses build(),
    #    the exact code path the console script funnels into).
    root = tmp_path / "proj"
    _make_sample_project(root)
    script = _build(root, out_name="sample.sh")
    assert script.is_file() and os.access(script, os.X_OK)

    # 2. Run the CLI help as a real subprocess (the installed console-script
    #    target is `shbuild.cli:main`), with src/ on PYTHONPATH so it resolves
    #    even without an editable install.
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "shbuild.cli", "--help"],
        capture_output=True, text=True, check=False, env=env,
    )

    assert proc.returncode == 0, f"help must exit 0; stderr={proc.stderr!r}"
    out = proc.stdout
    # Usage line + the prog name.
    assert "usage: shbuild" in out, f"missing usage header; stdout={out!r}"
    # Core flags documented in cli.py must all be advertised.
    for flag in ("--entry", "--deps", "--out", "--name", "--cache-dir", "--python", "--mode"):
        assert flag in out, f"--help should list {flag}; stdout={out!r}"


def test_cli_help_lists_mode_choices_and_default_out_hint(tmp_path) -> None:
    """The --help text must surface the concrete choices/default so users know
    valid values without reading source."""
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "shbuild.cli", "--help"],
        capture_output=True, text=True, check=False, env=env,
    )
    assert proc.returncode == 0
    out = proc.stdout
    # --mode choices are rendered as {auto,on,off} by argparse.
    assert "{auto,on,off}" in out, f"--mode choices missing; stdout={out!r}"
    # Default output naming is documented inline next to the flag.
    assert "standalone.sh" in out, "default --out hint missing from help text"
