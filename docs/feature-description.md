# Branch `feature/description`

What this branch changed, and the end-to-end runs that checked it
(2026-09-30 to 2026-10-01). It replaces the long working `TODO.md` of that
time; open work is in the root [`TODO.md`](../TODO.md). Numbers in brackets
like (#23) are that list's item numbers, kept so older notes still match.

## End-to-end runs

All three ran through `benchmark/harness.py` (ConPTY on Windows) with the
onedir binary `dist/jfi/jfi.exe`, `qwen/qwen3.8-27b` in LM Studio at a 40960
context, settings from `uv run create-env` defaults, and the Streamlit
dashboard and fleet master on. Scored with `benchmark/score.py`.

| Run | Folder | Verify | Time | Leaves | Episodes | Overflows | Reopened | Tool errors |
|---|---|---|---|---|---|---|---|---|
| calc | `D:\git\jfi-bench\calc` | PASS 6/6 | 14 min 13 s | 10/10 | 22 | 0 | 0 | 2.4% |
| calc, `LAYA=1` | `D:\git\jfi-bench-laya\calc` | PASS 6/6 | 11 min 17 s | 10/10 | 19 | 0 | 0 | 2.6% |
| react_counter | `D:\git\jfi-bench\react_counter` | PASS 6/6 | 29 min 17 s | 17/17 | 26 | 0 | 0 | 4.5% |

What they showed:

- **The new planning pieces worked as designed.** The runbook got `script`,
  `src_dir`, `test_dir`, `test_naming` and a `test_one` example; every node
  carried notes and references; Dev's brief carried them (the referenced
  design contract inlined under "read first"); every leaf got a git
  checkpoint and `leaf_diff` lists them per file. `add_node` failed 0 of 19
  times on calc (28% on the QA machine before).
- **The binary.** ~11 s from launch to the goal prompt; Streamlit and the
  fleet run from it; Ctrl+C at idle stops it within 10 s and takes the
  dashboard down with it.
- **Laya in the binary.** With `LAYA=1` all 15 verdicts carry an `english`
  score and Laya agreed with the rule on 12. Its confidence stayed at
  0.37-0.50, under `LAYA_MIN_CONFIDENCE` (0.75), so the 3 disagreements went
  to the rule and the LLM tie-break never fired.
- **The reviewer on a web page (react_counter).** It ran the e2e, tried
  `check_page` on the page's `file://` URL (then refused), started its own
  `python -m http.server`, and `check_page` reported no console errors or
  failed requests, with a screenshot. It didn't need `browser`: the project's
  own Playwright tests click the buttons. After `stop_background_process` it
  also ran `taskkill /PID ... /T /F` -- the Windows process-group gap (open).
- **Plan text.** The react_counter Task notes opened with where to edit
  ("Replace the JFI comment at index.html L3. &lt;head&gt; holds only ..."),
  used HTML entities, and one node was "No new code expected -- this is the
  verification pass". Fixed (see Planner).
- **An escalation, for real** (react_counter with `LAYA=1` and the
  `multilingual` checkpoint, 2026-10-01): the runbook set `test_dir` to "."
  and `add_node`'s test-path check turned that into the folder "/", refusing
  every path for `test_index.py`; the Task planner escalated, the Lead redid
  the node (18 turns) and the Task planner then broke it down. The check is
  fixed (a root `test_dir` means no folder; regression test).
- **react_counter with `LAYA=1` / `multilingual` passed 6/6** in 52 min
  (9/9 leaves, 21 episodes, all finished), through the escalation above.
  Its reviewer loaded `browser` and used it with `check_page` and
  `leaf_diff` -- the first model use of `browser`, under the prompt that
  now asks it to click through every interaction the goal names.
- Not seen on any run: a reopened leaf (the reviewer's fix loop; a planned
  break-and-resume test was dropped, `reopen_leaf` stays unit-tested) or an
  overflow split. Note: setting that test up, the multilingual
  react_counter DB was copied without its WAL, so its cleanup episode's 14
  transcript messages are missing from `D:\git\jfi-bench-laya\react_counter`
  (the episode row, the CLEANUP_COMPLETE marker and the scored results are
  intact).

