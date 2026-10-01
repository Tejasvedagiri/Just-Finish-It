#!/usr/bin/env python3
"""
Scores one finished (or abandoned) JFI benchmark session by combining:
  1. The objective outcome from harness.py's bench_results.json (did the
     task's hidden/held-out verify.command actually pass?).
  2. Process metrics read from the project's own database, `.jfi/JFI.db`
     (the `Leaf`, `Episode`, `PlannerVerdict`, `PlanEvent` and
     `HistoryMessage` tables), read-only with the standard library's sqlite3,
     so this runs with any Python, not only JFI's venv.

Why these metrics: each one tracks a failure seen on real runs.
  - plan: nodes per level, leaves finished / skipped / left, the deepest
    leaf. A leaf left unfinished means imp never completed.
  - judge: how the planner's nodes were settled (agree / rule / llm) and how
    many redos and escalations it took.
  - episodes: per role, and how they ended. `budget` and `turn_cap` endings
    are overflows -- on the calc run a finished leaf was re-split after its
    episode hit the turn cap.
  - dev: attempts, reopened leaves (the reviewer's fix loop) and deferrals.
  - tool_call_error_rate: the fraction of tool results starting with
    "Error" -- on the QA machine add_node alone failed 28% of the time.
  - hidden_tests: whether a task's hidden_tests_dir survived the run (the
    cleanup phase has mv/rm).

Usage:
    python3 score.py <projects_root or project_dir> [--results bench_results.json]
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import harness

PHASES = ("planner", "imp", "reviewer", "cleanup")
OVERFLOW_ENDINGS = ("budget", "turn_cap")


def _connect(project_dir: Path) -> Optional[sqlite3.Connection]:
    db = project_dir / ".jfi" / "JFI.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _session_id(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The harness names the session after the task; otherwise the latest."""
    row = conn.execute("SELECT session_id FROM sessionrecord WHERE session_id = ?", (task_id,)).fetchone()
    if row is None:
        row = conn.execute("SELECT session_id FROM sessionrecord ORDER BY updated_at DESC LIMIT 1").fetchone()
    return row["session_id"] if row else None


def _plan_metrics(conn: sqlite3.Connection, sid: str) -> dict:
    nodes = conn.execute("SELECT id, parent_id, level, status, redo_count, escalation_count FROM leaf "
                         "WHERE session_id = ?", (sid,)).fetchall()
    by_id = {n["id"]: n for n in nodes}
    parents = {n["parent_id"] for n in nodes if n["parent_id"] is not None}
    leaves = [n for n in nodes if n["id"] not in parents]

    def depth(node) -> int:
        d = 1
        while node["parent_id"] in by_id:
            node, d = by_id[node["parent_id"]], d + 1
        return d

    status = Counter((n["status"] or "").lower() for n in leaves)
    return {
        "nodes": len(nodes),
        "nodes_per_level": dict(Counter(n["level"] or "?" for n in nodes)),
        "leaves": len(leaves),
        "done": status.get("done", 0),
        "skipped": status.get("skipped", 0),
        "left": len(leaves) - status.get("done", 0) - status.get("skipped", 0),
        "max_depth": max((depth(n) for n in leaves), default=0),
        "redos": sum(n["redo_count"] or 0 for n in nodes),
        "escalations": sum(n["escalation_count"] or 0 for n in nodes),
    }


def _judge_metrics(conn: sqlite3.Connection, sid: str) -> dict:
    rows = conn.execute("SELECT decided_by, final_status FROM plannerverdict WHERE session_id = ?", (sid,)).fetchall()
    return {"verdicts": len(rows), "decided_by": dict(Counter(r["decided_by"] or "?" for r in rows)),
            "final": dict(Counter(r["final_status"] for r in rows))}


