"""Baked virtualenv creation and relocation for shbuild.

The payload stores a single ``venv/`` directory that can be executed from any
filesystem path after extraction:

- ``venv/bin/pythonX.Y`` is the *actual* interpreter binary (a copy of the
  base install's binary), with ``python3`` / ``python`` as relative symlinks.
- The standard library (stdlib zip, .py sources and lib-dynload) lives in
  ``venv/lib/pythonX.Y/`` + ``venv/lib/pythonX.Y/lib-dynload/`` so a venv whose
  ``pyvenv.cfg`` has an empty ``home =`` can bootstrap itself.
- Installed third-party packages live in the usual site-packages directory.

This makes every path inside the payload relative to its own root, which is
what allows the generated .sh to extract it into an arbitrary cache dir and
run ``<cache>/venv/bin/python <entry>`` with no external state.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class VenvInfo:
    """Metadata about a built venv."""

    root: Path  # the relocatable venv directory (inside the payload staging dir)
    python_version: str  # e.g. "3.12"
    interpreter_name: str  # e.g. "python3.12"
    deps_installed: list[str] = field(default_factory=list)
    lock_path: Path | None = None  # requirements.lock written into the payload staging dir
    wheelhouse_dir: Path | None = None  # staged wheels/ (fallback mode only)
    fallback_active: bool = False  # True when the payload carries a wheelhouse


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def _emit(verbose: bool, message: str) -> None:
    """Progress line for ``--verbose`` builds (step 4.1)."""
    if verbose:
        print(f"shbuild: {message}", file=sys.stderr, flush=True)


def find_base_interpreter(python_spec: str | None) -> Path:
    """Resolve the interpreter (path or bare name) to an absolute executable."""
    if python_spec:
        path = shutil.which(python_spec)
        if path is None:
            # Treat as a direct path.
            p = Path(python_spec).expanduser()
            if p.is_file():
                return p.resolve()
            raise FileNotFoundError(f"interpreter not found: {python_spec!r}")
        return Path(path).resolve()
    # Default to the interpreter running shbuild itself.
    exe = Path(sys.executable).resolve()
    # If we run inside a venv, prefer the base interpreter so the baked venv
    # does not nest one venv inside another's site-packages layout.
    try:
        with open(exe.parent.parent / "pyvenv.cfg") as fh:  # noqa: SIM105
            for line in fh:
                if line.startswith("home ="):
                    home = Path(line.split("=", 1)[1].strip())
                    base_candidates = [home / exe.name]
                    for cand in base_candidates:
                        if cand.is_file():
                            return cand.resolve()
    except OSError:
        pass
    return exe


def _venv_python(base: Path) -> str | None:
    """Return the version suffix (e.g. '3.12') of a python binary name."""
    m = re.search(r"python(\d+\.\d+)", base.name)
    if not m:
        return None
    return m.group(1)


def expand_requirements(raw: list[str] | tuple[str, ...]) -> list[str]:
    """Expand raw CLI dependency specs into a flat PEP 508 requirement list.

    A single spec ending in ``.txt`` or ``.in`` is treated as a requirements
    file: its non-comment lines are used (one requirement per line). Anything
    else is passed through verbatim as a PEP 508 string.
    """
    reqs: list[str] = []
    for spec in raw:
        if len(raw) == 1 and Path(spec).suffix.lower() in (".txt", ".in"):
            path = Path(spec)
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip() if not line.lstrip().startswith("-") else ""
                if line:
                    reqs.append(line)
        elif spec.strip():
            reqs.append(spec.strip())
    return reqs


def create_venv(
    staging_dir: Path,
    python_spec: str | None = None,
    deps: list[str] | None = None,
    fallback_mode: str = "auto",
    verbose: bool = False,
) -> VenvInfo:
    """Create a build venv in staging_dir/venv and make it relocatable.

    Steps:
      1. ``python -m venv`` with pip at ``staging_dir/venv``.
      2. Install ``deps`` (PEP 508 strings) via the venv's pip.
      3. Relocate: copy the base interpreter binary + stdlib into the venv,
         rewrite ``pyvenv.cfg`` to an empty ``home =``, drop absolute
         symlinks/shebangs in favor of relative ones, and clear __pycache__.

    Returns a :class:`VenvInfo` describing the result.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    venv_root = staging_dir / "venv"
    base = find_base_interpreter(python_spec)
    version = _venv_python(base)
    if not version:
        raise RuntimeError(f"cannot determine python version from {base.name!r}")

    _emit(verbose, f"creating virtualenv with {base.name} (python {version})")

    # 1. Create the venv (with pip).
    if venv_root.exists():
        shutil.rmtree(venv_root)
    proc = subprocess.run([str(base), "-m", "venv", str(venv_root)])
    if proc.returncode != 0:
        raise RuntimeError(f"failed to create venv with {base}: {proc.stderr}")

    # 2. Install dependencies (if any). Requirements may include a single
    #    requirements-file path, which we expand here into plain specs.
    deps = expand_requirements(deps or [])
    installed: list[str] = []
    if deps:
        _emit(verbose, f"installing {len(deps)} requirement(s): " + ", ".join(deps))
        installed = run_pip_install(venv_root, version, deps)

    # 3. Relocate the venv so it runs from any path.
    _emit(verbose, "making virtualenv relocatable (embedding interpreter + stdlib)")
    make_relocatable(venv_root, base, version)

    # 4. Snapshot exactly what got installed into requirements.lock so the
    #    payload carries an auditable, reproducible dependency record. This is
    #    done *after* relocation so pip freeze sees the final venv state.
    lock_path: Path | None = None
    wheelhouse_dir: Path | None = None
    fallback_active = False
    if deps:
        from .wheelhouse import needs_fallback, write_lock_file

        lock_path, _pinned = write_lock_file(staging_dir, venv_root, version)

        # 5. Decide whether the payload also carries a wheelhouse for an
        #    offline `pip install --no-index` at first run (fallback mode).
        if fallback_mode == "on" or (fallback_mode == "auto"):
            required, ext_dists = needs_fallback(venv_root, version)
            if fallback_mode == "auto":
                fallback_active = bool(required)
            else:
                fallback_active = True
            if fallback_mode != "off" and fallback_active:
                _emit(verbose, "building offline wheelhouse (fallback mode)")
                from .wheelhouse import build_wheelhouse

                wh_info = build_wheelhouse(staging_dir, venv_root, version)
                wheelhouse_dir = wh_info.wheelhouse_dir
    return VenvInfo(
        root=venv_root,
        python_version=version,
        interpreter_name=f"python{version}",
        deps_installed=installed,
        lock_path=lock_path,
        wheelhouse_dir=wheelhouse_dir,
        fallback_active=fallback_active,
    )