## Pipeline and Dev

- An episode that ends on its budget or turn cap gets a 3-turn wrap-up that
  can still `mark_leaf_done` before the leaf is split (#1): on an earlier calc
  run a finished leaf was re-split (~7 minutes).
- "2 turns left" warning before the turn cap (`TURNS_LEFT_WARNING`) (#2).
- Dev's rules: build only SCOPE (#3); run the leaf's test with the runbook's
  `test_one` through `execute_command`, `test_id` is only the id (#15);
  scratch code goes in a file under `.jfi/scratch/` run with the runbook's
  `script` entry, never inline `python -c` / heredocs (#17).
- `mark_leaf_done` refuses a whole command or a path as `test_id` (#16).
- Session history loads only session-level rows (`episode_id` NULL) (#5).
- **Git checkpoints per leaf** (#23): a private repo `.jfi/checkpoints.git`
  whose work tree is the project; the project's own git is never touched.
  `leaf_diff(leaf_id)` / `leaf_diff(path=...)` for the reviewer.
  `reopen_leaf(..., revert=true)` puts the leaf's files back to before it so
  Dev rebuilds a wrong approach (#32); refused when later work touched them.
- **Project memory** (#26): a new session starts from the previous session's
  runbook and lasting design (`session/project_memory.py`).

## Planner

- **The app's entry point is planned** (2026-10-01, after the QA machine's
  stui run: `index.html` imported `/src/main.js`, no node owned it, and Vite
  failed mid-imp). New runbook entry `entry` (the file the app starts from);
  the Architect makes it its own component that wires the others together,
  and `finish` refuses until a node's `files` include it or the project
  already has it.
- Nodes carry `notes` (what to implement and how) and `references` (design
  entries, source ranges, docs) (#29); the next layer's brief shows them,
  design entries inlined (#19).
- Where tests live is the runbook's: `src_dir`, `test_dir`, `test_naming`;
  `add_node` refuses a test file outside `test_dir` (#30).
- `depends_on` errors list the real ids; no false duplicates from "word ("
  (#18); nested data goes in JSON, not CSV cells (#21).
- **Notes say what to do.** RULES: notes start with what the item must do and
  why, locations go in references, plain text, running the tests is never a
  node, with a bad/good example from an unrelated project (an example about
  the same task was copied word for word in trials). `add_node` /
  `update_node` decode HTML entities and refuse notes that open with
  "Replace/Fill the JFI ...". Tried on LM Studio: 6 of 6 notes led with the
  job.

## Reviewer

- `search_code` and `list_dir` in its core set (#20): it had searched with
  `findstr` and got 99,395 characters back.
- A cut-short review gets one continuation episode.
- Web apps: start the app, `check_page` the view URL; a static page is
  checked as a `file:///` URL, no server.

## Tools

- `find_references` (#25), `apply_patch` (#27), `http_request` (#28),
  `check_page` (#24), `leaf_diff`.
- **`browser`** (optional pool, `tool/browser_session.py`): an interactive
  headless browser kept open across calls -- open, screenshot (attached for
  the model), find, click, type, press, scroll, text, back, close -- with
  elements addressed by `[ref]`, visible text or CSS selector. Elements with
  a pointer cursor count as clickable; a target that matches nothing fails at
  once; `file://` pages open too. Closed when the session ends.

## Dashboards (Streamlit and fleet)

- The Session DB tab lists the episode tables in both repos (#12).
- The Task | Judge table has Notes and References columns; the fleet's Plan
  checklist and a Node picker in Streamlit show the selected node's notes as
  points (`note_points`: numbered steps nest under the line before them,
  every other sentence is a bullet) and its references as a list.
- Streamlit refreshes every 20 s by default (slider 1-60).

## Laya

The judge's Laya score was checked against what really happened: over 62
nodes from 4 runs, a Task leaf was done by Dev in one episode (achievable),
an Architect or Lead node had to be broken down. AUC is how well Laya's
P(yes) ranks the two groups (0.5 = no signal).

| State and question | english acc / AUC | multilingual acc / AUC |
|---|---|---|
| current: goal, level, path, node, done_when, files; "clean enough?" A/B/C | 66% / 0.59 | 52% / 0.49 |
| Q&A: `input` = the task, `goal` = "Can this be achieved within 30720 tokens?", noul | 35% / 0.41 | 56% / 0.53 |
| Q&A, noul "one small step?" / "several files or functions?" | 60% / 0.60, 47% / 0.68 | 61% / 0.67, 42% / 0.47 |
| Q&A, `goal` = "Can this be created with 20k tokens?", noul | 48% / 0.61 | 61% / 0.73 |
| **task + description only, "Can this be solved with 20k tokens?", noul** (67 nodes) | 67% / **0.76** | 63% / 0.62 |

- The current state gives Laya little it can use: the goal is cut at 300
  characters, and `level`/`path`/`node` are labels it can't interpret. Its
  confidence stayed at 0.36-0.55, so the tie-break (0.75) never fired.
- A Q&A state raises confidence a lot (most answers over 0.75), but mostly
  as a one-sided bias: one checkpoint answers "no" to nearly everything, the
  other "yes".
- The best ranking (multilingual, "created with 20k tokens?", noul, AUC 0.73)
  still puts every answer at 0.94-0.999 "yes": Task leaves median 0.9956,
  broken-down nodes 0.9912. The best threshold scores 70% on the same data.
- Offline, `multilingual` on the current state agreed with the rule on 2 of
  the calc run's 15 nodes (`english`: 12) and called most good Task leaves
  REDO.
- **The sizing question works with `english`** (`benchmark/laya_poc.py`,
  report in [`laya_poc.md`](laya_poc.md)): with only the task and its
  description as `input` and "Can this be solved with Nk tokens?", median
  P(yes) is 0.56 for Task leaves vs 0.18 for broken-down nodes (AUC 0.76 at
  20k and 40k, 0.72 at 30k; 76-78% with a threshold fitted on the same
  data). Dropping `done_when`/files and the judge's labels is what made the
  difference. The budget number barely matters (P(yes) rises 20k -> 30k ->
  40k on only 20 of 67 nodes): Laya reads the task text, not the count. Its
  misses: a whole index.html with a long description looked small; tiny
  technical leaves (CDN script tags, test scaffolding, a "verification
  pass") looked big.
- **What the judge does now:** with `LAYA=1` it asks `english` exactly this
  (task + description, "Can this be solved with 20k tokens?", `noul`);
  P(yes) >= 0.4 is GOOD (73% on the 67 nodes, 88% on the ones that needed a
  breakdown; 0.5 gave 67%), otherwise BREAKDOWN, and the decision is as
  before: agree -> that verdict, a confident disagreement (>= 0.75) -> the
  LLM tie-break, otherwise the rule. P(yes) is stored on each
  `PlannerVerdict` (`probabilities: {"yes": ...}`). Laya no longer answers
  REDO. Checked through the real router on calc nodes: P(yes) 0.18 / 0.15
  for an Architect and a Lead node (agree), 0.80 for a Task leaf (agree).

## Binary and create-env

- `uv run build` is a onedir build, `dist/jfi/jfi(.exe)`, with the package
  metadata (`jfi --version`) (#7, #8); it removes an old onefile `dist/jfi`
  or `dist/jfi.exe` (#34). Since replaced: `bugfix/build` makes it a onefile
  build, `dist/jfi(.exe)`, on every platform.
- create-env: end-of-input stops the wizard with "nothing was written" (#9).
- Ctrl+C at idle exits on Windows; the dashboard's whole process tree is
  stopped (#13).
- `stop_background_process` works on Windows: the process starts in a new
  console process group and the stop ends its whole tree (`taskkill /T /F`)
  while the shell wrapper is still alive. On react_counter the reviewer had
  had to `taskkill` its own server. The suite now passes on Windows (803
  passed; the SIGKILL-fallback test is POSIX-only).

## Benchmark

- `score.py` reads `.jfi/JFI.db` (#31); the harness drives tmux on
  Linux/macOS and ConPTY on Windows (`uv run --with pywinpty`), finds the
  onedir binary, and on Windows runs verify commands through Git Bash with a
  real Python for `python3`.

## Repo

- The merged `feature/laya` branch was deleted locally (#14); it's still on
  `origin`.
