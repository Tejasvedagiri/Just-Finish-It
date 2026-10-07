#!/usr/bin/env python3
"""
Drives JFI through one or more benchmark tasks, unattended, and records
timing/outcome plus an objective pass/fail (via each task's `verify`
command) for score.py to combine with JFI's own on-disk artifacts (run.log,
history.jsonl.gz, plan.md, metadata.json under JFI/<task>/).

JFI's console is a full-screen prompt_toolkit TUI with no non-interactive
mode -- there is no flag or stdin pipe that skips the UI -- so this drives
it the only way that actually works: a real terminal, with keystrokes for
the two startup prompts (session name, goal) and polling of the screen for
known blocking prompts and the completion marker. On Linux/macOS that
terminal is tmux; on Windows it's ConPTY through pywinpty (`pip install
pywinpty`), the driver the Windows calc runs used.

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
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
TASKS_DIR = BENCHMARK_DIR / "tasks"
# `uv run build` is a onefile build: dist/jfi (dist/jfi.exe on Windows).
DEFAULT_JFI_BIN = BENCHMARK_DIR.parent / "dist" / ("jfi.exe" if os.name == "nt" else "jfi")

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
        "accept it and start building?",
        ["Enter"],  # "Accept -- start building" (only asked with EVIDENCE_REVIEW=1)
        "evidence review requested -- auto-accepting",
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


class Tmux:
    """A JFI run in a detached tmux session (Linux/macOS)."""

    KEYS = {"Enter": "Enter", "Right": "Right", "C-c": "C-c"}

    def __init__(self, name: str):
        self.name = name

    def _tmux(self, *args):
        return subprocess.run(["tmux", *args], capture_output=True, text=True)

    def start(self, command: str, cwd: Path) -> str:
        self._tmux("kill-session", "-t", self.name)  # clear any stale session from a prior attempt
        r = self._tmux("new-session", "-d", "-s", self.name, "-x", "220", "-y", "50", "-c", str(cwd), command)
        return r.stderr.strip() if r.returncode != 0 else ""

    def type(self, text: str):
        self._tmux("send-keys", "-t", self.name, "-l", text)

    def key(self, key: str):
        self._tmux("send-keys", "-t", self.name, self.KEYS[key])

    def text(self) -> str:
        r = self._tmux("capture-pane", "-t", self.name, "-p", "-S", "-2000")
        return r.stdout if r.returncode == 0 else ""

    def alive(self) -> bool:
        return self._tmux("has-session", "-t", self.name).returncode == 0

    def kill(self):
        self._tmux("kill-session", "-t", self.name)


class ConPty:
    """A JFI run in a Windows pseudo-console (pywinpty). The screen is the
    raw output stream with escape codes stripped and whitespace squashed --
    prompt_toolkit redraws in place, so needles are matched without spaces."""

    KEYS = {"Enter": "\r", "Right": "\x1b[C", "C-c": "\x03"}
    ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]|\x1b[=>]")

    def __init__(self, name: str):
        self.name, self.proc, self.chunks = name, None, []

    def start(self, command: str, cwd: Path) -> str:
        try:
            from winpty import PtyProcess
        except ImportError:
            return "pywinpty is not installed (pip install pywinpty)"
        self.proc = PtyProcess.spawn(command, cwd=str(cwd), env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                                     dimensions=(50, 220))
        threading.Thread(target=self._read, daemon=True).start()
        return ""

    def _read(self):
        while self.proc.isalive():
            try:
                self.chunks.append(self.proc.read(65536))
            except EOFError:
                break
            except Exception:
                time.sleep(0.2)

    def type(self, text: str):
        self.proc.write(text)

    def key(self, key: str):
        self.proc.write(self.KEYS[key])

    def text(self) -> str:
        return re.sub(r"\s+", "", self.ANSI.sub("", "".join(self.chunks)[-200_000:]))

    def alive(self) -> bool:
        return self.proc is not None and self.proc.isalive()

    def kill(self):
        if self.alive():
            self.proc.terminate(force=True)


def terminal(name: str):
    return ConPty(name) if os.name == "nt" else Tmux(name)


def seen(term, needle: str, text: str) -> bool:
    return (re.sub(r"\s+", "", needle) if isinstance(term, ConPty) else needle) in text


def wait_for(term, needle, timeout, log):
    """Polls the screen for `needle`, auto-answering AUTO_PROMPTS in the
    meantime. Returns the screen text once `needle` appears or the run
    dies (caller must check with seen() to tell those apart), or None on
    timeout."""
    deadline = time.time() + timeout
    auto_actions = 0
    prompt_was_up = None
    while time.time() < deadline:
        text = term.text()
        if seen(term, needle, text):
            return text
        if not term.alive():
            log(f"session died while waiting for {needle!r}")
            return text
        recent = text[-20_000:]
        handled = None
        for prompt_needle, keys, description in AUTO_PROMPTS:
            if seen(term, prompt_needle, recent):
                handled = prompt_needle
                if prompt_was_up != prompt_needle:
                    if auto_actions < MAX_AUTO_ACTIONS:
                        auto_actions += 1
                        log(f"{description} ({auto_actions}/{MAX_AUTO_ACTIONS})")
                        for key in keys:
                            term.key(key)
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

    run_as = command
    if os.name == "nt":
        # On Windows `python3` is usually the Microsoft Store alias, not an
        # interpreter, and the verify commands are POSIX shell (`>/dev/null
        # 2>&1`, `&&`), which cmd.exe can't run -- Git Bash can.
        if command.startswith("python3 "):
            command = f'"{Path(sys.executable).as_posix()}" {command[len("python3 "):]}'
        bash = shutil.which("bash")
        run_as = [bash, "-c", command] if bash else command
    log(f"running objective verify: {command}")
    try:
        proc = subprocess.run(run_as, shell=isinstance(run_as, str), cwd=project_dir, capture_output=True,
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

    term = terminal(session)
    log(f"launching {jfi_bin} in {project_dir} as {type(term).__name__} session {session}")
    error = term.start(str(jfi_bin), project_dir)
    if error:
        log(f"FAILED to start the terminal: {error}")
        result["outcome"] = "launch_failed"
        result["ended_at"] = time.time()
        return result

    if wait_for(term, "session name to begin", PROMPT_TIMEOUT, log) is None:
        log("TIMEOUT waiting for session-name prompt")
        term.kill()
        result["outcome"] = "launch_failed"
        result["ended_at"] = time.time()
        return result

    term.type(task_id)
    term.key("Enter")

    if wait_for(term, "What is your goal", PROMPT_TIMEOUT, log) is None:
        log("TIMEOUT waiting for goal prompt")
        term.kill()
        result["outcome"] = "launch_failed"
        result["ended_at"] = time.time()
        return result

    term.type(" ".join(task["prompt"].split()))  # one line: a newline would submit the goal early
    time.sleep(1)
    term.key("Enter")

    log(f"goal submitted, waiting up to {timeout}s for PIPELINE COMPLETE")
    text = wait_for(term, "PIPELINE COMPLETE", timeout, log)
    if text is None:
        log(f"TIMEOUT after {timeout}s -- leaving session '{session}' running for inspection")
        if isinstance(term, ConPty):
            term.kill()  # a pseudo-console can't outlive this process anyway
        result["outcome"] = "timeout"
        result["ended_at"] = time.time()
        return result

    if not seen(term, "PIPELINE COMPLETE", text):
        log("session ended WITHOUT reaching PIPELINE COMPLETE (crashed or was stopped early)")
        term.kill()
        result["outcome"] = "session_died"
        result["ended_at"] = time.time()
        result["verify"] = run_verify(task, project_dir, log)
        return result

    if term.alive():
        log("pipeline complete -- sending Ctrl-C to stop gracefully")
        term.key("C-c")
        if wait_for(term, "SESSION TERMINATED", STOP_CONFIRM_TIMEOUT, log) is None:
            log("graceful stop did not confirm in time; force-killing session")
        term.kill()
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
