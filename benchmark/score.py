#!/usr/bin/env python3
"""
Scores one finished (or abandoned) JFI benchmark session by combining:
  1. The objective outcome from harness.py's bench_results.json (did the
     task's hidden/held-out verify.command actually pass?).
  2. Process metrics scraped from JFI's own on-disk artifacts under
     JFI/<task_id>/ -- run.log, history.jsonl.gz, plan.md, metadata.json --
     none of which JFI computes or exposes itself.

Why these particular process metrics: each one is directly traceable to a
real failure mode observed while running JFI against a local ~12B reasoning
model (see the harness/README for the full story):
  - plan_rewrite_count: the model repeatedly called write_file on plan.md
    instead of resuming it -- 15+ full re-plans in one session instead of
    ticking boxes. Per PLAN_FORMAT_RULES, plan.md should be write_file'd
    ONCE (by the planner) and only ever edited afterward via
    replace_in_file (the boxes get ticked, the file is never rewritten
    wholesale) -- so any write_file call to it beyond the first is exactly
    the anomaly this metric is built to catch, independent of how the
    reviewer or the model itself judged the run.
  - stall_nudges: how many times the model reasoned for a full turn
    without ever producing content or a tool call (JFI's own AUTO-RECTIFY
    "entirely reasoning" nudge, see runner.py) -- a proxy for the model
    losing its footing.
  - llm_retry_events / crash_events: how many times the LLM backend itself
    failed mid-session (timeouts, 5xx, or an HTML error page) -- this is
    infrastructure instability, not the model's fault, but it directly
    caused the one real session death seen in practice (a growing,
    redundant context eventually crashing the local server outright).
  - tool_call_error_rate: fraction of tool calls that came back as an
    Error -- a cheap, model-agnostic efficiency signal independent of
    whether the task ultimately passed.
  - hidden_tests: whether a task's hidden_tests_dir (e.g. tests/, present
    per task.json) survived the run. JFI's pipeline now ends with a
    cleanup phase that has execute_command's mv/rm and is told to leave
    the deliverable's own tests/ alone -- but it's new and autonomous, so
    this makes "cleanup deleted/moved the oracle test file" a labeled
    flag instead of a mysterious objective-verify failure.

Usage:
    python3 score.py <project_dir> [--results bench_results.json]
"""
import argparse
import gzip
import json
import re
from pathlib import Path
from typing import Optional

import harness


def _read_history(history_path: Path) -> list:
    if not history_path.exists():
        return []
    try:
        with gzip.open(history_path, "rb") as f:
            raw = f.read()
    except OSError:
        return []
    messages = []
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return messages


def _plan_progress(plan_text: str) -> dict:
    done = len(re.findall(r"^[ \t]*[-*][ \t]*\[[xX]\]", plan_text, re.M))
    skipped = len(re.findall(r"^[ \t]*[-*][ \t]*\[○\]", plan_text, re.M))
    todo = len(re.findall(r"^[ \t]*[-*][ \t]*\[ \]", plan_text, re.M))
    return {"done": done, "skipped": skipped, "todo": todo, "total": done + skipped + todo}


# A leaf line's number (e.g. "1.1.1.1") shows its full path from the section
# root per PLAN_FORMAT_RULES -- dot-count is therefore an exact, mechanical
# depth signal straight from the numbering the planner is mandated to use,
# not an indentation guess. Same shape for a parent bullet, just without the
# checkbox.
_LEAF_NUM_RE = re.compile(r"^[ \t]*[-*][ \t]*\[[xX ○]\][ \t]*(\d+(?:\.\d+)*)")
# A top-level parent is often written "- 1. Description" (trailing period),
# while a nested parent is "- 1.1 Description" (no period) -- both seen in
# real plans -- so the period before the required whitespace is optional.
_PARENT_NUM_RE = re.compile(r"^[ \t]*[-*][ \t]+(\d+(?:\.\d+)*)\.?[ \t]+\S")


