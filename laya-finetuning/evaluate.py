"""Score checkpoints on the held-out test set with the judge's real questions.

    uv run --extra laya python laya-finetuning/evaluate.py
    uv run --extra laya python laya-finetuning/evaluate.py --checkpoint english=laya-finetuning/checkpoints/jfi-judge-english

Reports two numbers per checkpoint:
- raw:   Laya's own top answer vs the label;
- judge: what JFI.planner.judge actually decides (confidence gate + fallback rule).
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from laya import Router  # noqa: E402

from JFI.planner.judge import (  # noqa: E402
    _REDO_REASON_BY_KEY, _STATUS_BY_KEY, JudgeNode, build_state, fallback_status, questions_for,
)


def load_rows(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


STATUSES = ("GOOD", "BREAKDOWN", "REDO")


def print_matrix(matrix):
    """Rows = the label, columns = Laya's raw answer; recall per row, precision per column."""
    print(f"    {'want / got':<12}" + "".join(f"{s:>11}" for s in STATUSES) + f"{'total':>8}{'recall':>8}")
    for want in STATUSES:
        row = [matrix[(want, got)] for got in STATUSES]
        total = sum(row)
        recall = matrix[(want, want)] / total if total else 0
        print(f"    {want:<12}" + "".join(f"{n:>11}" for n in row) + f"{total:>8}{recall:>8.0%}")
    precision = []
    for got in STATUSES:
        col = sum(matrix[(want, got)] for want in STATUSES)
        precision.append(f"{(matrix[(got, got)] / col if col else 0):>11.0%}")
    print(f"    {'precision':<12}" + "".join(precision))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(HERE / "data" / "test.jsonl"))
    ap.add_argument("--checkpoint", action="append", default=[],
                    help="name=path to a fine-tuned checkpoint (name: english or typed-decisions); repeatable")
    ap.add_argument("--min-confidence", type=float, default=0.6)
    args = ap.parse_args()

    rows = load_rows(args.data)
    candidates = [("english (base)", "english", None), ("typed-decisions (base)", "typed-decisions", None)]
    for spec in args.checkpoint:
        name, path = spec.split("=", 1)
        candidates.append((f"{name} (fine-tuned: {Path(path).name})", name, path))

    for label, slot, path in candidates:
        router = Router(max_loaded=1, models={slot: path} if path else None)
        reqs = []
        for r in rows:
            node = JudgeNode(0, r["level"], r["node"], done_when=r.get("done_when", ""),
                             files=r.get("files", []), path=r.get("path", []))
            reqs.append({"state": build_state(r["goal"], node), "questions": questions_for(r["level"]),
                         "model": slot})
        results = router.predict_batch(reqs)
        raw_ok = judge_ok = reason_ok = redo_hits = 0
        matrix = Counter()
        per_level, errors = Counter(), Counter()
        for r, res in zip(rows, results):
            v = res["answers"]["verdict"]
            raw = _STATUS_BY_KEY[v["choice"]]
            judged = raw if v["answer_confidence"] >= args.min_confidence else fallback_status(r["level"])
            matrix[(r["label"], raw)] += 1
            raw_ok += raw == r["label"]
            judge_ok += judged == r["label"]
            per_level[r["level"]] += raw == r["label"]
            if raw != r["label"]:
                errors[(r["label"], raw)] += 1
            if r["label"] == "REDO" and raw == "REDO":
                redo_hits += 1
                reason_ok += _REDO_REASON_BY_KEY[res["answers"]["redo_reason"]["choice"]] == r["redo_reason"]
        n = len(rows)
        levels = Counter(r["level"] for r in rows)
        print(f"{label:<55} raw {raw_ok / n:.0%}  judge {judge_ok / n:.0%}  | "
              + " ".join(f"{lvl}={per_level[lvl] / levels[lvl]:.0%}" for lvl in ("architect", "lead", "task"))
              + f" | redo reason {reason_ok}/{redo_hits} | errors (want->got) {dict(errors)}")
        print_matrix(matrix)
        router.unload()


if __name__ == "__main__":
    main()