def _episode_metrics(conn: sqlite3.Connection, sid: str) -> dict:
    rows = conn.execute("SELECT role, turns, tokens, end_reason, started_at, ended_at FROM episode "
                        "WHERE session_id = ?", (sid,)).fetchall()
    endings = Counter(r["end_reason"] or "unfinished" for r in rows)
    started = min((r["started_at"] for r in rows), default=None)
    ended = max((r["ended_at"] for r in rows if r["ended_at"]), default=None)
    return {
        "episodes": len(rows),
        "per_role": dict(Counter(r["role"] for r in rows)),
        "endings": dict(endings),
        "overflows": sum(endings.get(e, 0) for e in OVERFLOW_ENDINGS),
        "turns": sum(r["turns"] or 0 for r in rows),
        "tokens": sum(r["tokens"] or 0 for r in rows),
        "first_episode_at": started,
        "last_episode_at": ended,
    }


def _dev_metrics(conn: sqlite3.Connection, sid: str) -> dict:
    events = Counter(r["type"] for r in conn.execute("SELECT type FROM planevent WHERE session_id = ?", (sid,)))
    row = conn.execute("SELECT SUM(attempt_count) AS attempts, SUM(reopened_count) AS reopened FROM leaf "
                       "WHERE session_id = ?", (sid,)).fetchone()
    return {"attempts": row["attempts"] or 0, "reopened": row["reopened"] or 0,
            "overflow_events": events.get("overflow", 0), "deferrals": events.get("skip", 0),
            "events": dict(events)}


def _tool_metrics(conn: sqlite3.Connection, sid: str) -> dict:
    counts: Counter = Counter()
    errors: Counter = Counter()
    for row in conn.execute("SELECT role, content, tool_calls, name FROM historymessage WHERE session_id = ? "
                            "AND role IN ('assistant', 'tool')", (sid,)):
        if row["role"] == "assistant" and row["tool_calls"]:
            for call in json.loads(row["tool_calls"]) or []:
                counts[(call.get("function") or {}).get("name") or "?"] += 1
        elif row["role"] == "tool":
            content = json.loads(row["content"]) if row["content"] else ""
            if isinstance(content, str) and content.lstrip().startswith("Error"):
                errors[row["name"] or "?"] += 1
    total = sum(counts.values())
    return {"tool_calls_total": total, "tool_call_errors": sum(errors.values()),
            "tool_call_error_rate": round(sum(errors.values()) / total, 3) if total else 0.0,
            "tool_name_counts": dict(counts.most_common()), "tool_errors_by_name": dict(errors.most_common())}


def _phases_completed(conn: sqlite3.Connection, sid: str) -> list:
    """From the `<PHASE>_COMPLETE` markers resume reads (session-level
    history rows, outside any episode)."""
    texts = [json.loads(r["content"]) for r in conn.execute(
        "SELECT content FROM historymessage WHERE session_id = ? AND episode_id IS NULL AND content LIKE ?",
        (sid, "%_COMPLETE%"))]
    return [p for p in PHASES if any(isinstance(t, str) and f"{p.upper()}_COMPLETE" in t for t in texts)]


def _hidden_tests_status(task_id: str, project_dir: Path) -> Optional[dict]:
    try:
        task = harness.load_task(task_id)
    except (FileNotFoundError, ValueError):
        return None
    hidden_dir = task.get("hidden_tests_dir")
    if not hidden_dir:
        return None
    path = project_dir / hidden_dir
    present = path.is_dir()
    return {"path": hidden_dir, "present": present, "empty": present and not any(path.iterdir())}


def score_session(project_dir: Path, task_id: str, verify_result: Optional[dict]) -> dict:
    report = {"task_id": task_id, "project_dir": str(project_dir), "objective_verify": verify_result,
              "hidden_tests": _hidden_tests_status(task_id, project_dir), "session_id": None,
              "phases_completed": [], "plan": None, "judge": None, "episodes": None, "dev": None, "tools": None}
    conn = _connect(project_dir)
    sid = _session_id(conn, task_id) if conn else None
    if sid:
        report.update(session_id=sid, phases_completed=_phases_completed(conn, sid), plan=_plan_metrics(conn, sid),
                      judge=_judge_metrics(conn, sid), episodes=_episode_metrics(conn, sid),
                      dev=_dev_metrics(conn, sid), tools=_tool_metrics(conn, sid))
    if conn:
        conn.close()
    report["flags"] = _flags(report)
    return report