def _plan_structure_metrics(plan_text: str) -> dict:
    """
    Purely structural stats about the plan's tree shape: how deep it
    recursed and how many leaves each top-level task ended up with.

    Deliberately NOT a judgment call about whether any given leaf's
    description is "too big" -- flagging that reliably would need real
    language understanding of the leaf's own prose (see PLAN_FORMAT_RULES'
    "+"/"and"/"/" heuristic, which is a authoring instruction for the model,
    not something safe to re-implement here as a pass/fail check: e.g.
    "division by zero and any unparseable line" is one legitimate leaf about
    one error-handling contract, not two leaves wearing a trenchcoat, and no
    regex can tell those two cases apart reliably). A wrong semantic guess
    here would be worse than no signal, so this only reports the tree's
    shape -- depth and leaves-per-top-level-task -- the same way
    plan_rewrite_count reports write_file counts without judging the plan's
    prose. Read alongside plan_progress's leaf total to spot a plan that's
    suspiciously flat (many leaves, but max_leaf_depth stuck at 1-2, or one
    top-level task hoarding most of the leaves while its siblings got one
    each) -- worth a human/model second look, not an automatic fail.
    """
    leaf_depths = []
    parent_count = 0
    top_level_leaf_counts: dict = {}

    for line in plan_text.splitlines():
        m = _LEAF_NUM_RE.match(line)
        if m:
            number = m.group(1)
            parts = number.split(".")
            leaf_depths.append(len(parts))
            top_level_leaf_counts[parts[0]] = top_level_leaf_counts.get(parts[0], 0) + 1
            continue
        if _PARENT_NUM_RE.match(line):
            parent_count += 1

    return {
        "leaf_count": len(leaf_depths),
        "parent_task_count": parent_count,
        "max_leaf_depth": max(leaf_depths) if leaf_depths else 0,
        "min_leaf_depth": min(leaf_depths) if leaf_depths else 0,
        "avg_leaf_depth": round(sum(leaf_depths) / len(leaf_depths), 2) if leaf_depths else 0,
        "top_level_task_count": len(top_level_leaf_counts),
        "leaves_per_top_level_task": dict(sorted(
            top_level_leaf_counts.items(), key=lambda kv: int(kv[0])
        )),
    }


def _run_log_metrics(run_log_text: str) -> dict:
    started = re.search(r"===== JFI run started ([\d\-T:]+)", run_log_text)
    phases_completed = re.findall(r"Phase '(\w+)' completed successfully", run_log_text)
    review_fails = len(re.findall(r"REVIEW FAILED", run_log_text))
    llm_retry_events = len(re.findall(r"LLM request failed \(attempt", run_log_text))
    crash_events = len(re.findall(r"HTTP \d{3} — the server returned an HTML error page", run_log_text))
    reached_pipeline_complete = "PIPELINE COMPLETE" in run_log_text
    review_loop_cap_hit = "REVIEW LOOP CAP REACHED" in run_log_text
    return {
        "started_at": started.group(1) if started else None,
        "phases_completed": phases_completed,
        "review_failures": review_fails,
        "llm_retry_events": llm_retry_events,
        "crash_events": crash_events,
        "reached_pipeline_complete": reached_pipeline_complete,
        "review_loop_cap_hit": review_loop_cap_hit,
    }


def _history_metrics(history: list, plan_basename: str) -> dict:
    tool_calls_total = 0
    tool_call_errors = 0
    tool_name_counts: dict = {}
    plan_write_file_count = 0
    stall_nudges = 0

    for message in history:
        if message.get("role") == "user" and "your last turn was entirely reasoning" in str(message.get("content") or ""):
            stall_nudges += 1

        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            name = function.get("name") or "?"
            tool_calls_total += 1
            tool_name_counts[name] = tool_name_counts.get(name, 0) + 1
            if name == "write_file":
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if str(args.get("file_path", "")).endswith(plan_basename):
                    plan_write_file_count += 1

        if message.get("role") == "tool" and str(message.get("content") or "").lstrip().startswith("Error"):
            tool_call_errors += 1

    error_rate = (tool_call_errors / tool_calls_total) if tool_calls_total else 0.0
    return {
        "tool_calls_total": tool_calls_total,
        "tool_call_errors": tool_call_errors,
        "tool_call_error_rate": round(error_rate, 3),
        "tool_name_counts": tool_name_counts,
        # First write_file to plan.md is the planner creating it -- expected
        # exactly once. Every one after that is a full re-plan instead of
        # a replace_in_file tick; see the module docstring.
        "plan_rewrite_count": max(0, plan_write_file_count - 1),
        "stall_nudges": stall_nudges,
    }


def _hidden_tests_status(task_id: str, project_dir: Path) -> Optional[dict]:
    """Whether this task's hidden_tests_dir (if it declares one) is still
    present and non-empty after the run -- see the module docstring's
    hidden_tests bullet for why this specifically needs checking now that
    a cleanup phase with mv/rm runs at the end of every JFI session."""
    try:
        task = harness.load_task(task_id)
    except (FileNotFoundError, ValueError):
        return None
    hidden_dir = task.get("hidden_tests_dir")
    if not hidden_dir:
        return None
    path = project_dir / hidden_dir
    present = path.is_dir()
    return {
        "path": hidden_dir,
        "present": present,
        "empty": present and not any(path.iterdir()),
    }


