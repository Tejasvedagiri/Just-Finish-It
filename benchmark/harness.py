#!/usr/bin/env python3
"""
Drives JFI through one or more benchmark tasks, unattended, and records
timing/outcome plus an objective pass/fail (via each task's `verify`
command) for score.py to combine with JFI's own on-disk artifacts (run.log,
history.jsonl.gz, plan.md, metadata.json under JFI/<task>/).

JFI's console is a full-screen prompt_toolkit TUI with no non-interactive
mode -- there is no flag or stdin pipe that skips the UI -- so this drives
it the only way that actually works: a real pty via tmux, with send-keys
for the two startup prompts (session name, goal) and polling of the pane
content for known blocking prompts and the completion marker.

This exists because an earlier ad-hoc version of this exact script (built
during a manual eval session, living only in a throwaway scratchpad) hit
two real blocking prompts it didn't know how to answer -- the LLM-failure
"Retry, or stop the run?" menu (after the local LLM backend crashed) and
JFI's own execute_command "Run this command?" approval gate -- and both
would have silently stalled the run until timeout. Handling both here,
generically, is the main thing that makes unattended benchmark runs
actually finish instead of needing a human watching the tmux pane.

Task layout (see README.md for the full methodology and why these three
tiers): tasks/<tier>/<task_id>/task.json, with optional sibling
directories referenced by the task:
  - hidden_tests_dir: copied into the project root BEFORE the JFI run
    starts, so it's visible to the model (which is told not to edit it) --
    the Aider-Polyglot-style "hidden but present" reference test oracle.
  - verify_files_dir: copied into the project root AFTER the JFI run ends,
    never seen by the model -- e.g. a checker script that drives the
    finished program end-to-end.

Usage:
    python3 harness.py --task calc --project-dir /path/to/projects/calc
    python3 harness.py --all --projects-root /path/to/projects
    python3 harness.py --tier polyglot --projects-root /path/to/projects
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
TASKS_DIR = BENCHMARK_DIR / "tasks"
DEFAULT_JFI_BIN = BENCHMARK_DIR.parent / "dist" / "jfi"

POLL_INTERVAL = 5
PROMPT_TIMEOUT = 60          # waiting for the two startup prompts
STOP_CONFIRM_TIMEOUT = 60    # waiting for graceful shutdown after Ctrl-C

# Blocking menus JFI can stop on mid-run, and the unattended-safe answer for
# each -- see the module docstring for why these two specifically matter.
AUTO_PROMPTS = [
    (
        "Retry, or stop the run?",
        ["Enter"],  # "Retry now" is the default-highlighted option
        "LLM request failed -- auto-selecting Retry now",
    ),
    (
        "Run this command?",
        ["Right", "Right", "Enter"],  # -> "Yes, for the rest of this session"
        "command approval requested -- auto-approving for the rest of this session",
    ),
]
MAX_AUTO_ACTIONS = 30  # cap total auto-answers so a truly stuck run can't loop forever


def find_task_path(task_id: str) -> Path:
    matches = list(TASKS_DIR.glob(f"*/{task_id}/task.json"))
    if not matches:
        raise FileNotFoundError(f"no task named '{task_id}' under {TASKS_DIR}")
    if len(matches) > 1:
        raise ValueError(f"task id '{task_id}' is ambiguous across tiers: {matches}")
    return matches[0]


def load_task(task_id: str) -> dict:
    path = find_task_path(task_id)
    with open(path, "r", encoding="utf-8") as f:
        task = json.load(f)
    task["_dir"] = path.parent
    return task


def list_tasks(tier: str = None) -> list:
    pattern = f"{tier or '*'}/*/task.json"
    ids = []
    for path in sorted(TASKS_DIR.glob(pattern)):
        with open(path, "r", encoding="utf-8") as f:
            ids.append(json.load(f)["id"])
    return ids


def tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def pane_text(session):
    r = tmux("capture-pane", "-t", session, "-p", "-S", "-2000")
    return r.stdout if r.returncode == 0 else ""


def session_alive(session):
    return tmux("has-session", "-t", session).returncode == 0


def wait_for(session, needle, timeout, log):
    """Polls the pane for `needle`, auto-answering AUTO_PROMPTS in the
    meantime. Returns the pane text once `needle` appears or the session
    dies (caller must check `needle in text` to tell those apart), or None
    on timeout."""
    deadline = time.time() + timeout
    auto_actions = 0
    prompt_was_up = None
    while time.time() < deadline:
        text = pane_text(session)
        if needle in text:
            return text
        if not session_alive(session):
            log(f"session died while waiting for {needle!r}")
            return text
        handled = None
        for prompt_needle, keys, description in AUTO_PROMPTS:
            if prompt_needle in text:
                handled = prompt_needle
                if prompt_was_up != prompt_needle:
                    if auto_actions < MAX_AUTO_ACTIONS:
                        auto_actions += 1
                        log(f"{description} ({auto_actions}/{MAX_AUTO_ACTIONS})")
                        for key in keys:
                            tmux("send-keys", "-t", session, key)
                    else:
                        log(f"hit MAX_AUTO_ACTIONS ({MAX_AUTO_ACTIONS}) on {prompt_needle!r} -- leaving it for inspection")
                break
        prompt_was_up = handled
        time.sleep(POLL_INTERVAL)
    return None


def run_verify(task: dict, project_dir: Path, log) -> dict:
    """Runs the task's objective verify.command in project_dir, after
    copying in verify_files_dir (a checker never shown to the model) on
    top of whatever hidden_tests_dir already put there. Returns
    {"ran": bool, "passed": bool, "returncode": int, "output": str}."""
    verify = task.get("verify") or {}
    command = verify.get("command")
    if not command:
        return {"ran": False, "passed": None, "returncode": None, "output": "(no verify.command for this task)"}

    verify_files_dir = task.get("verify_files_dir")
    if verify_files_dir:
        src = task["_dir"] / verify_files_dir
        if src.is_dir():
            shutil.copytree(src, project_dir, dirs_exist_ok=True)

    log(f"running objective verify: {command}")
    try:
        proc = subprocess.run(command, shell=True, cwd=project_dir, capture_output=True,
                               text=True, timeout=int(task.get("verify_timeout", 120)))
        output = (proc.stdout or "") + (proc.stderr or "")
        return {"ran": True, "passed": proc.returncode == 0, "returncode": proc.returncode, "output": output[-4000:]}
    except subprocess.TimeoutExpired as e:
        return {"ran": True, "passed": False, "returncode": None, "output": f"verify command timed out: {e}"}
    except Exception as e:
        return {"ran": True, "passed": False, "returncode": None, "output": f"verify command errored: {e}"}


def run_task(task_id: str, project_dir: Path, jfi_bin: Path, timeout: int, log_path: Path) -> dict:
    task = load_task(task_id)
    session = f"bench-{task_id}"
    project_dir.mkdir(parents=True, exist_ok=True)

    def log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    result = {
        "task_id": task_id,
        "tier": task.get("tier"),
        "project_dir": str(project_dir),
        "started_at": time.time(),
        "ended_at": None,
        "outcome": None,  # "completed" | "timeout" | "session_died" | "launch_failed"
        "verify": None,
    }

    hidden_tests_dir = task.get("hidden_tests_dir")
    if hidden_tests_dir:
        src = task["_dir"] / hidden_tests_dir
        if src.is_dir():
            shutil.copytree(src, project_dir / hidden_tests_dir, dirs_exist_ok=True)
            log(f"placed hidden reference tests at {hidden_tests_dir}/ (visible to the model, do-not-edit)")

    tmux("kill-session", "-t", session)  # clear any stale session from a prior attempt

    log(f"launching {jfi_bin} in {project_dir} as tmux session {session}")
    r = tmux("new-session", "-d", "-s", session, "-x", "220", "-y", "50", "-c", str(project_dir), str(jfi_bin))
    if r.returncode != 0:
        log(f"FAILED to start tmux session: {r.stderr.strip()}")
        result["outcome"] = "launch_failed"
        result["ended_at"] = time.time()
        return result

    if wait_for(session, "session name to begin", PROMPT_TIMEOUT, log) is None:
        log("TIMEOUT waiting for session-name prompt")
        tmux("kill-session", "-t", session)
        result["outcome"] = "launch_failed"
        result["ended_at"] = time.time()
        return result

    tmux("send-keys", "-t", session, "-l", task_id)
    tmux("send-keys", "-t", session, "Enter")

    if wait_for(session, "What is your goal", PROMPT_TIMEOUT, log) is None:
        log("TIMEOUT waiting for goal prompt")
        tmux("kill-session", "-t", session)
        result["outcome"] = "launch_failed"
        result["ended_at"] = time.time()
        return result

    tmux("send-keys", "-t", session, "-l", task["prompt"])
    tmux("send-keys", "-t", session, "Enter")

    log(f"goal submitted, waiting up to {timeout}s for PIPELINE COMPLETE")
    text = wait_for(session, "PIPELINE COMPLETE", timeout, log)
    if text is None:
        log(f"TIMEOUT after {timeout}s -- leaving session '{session}' running for inspection")
        result["outcome"] = "timeout"
        result["ended_at"] = time.time()
        return result

    if "PIPELINE COMPLETE" not in text:
        log("session ended WITHOUT reaching PIPELINE COMPLETE (crashed or was stopped early)")
        tmux("kill-session", "-t", session)
        result["outcome"] = "session_died"
        result["ended_at"] = time.time()
        result["verify"] = run_verify(task, project_dir, log)
        return result

    if session_alive(session):
        log("pipeline complete -- sending Ctrl-C to stop gracefully")
        tmux("send-keys", "-t", session, "C-c")
        if wait_for(session, "SESSION TERMINATED", STOP_CONFIRM_TIMEOUT, log) is None:
            log("graceful stop did not confirm in time; force-killing session")
        tmux("kill-session", "-t", session)
    else:
        log("session ended on its own right at pipeline completion")

    result["outcome"] = "completed"
    result["ended_at"] = time.time()
    result["verify"] = run_verify(task, project_dir, log)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", help="Task id (e.g. 'calc', 'difference_of_squares'). Repeatable via --all/--tier instead.")
    parser.add_argument("--all", action="store_true", help="Run every task under tasks/, sequentially.")
    known_tiers = sorted(p.name for p in TASKS_DIR.iterdir() if p.is_dir())
    parser.add_argument("--tier", choices=known_tiers, help="Run every task in just this tier.")
    parser.add_argument("--project-dir", type=Path, help="Where to run a single --task (required with --task).")
    parser.add_argument("--projects-root", type=Path, help="Parent dir for --all/--tier; each task runs in <root>/<task_id>.")
    parser.add_argument("--jfi-bin", type=Path, default=DEFAULT_JFI_BIN, help=f"Path to the jfi binary (default: {DEFAULT_JFI_BIN}).")
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("BENCH_TIMEOUT", 2700)),
                         help="Per-task seconds to wait for PIPELINE COMPLETE (default 2700 / 45min, or $BENCH_TIMEOUT).")
    parser.add_argument("--results", type=Path, help="Where to write the results JSON (default: <projects-root>/bench_results.json).")
    args = parser.parse_args()

    if not args.jfi_bin.exists():
        print(f"error: jfi binary not found at {args.jfi_bin} -- build it with 'uv run build' first", file=sys.stderr)
        sys.exit(1)

    if args.all or args.tier:
        if not args.projects_root:
            print("error: --all/--tier requires --projects-root", file=sys.stderr)
            sys.exit(2)
        task_ids = list_tasks(args.tier)
        if not task_ids:
            print(f"error: no tasks found for tier={args.tier!r}", file=sys.stderr)
            sys.exit(2)
        results_path = args.results or (args.projects_root / "bench_results.json")
        log_path = args.projects_root / "bench_harness.log"
        results = []
        for task_id in task_ids:
            project_dir = args.projects_root / task_id
            results.append(run_task(task_id, project_dir, args.jfi_bin, args.timeout, log_path))
            results_path.parent.mkdir(parents=True, exist_ok=True)
            with open(results_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)
        passed = sum(1 for r in results if r.get("verify") and r["verify"].get("passed"))
        print(f"\n{passed}/{len(results)} tasks passed their objective verify. Results: {results_path}")
        return

    if not args.task or not args.project_dir:
        print("error: pass either --task <id> --project-dir <path>, or --all/--tier --projects-root <path>", file=sys.stderr)
        sys.exit(2)

    results_path = args.results or (args.project_dir / "bench_results.json")
    log_path = args.project_dir.parent / f"{args.task}_harness.log"
    result = run_task(args.task, args.project_dir, args.jfi_bin, args.timeout, log_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump([result], f, indent=2)
    verify_str = "n/a" if not result.get("verify") else ("PASS" if result["verify"]["passed"] else "FAIL")
    print(f"\n{args.task}: outcome={result['outcome']} verify={verify_str}. Results: {results_path}")


if __name__ == "__main__":
    main()
