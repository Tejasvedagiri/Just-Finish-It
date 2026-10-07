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
   committed yet. `uv run create-env` now asks for `PARALLEL_LLM`, with the
   server's cap as the default.
4. **Test `create-env`'s `PARALLEL_LLM` on real servers.** Unit-tested
   only (recorded `lms ps` / `/props` output). To check by hand:
   - LM Studio: `lms load <model> --context-length 40960 --parallel 4`, run
     `uv run create-env`, pick LM Studio and that model. With
     `CONTEXT_SIZE=20480` the default should be 2 and the reason should say
     to lower `CONTEXT_SIZE` to 10240 or reload at 81,920 for 4; at
     `CONTEXT_SIZE=10240` it should be 4. Does `lms ps --json` really carry
     `parallel` on the QA machine's LM Studio version (macOS)?
   - LM Studio with the model not loaded: 1, with the `lms load ... --parallel N`
     hint.
   - llama.cpp: `llama-server -c 65536 --parallel 2`: the default is
     `min(2, 65536 // CONTEXT_SIZE)`. Newer llama.cpp may report the
     per-slot `n_ctx` in `/props`, not the total -- if so both this and
     `JFI.llm.parallel._llamacpp_slots` undercount.
   - Ollama and Anthropic: 1 and 4.
   - Non-interactive check (`uv run create-env < /dev/null`) with
     `PARALLEL_LLM=4` in `.env` against the 40,960 / parallel=4 load at
     `CONTEXT_SIZE=20480`: a warning that the planner will run 2.
   - Then run a session with the suggested value and compare the planner's
     "Lead episodes: N at a time" line with what create-env suggested.
5. **Music player benchmark** (`benchmark/tasks/webapp/music_player`): a
   browser music player (library, search, play/pause, next/prev, shuffle,
   playlists that survive a reload), graded in a real Chromium through
   Playwright. Waiting for review before it's used.
6. **Dev in parallel.** imp runs one leaf at a time, so on the html runs it
   was most of the time (62 leaves, 1,086 s at `PARALLEL_LLM=1`). Leaves on
   different files with no `depends_on` between them could run side by
   side, the way Lead and Task now do. To settle first: which leaves may
   share a batch (files, `depends_on`, a test file two leaves share), the
   per-leaf checkpoint commits (`checkpoints.git` is one repo), the
   `mark_leaf_done` gate running tests while another leaf is mid-edit, and
   `execute_command` / background processes from two episodes at once.
   Then compare `PARALLEL_LLM` 1 vs 2 on the music player.
7. **The entry point is planned but never started.** Three of eight calc
   runs (serial and parallel) failed the same way: `main.py` defined
   `main()` and nothing called it. Once the entry file is split into
   functions, no node owns the `if __name__ == "__main__"` line, and the
   reviewer didn't run the program. Separately, one calc run lost `^`
   because the Architect's component descriptions said "+ - * /".
   The 2026-10-01 fix (the runbook's `entry`, `42d8316`) makes some node own
   the entry *file*; it doesn't make anything start the app from it.
8. **JFI doesn't exit on Ctrl+C after the pipeline completes.** All 14
   benchmark runs on 2026-10-02 were force-killed by the harness 60 s after
   `PIPELINE COMPLETE` ("graceful stop did not confirm in time"); 2026-10-01
   saw it once mid-planning too.
9. **Stray empty files in the project** (`barChart`, `applies`, `returns`,
   ... on the QA machine's stui run): probably shell commands with `->` or
   `>` inside quoted text, written as redirections. Unconfirmed -- needs that
   run's `.jfi/JFI.db`. The 2026-10-01 entry-point test project is still at
   `D:\git\jfi-bench\entry-check`.
10. **Check the bottom-up imp and per-node evidence on a real run**
    (`feature/screenshots`, 2026-10-06). Every node now has a checkbox and a
    Dev turn: leaves first, then each parent (`VERIFY`), and under a ground
    truth every component, file and leaf captures its own case, with no
    compare leaves. Unit-tested only. On the portfolio-dashboard conversion,
    check: (a) Task really captures a case for every leaf without stalling on
    `finish` (a type-only or imports leaf may only have LLM or file evidence);
    (b) how many tokens the parents' check episodes cost next to the leaves;
    (c) how often a leaf's visual case fails only because the rest of the
    page isn't built yet, and whether `accept_difference` then hides real
    misses that the parent's or the reviewer's comparison has to catch.
    The fleet's checklist now gets `- [ ] 1 ...` for parents too (it was
    `- 1. ...`); check `parsePlanLines` in `Just-Finish-It-Fleet` still nests them.
11. **A long, structured story plans badly** (reported 2026-10-04, not yet
    reproduced: no model server was reachable from the session that took
    it). The goal, as given:

    > Create an action-packed story for 1 week. It should be split into
    > early-morning, morning, afternoon, evening, night and mid-night time
    > slots. Each session must follow the format:
    > `NARATOR: ""` / `CHAR_1: ""` / `CHAR_2: ""` / `*AMBIANCE*`

    That's 7 days x 6 slots = 42 sessions, each in a fixed four-part
    script format -- much bigger and more rigid than the two `story`
    benchmark tasks (one 1,500-2,500 word prose piece each). To do:
    - Run it and keep the `.jfi/JFI.db` (`uv run export-db`): what plan
      did the Architect / Lead / Task layers make? Expected, but unchecked:
      an outline (the document path, phase 9 in `docs/laya_impl_phases.md`)
      with one section per day and one `passage` leaf per slot, the
      cast and the format recorded once in the design so every passage
      uses the same characters and the same four parts.
    - Find where it goes wrong: the planner not taking the document path at
      all (treating it as code), slots merged or missing, the format not
      reaching each passage's Dev episode, or the days not following one
      plot.
    - The passage gate (`JFI.imp.dev._passage_problem`) checks only that the
      `JFI:` placeholder is gone and the word count; nothing checks a
      required *format*. A per-passage
      check for the `NARATOR:` / `CHAR_1:` / `CHAR_2:` / `*AMBIANCE*` parts
      may be needed, and the reviewer should check all 42 slots exist in
      order.
    - Add it as a `benchmark/tasks/story/` task (`verify_story.py` checking
      the 42 slots and the format) so the fix is measured, not eyeballed.

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
