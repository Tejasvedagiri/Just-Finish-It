"""`uv run laya-finetune`: the recommended judge fine-tune in one command.

Runs laya-finetuning/finetune.py with the settings that produced the current
recommended checkpoint (full fine-tune on CUDA), writing to
laya-finetuning/checkpoints/jfi-judge-english-full-v2. Any extra arguments
are passed through and override these defaults (argparse keeps the last
value), e.g. `uv run laya-finetune --epochs 5 --out /tmp/try`.

Lives in src/ only because a console script needs an importable module;
laya-finetuning/ (hyphenated, not a package) holds the real script.
"""
import runpy
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "laya-finetuning" / "finetune.py"
DEFAULTS = [
    "--base", "english", "--device", "cuda", "--train-encoder", "--epochs", "40", "--patience", "10",
    "--out", str(PROJECT_ROOT / "laya-finetuning" / "checkpoints" / "jfi-judge-english-full-v2"),
]


def main() -> None:
    try:
        import laya  # noqa: F401
    except ImportError:
        print("Laya isn't installed. Run: uv sync --extra laya --group dev", file=sys.stderr)
        raise SystemExit(1)
    sys.argv = [str(SCRIPT), *DEFAULTS, *sys.argv[1:]]
    runpy.run_path(str(SCRIPT), run_name="__main__")