def _base_python_dir(base: Path) -> Path:
    """Directory containing the real interpreter binary (the bin/ dir)."""
    resolved = base.resolve()
    # venvs point into their home; resolve through symlinks to get there.
    return resolved.parent


def _base_install_root(base: Path) -> Path:
    """Install root of the base python (bin/, lib/, share/ live under it)."""
    return _base_python_dir(base).parent


def make_relocatable(venv_root: Path, base: Path, version: str) -> None:
    """Rewrite an existing venv so it is self-contained and path-independent."""
    bin_dir = venv_root / "bin"
    lib_root = venv_root / "lib"
    target_lib = lib_root / f"python{version}"

    bin_base_dir = _base_python_dir(base)
    install_root = _base_install_root(base)

    # 3a. Copy the real interpreter binary into bin/ (replacing any symlink).
    interp_name = f"python{version}"
    base_bin = bin_base_dir / interp_name
    if not base_bin.exists():
        # Some installs only have python3; find whatever the venv symlink points to.
        link = bin_dir / interp_name
        if link.is_symlink():
            base_bin = Path(os.readlink(link))
    target_bin = bin_dir / interp_name
    try:
        # Remove a symlink so the copy lands on a fresh regular file.
        target_bin.unlink()
    except FileNotFoundError:
        pass
    shutil.copy2(base_bin, target_bin)
    os.chmod(target_bin, 0o755)

    # Relative convenience symlinks: python3 -> pythonX.Y (and python if absent).
    for name in ("python3", "python"):
        p = bin_dir / name
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        os.symlink(interp_name, p)

    # 3b. Bring the standard library next to the binary so an empty ``home =``
    #     in pyvenv.cfg can bootstrap (base_prefix == venv root).
    src_lib = install_root / "lib" / f"python{version}"
    if src_lib.exists():
        # Merge the stdlib next to site-packages (which already lives in
        # target_lib thanks to the venv layout). dirs_exist_ok keeps both.
        shutil.copytree(src_lib, target_lib, dirs_exist_ok=True)

    # Copy the shared libpython so ``ctypes.util.find_library`` and modules
    # that dlopen it resolve inside the payload (avoids a stderr warning).
    for so in sorted(install_root.glob("lib/libpython*.so*")):
        dest = lib_root / so.name
        if not dest.exists():
            shutil.copy2(so, dest)

    sp_src = target_lib / "site-packages"
    if not sp_src.exists():
        sp_src.mkdir(parents=True, exist_ok=True)

    # 3d. Rewrite pyvenv.cfg: empty home so the venv is standalone; keep version.
    cfg = venv_root / "pyvenv.cfg"
    lines = []
    for line in cfg.read_text().splitlines():
        if line.startswith(("home =", "executable =", "command =")):
            continue
        lines.append(line)
    lines.insert(0, "home =")
    if not any(l.startswith("version =") for l in lines):
        lines.append(f"version = {version}")
    cfg.write_text("\n".join(lines) + "\n")

    # 3e. Rewrite shebangs in bin/* scripts so console scripts run with the
    #     local interpreter from wherever the payload was extracted to. POSIX
    #     ``#!`` paths must be absolute, and extraction happens at runtime, so
    #     keep the original body next to it as <name>.py and replace the entry
    #     point with a tiny sh shim: exec "$(dirname "$0")/pythonX.Y"
    #     "$(dirname "$0")/<name>.py" "$@". (Prepending the sh line directly to
    #     the Python body would make line 2 get parsed as Python.)
    for script in sorted(bin_dir.iterdir()):
        if script.name.startswith(("python", "activate")) or script.suffix != "":
            continue
        try:
            data = script.read_bytes()
        except OSError:
            continue
        if not data.startswith(b"#!"):
            continue
        nl = data.find(b"\n")
        rest = data[nl + 1 :] if nl >= 0 else b""
        body_path = bin_dir / (script.name + ".py")
        body_path.write_bytes(rest)
        wrapper = (
            f'#!/bin/sh\n'
            f'exec "$(dirname "$0")/{interp_name}" "$(dirname "$0")/{script.name}.py" "$@"\n'
        ).encode("utf-8")
        script.write_bytes(wrapper)

    # 3f. Clear compiled caches to avoid stale absolute-path .pyc entries.
    for pycache in list(venv_root.rglob("__pycache__")):
        shutil.rmtree(pycache, ignore_errors=True)