def score_session(project_dir: Path, task_id: str, verify_result: Optional[dict]) -> dict:
    session_dir = project_dir / "JFI" / task_id
    run_log_path = session_dir / "run.log"
    history_path = session_dir / "history.jsonl.gz"
    plan_path = session_dir / "plan.md"

    run_log_text = run_log_path.read_text(encoding="utf-8", errors="replace") if run_log_path.exists() else ""
    plan_text = plan_path.read_text(encoding="utf-8", errors="replace") if plan_path.exists() else ""
    history = _read_history(history_path)

    report = {
        "task_id": task_id,
        "project_dir": str(project_dir),
        "objective_verify": verify_result,
        "plan_progress": _plan_progress(plan_text) if plan_text else None,
        "plan_structure": _plan_structure_metrics(plan_text) if plan_text else None,
        **_run_log_metrics(run_log_text),
        **_history_metrics(history, Path(plan_path).name),
        "hidden_tests": _hidden_tests_status(task_id, project_dir),
    }

    flags = []
    if report["plan_rewrite_count"] >= 2:
        flags.append(f"plan.md was rewritten wholesale {report['plan_rewrite_count']} extra time(s) instead of resumed -- likely coherence/context-tracking breakdown")
    if report["stall_nudges"] >= 5:
        flags.append(f"{report['stall_nudges']} reasoning-only dead turns -- model repeatedly failed to commit to an action")
    if report["crash_events"] >= 1:
        flags.append(f"{report['crash_events']} LLM backend crash(es) (HTML error page) -- likely local server instability, possibly context-window overflow")
    if report["review_loop_cap_hit"]:
        flags.append("hit the 3-failed-review cap -- never reached a clean review")
    hidden_tests = report["hidden_tests"]
    if hidden_tests and (not hidden_tests["present"] or hidden_tests["empty"]):
        flags.append(
            f"hidden test dir '{hidden_tests['path']}' is missing or empty after the run -- "
            "the cleanup phase (or an earlier one) likely moved/deleted it"
        )
    if verify_result and verify_result.get("ran") and not verify_result.get("passed"):
        flags.append("finished the pipeline but failed the objective hidden-test verification")
    structure = report["plan_structure"]
    # Advisory only -- see _plan_structure_metrics' docstring for why this
    # doesn't try to judge any single leaf. A plan can legitimately never
    # nest past depth 1 for a genuinely small task; this just surfaces the
    # shape for a human/model to glance at, the same spirit as story's
    # honestly-labeled weak grading.
    if structure and structure["leaf_count"] >= 5 and structure["max_leaf_depth"] <= 1:
        flags.append(
            f"plan never nested past depth 1 despite {structure['leaf_count']} leaves -- "
            "worth a second look for under-decomposition (see plan_structure)"
        )
    report["flags"] = flags

    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("projects_root_or_dir", type=Path, help="A single task's project dir, or a --results-bearing projects root.")
    parser.add_argument("--results", type=Path, help="bench_results.json from harness.py (default: <arg>/bench_results.json).")
    parser.add_argument("--out", type=Path, help="Where to write the combined scorecard JSON (default: <arg>/bench_scorecard.json).")
    args = parser.parse_args()

    results_path = args.results or (args.projects_root_or_dir / "bench_results.json")
    if not results_path.exists():
        print(f"error: no results file at {results_path} -- run harness.py first", file=sys.stderr)
        raise SystemExit(1)

    with open(results_path, "r", encoding="utf-8") as f:
        run_results = json.load(f)

    scorecards = []
    for run_result in run_results:
        task_id = run_result["task_id"]
        project_dir = Path(run_result["project_dir"])
        card = score_session(project_dir, task_id, run_result.get("verify"))
        card["harness_outcome"] = run_result.get("outcome")
        scorecards.append(card)

    out_path = args.out or (args.projects_root_or_dir / "bench_scorecard.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(scorecards, f, indent=2)

    print(f"{'task':<24} {'harness':<14} {'verify':<8} {'plan':<10} {'depth':<7} {'rewrites':<9} {'stalls':<7} {'crashes':<8} {'tool_err%':<10}")
    for c in scorecards:
        verify = c["objective_verify"] or {}
        verify_str = "n/a" if verify.get("passed") is None else ("PASS" if verify["passed"] else "FAIL")
        plan = c["plan_progress"] or {}
        plan_str = f"{plan.get('done', 0)}/{plan.get('total', 0)}" if plan else "n/a"
        structure = c["plan_structure"] or {}
        depth_str = (f"{structure.get('avg_leaf_depth', 0)}/{structure.get('max_leaf_depth', 0)}"
                     if structure else "n/a")
        print(f"{c['task_id']:<24} {c['harness_outcome']:<14} {verify_str:<8} {plan_str:<10} {depth_str:<7} "
              f"{c['plan_rewrite_count']:<9} {c['stall_nudges']:<7} {c['crash_events']:<8} {c['tool_call_error_rate']*100:<10.1f}")
        for flag in c["flags"]:
            print(f"    ⚠ {flag}")

    print(f"\nFull scorecard: {out_path}")


if __name__ == "__main__":
    import sys
    main()
