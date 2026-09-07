"""Configuration dataclasses shared by the shbuild CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BuildConfig:
    """Everything needed to build one standalone .sh file.

    Attributes:
        name: Logical build name; used in the runtime cache directory.
        entry: The Python script that becomes the entry point.
        deps: Raw dependency specifications (PEP 508 strings or, for a
            single item ending in ``.txt``/``.in``, a path to a requirements
            file whose lines are used instead).
        out: Destination path of the generated .sh script.
        cache_dir: Optional fixed runtime cache directory; when None the
            generated script derives one from XDG_CACHE_HOME / TMPDIR.
        python: Interpreter (path or name) used to create the baked venv.
        fallback_mode: ``"auto"``, ``"on"`` or ``"off"`` — controls whether the
            payload additionally carries a wheelhouse for an offline
            ``pip install --no-index`` at first run. ``auto`` enables it only
            when a dependency ships C extensions (non-relocatable wheels).
        verbose: When True, build steps print progress to stderr.
    """

    name: str
    entry: Path
    deps: tuple[str, ...] = field(default_factory=tuple)
    out: Path = Path("shbuild-output.sh")
    cache_dir: str | None = None
    python: str | None = None
    fallback_mode: str = "auto"
    verbose: bool = False
