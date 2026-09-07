"""Payload assembly and build orchestration for shbuild.

The *payload* is a plain directory tree that the generated .sh script will
extract (from its embedded tar.gz) into a runtime cache dir before launching:

    payload/
      venv/                 # relocatable virtualenv (interpreter + stdlib + deps)
      requirements.lock     # pip-freeze snapshot of the baked dependency set
      wheelhouse/*.whl      # optional offline wheels (fallback mode only)
      app/<rel paths>       # entry script + discovered local modules
      MANIFEST.json         # machine-readable build metadata

``assemble_payload`` builds that tree; ``run_build`` chains discovery,
assembly and .sh generation into one call.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import sys

from .config import BuildConfig
from .discovery import discover, to_file_list
from .venv import VenvInfo, create_venv


def _log(verbose: bool, message: str) -> None:
    """Progress line for ``--verbose`` builds (step 4.1)."""
    if verbose:
        print(f"shbuild: {message}", file=sys.stderr, flush=True)


@dataclass
class BuildResult:
    """Outcome of a full shbuild invocation."""

    name: str
    entry: Path
    out: Path
    cache_dir: str | None
    python_version: str
    interpreter_name: str
    deps_installed: list[str] = field(default_factory=list)
    source_files: int = 0
    fallback_active: bool = False
    payload_size_bytes: int = 0


def assemble_payload(
    staging_dir: Path,
    entry: Path,
    deps: tuple[str, ...],
    python_spec: str | None = None,
    fallback_mode: str = "auto",
) -> VenvInfo:
    """Populate *staging_dir* with a complete shbuild payload tree.

    Steps performed here (the .sh generation happens later):

      1. Discover the entry script and its local modules via AST import
         scanning, then copy them under ``app/`` preserving relative paths.
      2. Create a relocatable venv at ``staging_dir/venv`` with all
         dependencies installed (see :func:`shbuild.venv.create_venv`).
      3. Write the dependency lock file into the payload root.

    Returns the :class:`~shbuild.venv.VenvInfo` describing the baked venv.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. Source files -------------------------------------------------
    project = discover(entry.resolve())
    app_root = staging_dir / "app"
    if app_root.exists():
        shutil.rmtree(app_root)
    for rel_posix, abs_path in to_file_list(project):
        dest = app_root / rel_posix
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(abs_path, dest)

    # --- 2. Baked venv (+ lock file, optional wheelhouse) -----------------
    info = create_venv(
        staging_dir,
        python_spec=python_spec,
        deps=list(deps),
        fallback_mode=fallback_mode,
    )

    # --- 3. Manifest -------------------------------------------------------
    manifest = {
        "name": entry.stem,
        "entry": rel_entry_path(entry.resolve(), project.root),
        "python_version": info.python_version,
        "deps_installed": info.deps_installed,
        "source_files": len(project.files),
        "fallback_active": info.fallback_active,
    }
    (staging_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    return info


def rel_entry_path(entry: Path, root: Path) -> str:
    """Root-relative POSIX path of the entry script (e.g. ``main.py``)."""
    try:
        return entry.resolve().relative_to(root).as_posix()
    except ValueError:
        return entry.name


def _payload_size(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            fp = Path(dirpath) / fn
            if not fp.is_symlink():
                total += fp.stat().st_size
    return total


def run_build(
    config: BuildConfig,
    generate_sh=None,
    payload_dir: Path | None = None,
) -> tuple[BuildResult, VenvInfo]:
    """Run the full build pipeline for *config*.

    1. Stage a temporary payload directory (or reuse *payload_dir* when given).
    2. Assemble the payload tree (:func:`assemble_payload`).
    3. Generate the standalone .sh script (delegated to *generate_sh*; defaults
       to :func:`shbuild.shgen.generate` once step 3.2 lands — a tiny inline
       placeholder keeps this module usable in isolation).
    4. Remove the temporary payload dir and return the result + venv info.

    Returns:
        A ``(BuildResult, VenvInfo)`` tuple. The caller (the CLI) is expected
        to surface ``result.out`` to the user.
    """
    if generate_sh is None:
        from .shgen import generate as _default_generate

        generate_sh = _default_generate

    tmp_ctx = None
    staging = payload_dir
    if staging is not None:
        staging.mkdir(parents=True, exist_ok=True)
    else:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="shbuild-")
        staging = Path(tmp_ctx.name) / "payload"

    try:
        info = assemble_payload(
            staging,
            entry=config.entry,
            deps=config.deps,
            python_spec=config.python,
            fallback_mode=config.fallback_mode,
        )
        size = _payload_size(staging)
        result = BuildResult(
            name=config.name,
            entry=config.entry,
            out=config.out,
            cache_dir=config.cache_dir,
            python_version=info.python_version,
            interpreter_name=info.interpreter_name,
            deps_installed=list(info.deps_installed),
            source_files=json.loads((staging / "MANIFEST.json").read_text())["source_files"],
            fallback_active=info.fallback_active,
            payload_size_bytes=size,
        )

        generate_sh(config=config, staging_dir=staging, result=result)
        return result, info
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


def build(
    entry: Path,
    deps: list[str] | tuple[str, ...] = (),
    out: Path | None = None,
    name: str | None = None,
    cache_dir: str | None = None,
    python: str | None = None,
    fallback_mode: str = "auto",
    verbose: bool = False,
) -> BuildResult:
    """Convenience entry point that mirrors the CLI flags one-to-one.

    Thin wrapper around :func:`run_build` for callers (tests, scripts) that do
    not want to construct a :class:`~shbuild.config.BuildConfig` themselves.
    The build name defaults to the entry script's stem; ``out`` defaults to
    ``<entry-stem>-standalone.sh`` next to the entry script.
    """
    entry = Path(entry)
    if not entry.is_file():
        raise SystemExit(f"error: --entry {entry!s} does not exist")

    # Anchor a relative requirements-file spec to the project (the entry
    # script's directory) so builds work from any CWD.
    deps = list(deps)
    if len(deps) == 1 and Path(deps[0]).suffix.lower() in (".txt", ".in"):
        req_path = Path(deps[0])
        if not req_path.is_file():
            candidate = entry.parent / req_path
            if candidate.is_file():
                deps = [str(candidate)]

    stem = entry.stem.replace("-", "_")
    safe_name = (stem or "build").lstrip("_") or "build"
    default_out = entry.parent / f"{safe_name}-standalone.sh"

    config = BuildConfig(
        name=name or safe_name,
        entry=entry.resolve(),
        deps=tuple(deps),
        out=(out.resolve() if out is not None else default_out),
        cache_dir=cache_dir,
        python=python,
        fallback_mode=fallback_mode,
        verbose=verbose,
    )
    result, _info = run_build(config)
    return result


__all__ = [
    "BuildResult",
    "assemble_payload",
    "build",
    "run_build",
]


# ---------------------------------------------------------------------------
# 3.2 — base64 chunking for shell heredoc embedding
# ---------------------------------------------------------------------------
#: Chunks are kept short (<= ~1 KB) so embedded lines never hit shell/terminal
#: line-length limits and diffs stay readable.
B64_CHUNK_SIZE = 768  # bytes of *encoded* text per line


def b64_chunks(data: bytes, chunk_size: int = B64_CHUNK_SIZE) -> list[str]:
    """Return the base64 encoding of *data* split into fixed-size lines.

    The concatenation of all chunks (no newlines) is the full valid base64
    stream; individual chunks are arbitrary slices, so decoding must always
    be done on the *joined* stream in the wrapper.
    """
    import base64

    encoded = base64.b64encode(data).decode("ascii")
    return [encoded[i : i + chunk_size] for i in range(0, len(encoded), chunk_size)]


def b64_chunk_count(data: bytes, chunk_size: int = B64_CHUNK_SIZE) -> int:
    """Number of chunks produced by :func:`b64_chunks` (cheap estimate)."""
    import math

    encoded_len = 4 * math.ceil(len(data) / 3)
    return max(1, -(-encoded_len // chunk_size))
