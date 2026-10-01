#!/usr/bin/env python3
"""
Laya as a sizing judge, measured against what really happened.

For every plan node in the given projects' `.jfi/JFI.db`, Laya is asked one
yes/no question per token budget:

    state = {"input": "Task: <the node's description>\\nDescription: <its notes>",
             "goal": "Can this be solved with 20k tokens?"}
    questions = {"solvable": {"type": "noul", "instructions": <the same goal>}}

Ground truth is the plan's own outcome: a Task leaf was done by Dev in one
episode (yes), an Architect or Lead node had to be broken down (no). Planner
nodes with no children yet (a run still planning) have no outcome and are
skipped.

Why: the judge's own state (goal, level, path, node) gave Laya little to read
and its confidence never reached the tie-break (docs/feature-description.md,
Laya). This checks whether a plain task + "can it be done within N tokens?"
separates the two groups, and whether P(yes) rises with the budget.

Usage (needs the laya extra):
    uv run python benchmark/laya_poc.py D:/git/jfi-bench/calc D:/git/jfi-bench/react_counter \\
        --out docs/laya_poc.md
"""
import argparse
import json
import sqlite3
from pathlib import Path

BUDGETS = (20_000, 30_000, 40_000)
MODELS = ("english", "multilingual")


def load_nodes(project: Path) -> list[dict]:
    db = sqlite3.connect(f"file:{(project / '.jfi' / 'JFI.db').as_posix()}?mode=ro", uri=True)
    rows = db.execute("select id, parent_id, level, description, notes from leaf").fetchall()
    db.close()
    parents = {r[1] for r in rows if r[1] is not None}
    nodes = []
    for node_id, _, level, description, notes in rows:
        broken_down = node_id in parents
        if not broken_down and level != "task":
            continue
        nodes.append({"project": f"{project.parent.name}/{project.name}", "id": node_id, "level": level, "solvable": not broken_down,
                      "description": description, "notes": notes or ""})
    return nodes


def state_for(node: dict, goal: str) -> dict:
    text = f"Task: {node['description']}"
    if node["notes"]:
        text += f"\nDescription: {node['notes']}"
    return {"input": text, "goal": goal}


def goal_for(budget: int) -> str:
    return f"Can this be solved with {budget // 1000}k tokens?"


def auc(positives: list[float], negatives: list[float]) -> float:
    if not positives or not negatives:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives)
    return wins / (len(positives) * len(negatives))


def score(nodes: list[dict]) -> dict:
    from laya import Router

    router = Router(max_loaded=1, device="cpu")
    p_yes: dict = {}
    for model in MODELS:
        for budget in BUDGETS:
            goal = goal_for(budget)
            out = router.predict_batch([
                {"state": state_for(n, goal), "questions": {"solvable": {"type": "noul", "instructions": goal}},
                 "model": model} for n in nodes])
            p_yes[(model, budget)] = [r["answers"]["solvable"]["noul"] for r in out]
    return p_yes


def summary_rows(nodes: list[dict], p_yes: dict) -> list[str]:
    truth = [n["solvable"] for n in nodes]
    lines = ["| Checkpoint | Budget | Accuracy (P >= 0.5) | Best threshold (fitted here) | AUC | "
             "P(yes) Task leaves, median | P(yes) broken down, median | P(yes) range |",
             "|---|---|---|---|---|---|---|---|"]
    for (model, budget), ps in p_yes.items():
        accuracy = sum((p >= 0.5) == t for p, t in zip(ps, truth)) / len(ps)
        pos = sorted(p for p, t in zip(ps, truth) if t)
        neg = sorted(p for p, t in zip(ps, truth) if not t)
        best = max(sorted(set(ps)), key=lambda th: sum((p >= th) == t for p, t in zip(ps, truth)))
        best_acc = sum((p >= best) == t for p, t in zip(ps, truth)) / len(ps)
        lines.append(f"| {model} | {budget // 1000}k | {accuracy:.0%} | P >= {best:.3f}: {best_acc:.0%} | "
                     f"{auc(pos, neg):.2f} | "
                     f"{pos[len(pos) // 2]:.4f} | {neg[len(neg) // 2]:.4f} | {min(ps):.4f}-{max(ps):.4f} |")
    return lines


def rising_share(nodes: list[dict], p_yes: dict) -> list[str]:
    lines = []
    for model in MODELS:
        rising = sum(p_yes[(model, BUDGETS[0])][i] <= p_yes[(model, BUDGETS[1])][i] <= p_yes[(model, BUDGETS[2])][i]
                     for i in range(len(nodes)))
        lines.append(f"- {model}: P(yes) rises (or holds) from 20k to 30k to 40k on {rising} of {len(nodes)} nodes.")
    return lines


def node_rows(nodes: list[dict], p_yes: dict) -> list[str]:
    head = "| Project | Node | Level | Truth | Task | " + " | ".join(
        f"{m[:2]} {b // 1000}k" for m in MODELS for b in BUDGETS) + " |"
    lines = [head, "|" + "---|" * (5 + len(MODELS) * len(BUDGETS))]
    order = sorted(range(len(nodes)), key=lambda i: (nodes[i]["project"], nodes[i]["id"]))
    for i in order:
        n = nodes[i]
        task = n["description"].replace("|", "\\|")
        task = task if len(task) <= 90 else task[:87] + "..."
        cells = " | ".join(f"{p_yes[(m, b)][i]:.3f}" for m in MODELS for b in BUDGETS)
        lines.append(f"| {n['project']} | {n['id']} | {n['level']} | {'yes' if n['solvable'] else 'no'} | "
                     f"{task} | {cells} |")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("projects", nargs="+", type=Path, help="project folders that have a .jfi/JFI.db")
    parser.add_argument("--out", type=Path, help="write the Markdown report here (default: print it)")
    args = parser.parse_args()

    nodes = [n for project in args.projects for n in load_nodes(project)]
    p_yes = score(nodes)
    solvable = sum(n["solvable"] for n in nodes)
    sample = next(n for n in nodes if n["solvable"] and n["notes"])
    report = [
        "# Laya as a sizing judge (POC)",
        "",
        f"Generated by `benchmark/laya_poc.py` from {len(args.projects)} project(s): "
        + ", ".join(f"`{p}`" for p in args.projects) + ".",
        "",
        f"{len(nodes)} plan nodes: {solvable} Task leaves that Dev finished in one episode (truth: yes), "
        f"{len(nodes) - solvable} Architect/Lead nodes that had to be broken down (truth: no).",
        "",
        "Each node is sent once per budget and checkpoint, as a yes/no (`noul`) question:",
        "",
        "```python",
        f"state = {json.dumps(state_for(sample, goal_for(20_000)), indent=4, ensure_ascii=False)}",
        f"questions = {json.dumps({'solvable': {'type': 'noul', 'instructions': goal_for(20_000)}})}",
        "```",
        "",
        "AUC is how well P(yes) ranks the Task leaves above the broken-down nodes: 0.5 is no signal, 1.0 "
        "is perfect. JFI's tie-break needs a confidence of 0.75 or more.",
        "",
        "## Summary",
        "",
        *summary_rows(nodes, p_yes),
        "",
        "Does a bigger budget make Laya more willing to say yes?",
        "",
        *rising_share(nodes, p_yes),
        "",
        "## Every node",
        "",
        "P(yes) per checkpoint (`en`, `mu`) and budget. Truth: `yes` = Dev finished it in one episode, "
        "`no` = it was broken down.",
        "",
        *node_rows(nodes, p_yes),
        "",
    ]
    text = "\n".join(report)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