def _flags(report: dict) -> list:
    flags = []
    if report["session_id"] is None:
        return ["no JFI session in .jfi/JFI.db -- JFI never started, or ran somewhere else"]
    missing = [p for p in PHASES if p not in report["phases_completed"]]
    if missing:
        flags.append(f"phases not completed: {', '.join(missing)}")
    plan, episodes, tools = report["plan"], report["episodes"], report["tools"]
    if plan["left"]:
        flags.append(f"{plan['left']} of {plan['leaves']} leaves never finished")
    if plan["skipped"]:
        flags.append(f"{plan['skipped']} leaves skipped (a split that couldn't break them up)")
    if episodes["overflows"] >= 3:
        flags.append(f"{episodes['overflows']} episodes ran out of budget or turns")
    if tools["tool_call_error_rate"] > 0.25:
        flags.append(f"{tools['tool_call_error_rate']:.0%} of tool calls failed")
    hidden = report["hidden_tests"]
    if hidden and (not hidden["present"] or hidden["empty"]):
        flags.append(f"hidden test dir '{hidden['path']}' is missing or empty after the run -- the cleanup "
                     "phase (or an earlier one) likely moved/deleted it")
    verify = report["objective_verify"]
    if verify and verify.get("ran") and not verify.get("passed"):
        flags.append("failed the objective hidden-test verification")
    return flags


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("projects_root_or_dir", type=Path,
                        help="A single task's project dir, or a --results-bearing projects root.")
    parser.add_argument("--results", type=Path,
                        help="bench_results.json from harness.py (default: <arg>/bench_results.json).")
    parser.add_argument("--out", type=Path,
                        help="Where to write the combined scorecard JSON (default: <arg>/bench_scorecard.json).")
    args = parser.parse_args()

    results_path = args.results or (args.projects_root_or_dir / "bench_results.json")
    if not results_path.exists():
        print(f"error: no results file at {results_path} -- run harness.py first", file=sys.stderr)
        raise SystemExit(1)
    run_results = json.loads(results_path.read_text(encoding="utf-8"))

    scorecards = []
    for run_result in run_results:
        card = score_session(Path(run_result["project_dir"]), run_result["task_id"], run_result.get("verify"))
        card["harness_outcome"] = run_result.get("outcome")
        scorecards.append(card)

    out_path = args.out or (args.projects_root_or_dir / "bench_scorecard.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(scorecards, indent=2), encoding="utf-8")

    print(f"{'task':<24} {'harness':<14} {'verify':<7} {'leaves':<8} {'episodes':<9} {'overflow':<9} "
          f"{'reopened':<9} {'tool_err%':<9}")
    for c in scorecards:
        verify = c["objective_verify"] or {}
        verify_str = "n/a" if verify.get("passed") is None else ("PASS" if verify["passed"] else "FAIL")
        plan, episodes, dev, tools = c["plan"] or {}, c["episodes"] or {}, c["dev"] or {}, c["tools"] or {}
        leaves = f"{plan.get('done', 0)}/{plan.get('leaves', 0)}" if plan else "n/a"
        print(f"{c['task_id']:<24} {str(c['harness_outcome']):<14} {verify_str:<7} {leaves:<8} "
              f"{episodes.get('episodes', 0):<9} {episodes.get('overflows', 0):<9} {dev.get('reopened', 0):<9} "
              f"{tools.get('tool_call_error_rate', 0) * 100:<9.1f}")
        for flag in c["flags"]:
            print(f"    ! {flag}")
    print(f"\nFull scorecard: {out_path}")


if __name__ == "__main__":
    main()
