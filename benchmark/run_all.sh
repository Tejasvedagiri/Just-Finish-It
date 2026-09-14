#!/bin/sh
# Runs the full JFI benchmark suite (harness.py --all) and then scores it
# (score.py), all in the background.
#
# Usage:
#   sh benchmark/run_all.sh                       # run everything
#   BENCH_TIER=polyglot sh benchmark/run_all.sh    # just one tier
#   BENCH_TIMEOUT=1200 sh benchmark/run_all.sh     # lower per-task timeout
#   PROJECTS_ROOT=/some/other/dir sh benchmark/run_all.sh   # override output location
#
# To actually run this in the background, detached from the current shell:
#   nohup sh benchmark/run_all.sh > /dev/null 2>&1 &
#   disown
#
# Then check progress with:
#   tail -f "$PROJECTS_ROOT/bench_run.log"     (PROJECTS_ROOT defaults below)
#   tmux ls                                     (one bench-<task_id> session per task, live)
set -u

BENCHMARK_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BENCHMARK_DIR"

# Defaults to benchmark/runs/ (inside this repo, gitignored via
# /benchmark/runs/ in .gitignore) -- run output, unlike the benchmark
# definitions themselves (tasks/, harness.py, score.py), is never meant to be
# committed, but keeping it inside the repo by default (rather than some
# external path) means a fresh checkout works with zero extra setup.
PROJECTS_ROOT="${PROJECTS_ROOT:-$BENCHMARK_DIR/runs}"
LOG="$PROJECTS_ROOT/bench_run.log"
mkdir -p "$PROJECTS_ROOT"

TIER_ARGS="--all"
if [ -n "${BENCH_TIER:-}" ]; then
  TIER_ARGS="--tier $BENCH_TIER"
fi

TIMEOUT_ARGS=""
if [ -n "${BENCH_TIMEOUT:-}" ]; then
  TIMEOUT_ARGS="--timeout $BENCH_TIMEOUT"
fi

{
  echo "===== benchmark run started $(date -Iseconds) (tier=${BENCH_TIER:-all}) ====="
  python3 harness.py $TIER_ARGS --projects-root "$PROJECTS_ROOT" $TIMEOUT_ARGS
  echo "===== harness finished $(date -Iseconds) -- scoring ====="
  python3 score.py "$PROJECTS_ROOT"
  echo "===== benchmark run finished $(date -Iseconds) ====="
} >> "$LOG" 2>&1
