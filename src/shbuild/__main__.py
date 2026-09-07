"""Allow ``python -m shbuild`` as an alias for the console script."""

from shbuild.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
