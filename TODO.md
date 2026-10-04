# TODO

Open work. What's done, and the runs that checked it, is in
[`docs/feature-description.md`](docs/feature-description.md); the Laya
sizing POC is in [`docs/laya_poc.md`](docs/laya_poc.md).

## Open

1. **Commit `feature/description`** in both repos (JFI and
   Just-Finish-It-Fleet). Nothing since `c70fa82` is committed.
2. **Update the QA machine's old install** (macOS). It still runs a v1 build
   (Product Owner and Testing phases). On that machine: stop the old
   session, `git pull`, `uv sync --extra web --extra laya`,
   `uv run create-env`, then `uv run build` or `uv run jfi`. A v1 session
   can't be resumed; a new session in the same project starts from the last
   v2 session's runbook and design.

3. **Commit `feature/parllel`** in both repos: `PARALLEL_LLM` (Lead and Task
   breakdowns side by side, capped by what the model server serves) and the
   `parallel` panel in `jfi-web` and the fleet dashboard. Nothing is
   committed yet.
4. **Music player benchmark** (`benchmark/tasks/webapp/music_player`): a
   browser music player (library, search, play/pause, next/prev, shuffle,
   playlists that survive a reload), graded in a real Chromium through
   Playwright. Waiting for review before it's used.
5. **Dev in parallel.** imp runs one leaf at a time, so on the html runs it
   was most of the time (62 leaves, 1,086 s at `PARALLEL_LLM=1`). Leaves on
   different files with no `depends_on` between them could run side by
   side, the way Lead and Task now do. To settle first: which leaves may
   share a batch (files, `depends_on`, a test file two leaves share), the
   per-leaf checkpoint commits (`checkpoints.git` is one repo), the
   `mark_leaf_done` gate running tests while another leaf is mid-edit, and
   `execute_command` / background processes from two episodes at once.
   Then compare `PARALLEL_LLM` 1 vs 2 on the music player.
6. **The entry point is planned but never started.** Three of eight calc
   runs (serial and parallel) failed the same way: `main.py` defined
   `main()` and nothing called it. Once the entry file is split into
   functions, no node owns the `if __name__ == "__main__"` line, and the
   reviewer didn't run the program. Separately, one calc run lost `^`
   because the Architect's component descriptions said "+ - * /".
   The 2026-10-01 fix (the runbook's `entry`, `42d8316`) makes some node own
   the entry *file*; it doesn't make anything start the app from it.
7. **JFI doesn't exit on Ctrl+C after the pipeline completes.** All 14
   benchmark runs on 2026-10-02 were force-killed by the harness 60 s after
   `PIPELINE COMPLETE` ("graceful stop did not confirm in time"); 2026-10-01
   saw it once mid-planning too.
8. **Stray empty files in the project** (`barChart`, `applies`, `returns`,
   ... on the QA machine's stui run): probably shell commands with `->` or
   `>` inside quoted text, written as redirections. Unconfirmed -- needs that
   run's `.jfi/JFI.db`. The 2026-10-01 entry-point test project is still at
   `D:\git\jfi-bench\entry-check`.

## Fixed 2026-10-02

- **`PARALLEL_LLM`** (1-10): Lead and Task breakdowns of one level run side
  by side, as many as the role's model server serves at once -- LM Studio's
  loaded `parallel`, and no more than its context holds whole
  (`contextLength // CONTEXT_SIZE`); see `docs/phase-planner.md`. On the
  html tier with qwen3.8-27b-turbo on LM Studio (40k context, `CONTEXT_SIZE=20480`, so 2
  at a time) Lead/Task took 23.9 s per episode against 32.0 s serial; all
  six runs passed.

## Fixed 2026-10-01

- **Laya's score is the sizing question.** With `LAYA=1` the judge asks
  Laya's `english` checkpoint, for every node, "Can this be solved with 20k
  tokens?" given only the task and its description (the node's notes);
  P(yes) >= 0.4 is GOOD, otherwise BREAKDOWN. The decision is as before:
  Laya and the rule agree -> that's the verdict; they disagree and Laya's
  confidence >= `LAYA_MIN_CONFIDENCE` -> the LLM tie-break; otherwise the
  rule. On the POC's 67 nodes it ranks them at AUC 0.76 and is 73% right at
  the 0.4 cut-off (88% on nodes that needed a breakdown). Rerun
  `benchmark/laya_poc.py` on new runs to keep checking the cut-off.

## Dropped

- The fix-loop test on a real run (a reopened leaf). `reopen_leaf` and its
  `revert` stay unit-tested (`test_reviewer.py`, `test_checkpoint_tools.py`);
  the escalation path did run for real.

## Not planned

Set aside on 2026-10-01; kept so they aren't rediscovered.

- Ollama, llama.cpp and vLLM detection is untested against real servers
  (only LM Studio was checked live).
- AMD GPUs on Windows report "unknown"; Laya on Apple Silicon stays on the
  CPU (no `mps`). Decisions 9 and 10 in
  [`docs/create_env_sizing.md`](docs/create_env_sizing.md).
