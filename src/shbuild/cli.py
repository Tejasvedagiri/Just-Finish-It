"""Command-line interface for shbuild."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import BuildConfig


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser for the `shbuild` CLI."""
    parser = argparse.ArgumentParser(
        prog="shbuild",
        description=(
            "Build a Python project into a single standalone .sh file. "
            "The generated script embeds a virtualenv with all dependencies "
            "plus your source files, so it can run anywhere with zero setup."
        ),
    )

    parser.add_argument(
        "--name",
        default=None,
        help=(
            "Build name used for the runtime cache directory. "
            "Defaults to the entry script's stem."
        ),
    )

    parser.add_argument(
        "--entry",
        required=True,
        type=Path,
        help="Python script that becomes the entry point of the build.",
    )

    parser.add_argument(
        "--deps",
        nargs="*",
        default=[],
        metavar="REQ",
        help=(
            "Dependency specifications (PEP 508 strings such as 'requests>=2'). "
            "A single argument ending in .txt or .in is treated as a path to a "
            "requirements file."
        ),
    )

    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Output path of the generated standalone .sh script. "
            "Defaults to <entry-stem>-standalone.sh next to the entry script."
        ),
    )

    parser.add_argument(
        "--cache-dir",
        default=None,
        help=(
            "Fixed runtime cache directory baked into the generated script. "
            "When omitted, the script uses $XDG_CACHE_HOME/shbuild/<name> "
            "(falling back to a system temp dir)."
        ),
    )

    parser.add_argument(
        "--python",
        default=None,
        help=(
            "Interpreter (path or bare name) used to create the baked venv. "
            "Defaults to the interpreter running shbuild."
        ),
    )

    parser.add_argument(
        "--mode",
        choices=("auto", "on", "off"),
        default="auto",
        help=(
            "Fallback (wheelhouse) mode: 'auto' bakes a wheelhouse only when a "
            "dependency ships C extensions that may not relocate cleanly; "
            "'on' always bakes one; 'off' never does. Default: auto."
        ),
    )

    return parser


def _default_name(entry: Path) -> str:
    """Derive a safe build name from the entry script path."""
    stem = entry.stem.replace("-", "_")
    if not stem or not (stem[0].isalpha() or stem[0] == "_"):
        stem = f"build_{stem}"
    return stem


def config_from_args(args: argparse.Namespace) -> BuildConfig:
    """Validate parsed arguments and produce a :class:`BuildConfig`."""
    entry: Path = args.entry

    if not entry.exists():
        raise SystemExit(f"error: --entry {entry!s} does not exist")
    if not entry.is_file():
        raise SystemExit(f"error: --entry {entry!s} is not a file")
    try:
        entry = entry.resolve()
    except OSError as exc:  # pragma: no cover - odd filesystems
        raise SystemExit(f"error: cannot resolve --entry path: {exc}") from exc

    deps = list(args.deps)
    # A relative requirements-file spec is anchored to the project (the entry
    # script's directory), so builds work regardless of where shbuild runs from.
    if len(deps) == 1 and Path(deps[0]).suffix.lower() in (".txt", ".in"):
        req_path = Path(deps[0])
        if not req_path.is_file():
            candidate = entry.parent / req_path
            if candidate.is_file():
                deps = [str(candidate)]
            else:
                raise SystemExit(f"error: requirements file {req_path!s} does not exist")
    deps = tuple(deps)

    out: Path | None = args.out
    if out is None:
        out = entry.parent / f"{entry.stem}-standalone.sh"
    else:
        try:
            out = out.resolve()
        except OSError as exc:  # pragma: no cover - odd filesystems
            raise SystemExit(f"error: cannot resolve --out path: {exc}") from exc

    return BuildConfig(
        name=args.name or _default_name(entry),
        entry=entry,
        deps=deps,
        out=out,
        cache_dir=args.cache_dir,
        python=args.python,
        fallback_mode=args.mode,
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point registered as the `shbuild` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = config_from_args(args)
    except SystemExit as exc:
        # argparse raises with code 0 on -h; re-raise so behavior is preserved.
        if str(exc.code or "") == "":
            raise
        print(str(exc), file=sys.stderr)
        return 2

    try:
        from .builder import run_build

        result, _info = run_build(config)
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - surfaced to the user verbatim
        print(f"shbuild: build failed: {exc}", file=sys.stderr)
        return 1

    summary_lines = [f"Built standalone script: {result.out}"]
    if result.payload_size_bytes:
        summary_lines.append(
            f"  payload size:   {result.payload_size_bytes / (1024 * 1024):.2f} MiB "
            f"(compressed into the .sh)"
        )
    summary_lines.append(f"  source files:   {result.source_files}")
    if result.deps_installed:
        summary_lines.append("  dependencies:   " + ", ".join(result.deps_installed))
    else:
        summary_lines.append("  dependencies:   (none)")
    summary_lines.append(f"  python version: {result.python_version}")
    if result.fallback_active:
        summary_lines.append("  wheelhouse:     baked in (offline fallback active)")
    print("\n".join(summary_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