def _top_level_modules(venv_root: Path, version: str) -> list[str]:
    """Collect the top-level importable modules of all installed packages.

    Reads each distribution's ``dist-info/top_level.txt``; falls back to a
    normalized form of the dist name when that file is missing.
    """
    sp = venv_root / "lib" / f"python{version}" / "site-packages"
    modules: list[str] = []
    for dist_info in sorted(sp.glob("*.dist-info")):
        top_level = dist_info / "top_level.txt"
        if top_level.is_file():
            for line in top_level.read_text().splitlines():
                name = line.strip()
                # Skip non-module entries (data dirs, namespace fragments).
                if name and not name.startswith(".") and "/" not in name:
                    modules.append(name)
        else:
            dist_name = dist_info.name.split("-")[0]
            modules.append(dist_name.replace("-", "_"))
    return modules


def smoke_test(info: VenvInfo) -> None:
    """Verify the relocated venv actually runs and can import installed deps.

    The check is done on a *copy* of the venv placed at a different path, which
    is exactly what happens when the generated .sh extracts its payload — so a
    pass here proves real relocation, not just a local-path coincidence.
    """
    dest = info.root.parent / f".venv-smoke-{info.interpreter_name}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(info.root, dest)
    py = str(dest / "bin" / info.interpreter_name)

    def _clean(err: str) -> str:
        # The interpreter prints a harmless "Could not find platform dependent
        # libraries <exec_prefix>" line when running from an arbitrary path; it is
        # only cosmetic and does not affect import behavior.
        return "\n".join(l for l in (err or "").splitlines() if "<exec_prefix>" not in l)

    try:
        proc = subprocess.run(
            [py, "-c", "import sys; print(sys.prefix); import site"],
            capture_output=True,
            text=True,
            cwd=dest.parent,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"relocated venv smoke test failed:\n{_clean(proc.stderr)}")

        modules = _top_level_modules(dest, info.python_version)
        for mod in sorted(set(modules)):
            p2 = subprocess.run(
                [py, "-c", f"import {mod}"],
                capture_output=True,
                text=True,
                cwd=dest.parent,
            )
            if p2.returncode != 0:
                raise RuntimeError(f"smoke test import failed for module {mod!r}:\n{_clean(p2.stderr)}")
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def run_pip_install(venv_root: Path, version: str, requirements: list[str]) -> list[str]:
    """Install a list of PEP 508 requirement strings into the venv via pip.

    Returns the distribution names+versions pip reports as installed
    (e.g. ``["requests-2.31.0", "charset_normalizer-3.4.0"]``).
    """
    if not requirements:
        return []
    py = str(venv_root / "bin" / f"python{version}")
    proc = _run([py, "-m", "pip", "install", "--no-cache-dir", *requirements])
    installed: list[str] = []
    for line in (proc.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Successfully installed"):
            # Format: "Successfully installed pkg1-1.0 pkg2-2.0 ..." (no colon).
            rest = stripped[len("Successfully installed") :].strip()
            installed.extend(rest.split())
    return installed
