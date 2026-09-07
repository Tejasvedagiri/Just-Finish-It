"""Tests for step 4.2: venv relocation after extraction to a different path."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from shbuild.venv import create_venv, make_relocatable


@pytest.fixture(scope="module")
def relocated_venv(tmp_path_factory) -> tuple[Path, str]:
    """Build a real venv with one pure-Python dep + a console script, then
    copy it to a *different* path (simulating payload extraction)."""
    staging = tmp_path_factory.mktemp("reloc")
    info = create_venv(staging, deps=["six"], fallback_mode="off")

    # Simulate "extraction" at an unrelated absolute path.
    dest = tmp_path_factory.mktemp("elsewhere", numbered=True) / "deep" / "cache" / "payload" / "venv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Copy child-by-child so that symlinked top-level entries are preserved as
    # symlinks (a plain copytree of the whole venv would follow them).
    dest.mkdir(exist_ok=True)
    for child in info.root.iterdir():
        target = dest / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, symlinks=True, ignore_dangling_symlinks=True)
        elif child.is_symlink():
            os.symlink(os.readlink(child), target)
        else:
            shutil.copy2(child, target)

    # Install a console script into the relocated venv's site-packages so we can
    # verify shebang rewriting (the wrapper must exec the local interpreter).
    py = str(dest / "bin" / info.interpreter_name)
    sp = next((dest / "lib").glob("python*/site-packages"))

    def run(cmd: list[str]) -> None:
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    # six ships a `six`-style import; add a tiny dist providing a console entry.
    pkg = sp / "shbuildtest"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("VALUE = 'from-shbuild'\n")
    (sp / "shbuildtest-0.1.dist-info").mkdir()
    (sp / "shbuildtest-0.1.dist-info/METADATA").write_text(
        "Metadata-Version: 2.1\nName: shbuildtest\nVersion: 0.1\n"
    )

    # Write a console script exactly as pip would, then relocate shebangs.
    cs = dest / "bin" / "shbuild-test-cmd"
    shebang = f"#!{py}\n"
    body = (
        "# -*- coding: utf-8 -*-\n"
        'import re\n'
        "def importlib_load():\n"
        "    pass\n"
        "from shbuildtest import VALUE\n"
        "print(VALUE)\n"
        "if __name__ == '__main__':\n"
        "    print(VALUE)\n"
    )
    cs.write_text(shebang + body)

    # Re-run shebang rewriting on this manually-added script, exactly like the
    # build pipeline does for every bin/* entry.
    version = info.python_version
    interp_name = f"python{version}"
    data = cs.read_bytes()
    assert data.startswith(b"#!")
    nl = data.find(b"\n")
    rest = data[nl + 1 :]
    body_path = dest / "bin" / (cs.name + ".py")
    body_path.write_bytes(rest)
    wrapper = (
        f'#!/bin/sh\n'
        f'exec "$(dirname "$0")/{interp_name}" "$(dirname "$0")/{cs.name}.py" "$@"\n'
    ).encode("utf-8")
    cs.write_bytes(wrapper)

    return dest, info.python_version


def test_import_pure_python_dep_after_relocation(relocated_venv) -> None:
    """`venv/bin/python -c 'import <dep>'` works from the new path."""
    dest, version = relocated_venv
    py = dest / "bin" / f"python{version}"
    assert (py).is_file() and not py.is_symlink(), "interpreter must be a real file copy"

    proc = subprocess.run(
        [str(py), "-c", "import six, shbuildtest; print(six.__name__, shbuildtest.VALUE)"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert "six from-shbuild" in proc.stdout


def test_console_script_shebang_resolution(relocated_venv) -> None:
    """A console script runs via its rewritten shebang (local interpreter)."""
    dest, version = relocated_venv
    cs = dest / "bin" / "shbuild-test-cmd"
    os.chmod(cs, 0o755)

    first_line = cs.read_bytes().split(b"\n", 1)[0]
    assert first_line == b"#!/bin/sh", "console script must be re-wrapped for relocation"

    proc = subprocess.run([str(cs)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert "from-shbuild" in proc.stdout


def test_relative_python_symlinks(relocated_venv) -> None:
    """python3 / python point at the local interpreter via relative symlinks."""
    import os as _os

    dest, version = relocated_venv
    for name in ("python3", "python"):
        link = dest / "bin" / name
        assert link.is_symlink()
        target = Path(_os.readlink(link))
        assert not target.is_absolute(), f"{name} symlink must be relative"

    py3 = str(dest / "bin" / "python3")
    proc = subprocess.run(
        [py3, "-c", "import sys; print(sys.version.split()[0])"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert version in proc.stdout
