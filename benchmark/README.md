# Benchmarking JFI

This replaces an earlier ad-hoc approach (7 hand-picked "build an app" prompts -- a
calculator, a portfolio homepage, a Snake game, two short stories, a character card -- run
manually against a local model and judged by eyeballing the transcript). That approach had
two real problems: there was no ground truth, so "did it work" was a judgment call every
time, and the tasks weren't anything another agent's reported numbers could be compared
against. This benchmark is built against the field's actual standards instead:

- **[SWE-bench](https://www.swebench.com/)** (`Verified`/`Lite`/`Pro` variants) -- the closest
  thing to an agreed standard for autonomous issue resolution: fix a real GitHub issue in a
  real repo, graded by that repo's own held-out tests.
- **[Terminal-Bench](https://www.tbench.ai/)** -- end-to-end terminal workflows (build,
  configure, run, debug) graded by actually running the result, not by inspecting files.
- **[Aider's polyglot benchmark](https://aider.chat/docs/leaderboards/)** -- function-level
  code-editing exercises across languages, graded against a hidden reference test suite the
  agent is told not to touch, including the "repair after a failing test" loop.

Every task here follows the same non-negotiable property those three share and the old
approach lacked: **an objective, held-out check the agent cannot see or influence, run after
the fact.** No task is scored by asking a human (or another LLM) "does this look right."

## The seven tiers

| Tier | Modeled on | What it tests | Populated? |
|------|-----------|----------------|------------|
| `polyglot` | Aider's polyglot benchmark | Clean-room implementation against a spec, verified by a hidden reference test suite | Yes -- 5 problems x 2 languages = 10 tasks (Python + JavaScript) |
| `terminal` | Terminal-Bench | Building a small app and having it actually work end-to-end (real stdin/stdout, not just "files exist") | Yes -- 1 task |
| `html` | Terminal-Bench's "does it actually work" spirit, applied to static frontend output | Producing correct, structured HTML/CSS with no build tooling in the way | Yes -- 3 tasks |
| `webapp` | Terminal-Bench + real framework tooling | A real npm-based frontend stack (React, Next.js) actually compiling/running, not just static markup | Yes -- 2 tasks |
| `data_engineering` | Aider-style hidden-test grading, applied to data work | Parsing/cleaning/aggregating a fixed dataset and matching exact reference values (pandas + stdlib) | Yes -- 2 tasks |
| `story` | No direct industry analogue -- see Evaluation methodology below | Following a creative-writing brief through JFI's own plan-driven pipeline | Yes -- 2 tasks |
| `swebench` | SWE-bench | Reading and correctly modifying an *existing* codebase to resolve a real issue | Not yet -- see `tasks/swebench/README.md` for exactly what's needed and how to plug real instances in |

`polyglot` now spans two languages. The three original problems (`difference_of_squares`,
`run_length_encoding`, `collatz_conjecture`) each have a Python task and a `_js` JavaScript
port (e.g. `run_length_encoding_js`), the JS side graded with Node's built-in `node:test` +
`node:assert` -- zero npm installs needed for the hidden suite itself. Two new, deliberately
harder original problems (`anagram_groups`: group-and-order under a tie-break rule;
`spiral_matrix`: classic but easy-to-get-subtly-wrong index bookkeeping) exist in both
languages too. All five are original problems in the same spirit as the exercises Aider's
benchmark draws from (small, unambiguous, algorithmic, easy to hide a test suite for) --
*not* verbatim Exercism exercises, which are copyrighted and weren't reproduced here.

`terminal`'s one task (`calc`) is the old benchmark's calculator prompt, kept because
it already fit Terminal-Bench's shape, but rewritten with a fixed I/O contract
(`main.py`, one expression per stdin line) and a real end-to-end checker (`verify/verify_calc.py`)
that pipes real input in and inspects real output, instead of the old prose "looks right"
criteria.

`html`'s three tasks (`recipe_card`, `pricing_table`, `faq_accordion`) are deliberately
**static** pages -- no npm, no build step, no dev server. That's not a simplification for
its own sake: the old ad-hoc eval's `homepage`/`stock-portfolio` tasks (Next.js, Vite) spent
most of their session time on scaffolding commands and `execute_command` approval prompts,
and never actually got benchmarked on the thing that mattered. A static page removes that
entire failure surface and lets the task test HTML/CSS structure directly.

`webapp` is where this benchmark deliberately takes on the exact fragility `html` was built
to avoid, because a real signal on modern frontend stacks was worth the cost:
- `react_counter`: a single index.html using React 18 loaded from a CDN (no npm/bundler at
  all), but graded *behaviorally* -- the checker loads the real page in `jsdom` (executing
  its actual `<script>` tags, CDN fetch included) and drives it with real `dispatchEvent`
  clicks, asserting the DOM actually updates. This is a step up in rigor from `html`'s purely
  structural checks, applied to a task that still has zero build-tooling surface to fail on.
- `nextjs_static`: a genuine two-page Next.js (pages router) project, `npm install && npm run
  build` is the real bar (a real compile), configured for `output: 'export'` so the result is
  static HTML with no dev server to keep alive during grading -- avoiding the port/process
  flakiness a live Next.js server would add, while still exercising real framework scaffolding,
  routing, and a real npm build.

`data_engineering` tasks provide a fixed input dataset alongside the hidden test suite (both
under `hidden_tests_dir`, visible to the model, do-not-edit) and check exact reference values
computed by actually running a correct implementation against that fixed dataset (never
hand-derived). `sales_summary` requires pandas (the verify command installs it if the model's
own session didn't); `log_pipeline` is stdlib-only, kept dependency-light on purpose so the
tier doesn't put all its eggs in one pip package.

`story`'s two tasks (`lighthouse_archive`, `monster_hunter_secret`) are original premises,
not the old eval's story prompts -- see Evaluation methodology below for why creative
writing is scored differently from every other tier here.

Extending any tier (more polyglot problems, more languages, real SWE-bench instances) means
adding another `tasks/<tier>/<id>/task.json` -- `harness.py` discovers tasks by walking the
directory, nothing else needs to change.

## Every task's shape

```
tasks/<tier>/<task_id>/
  task.json           # id, tier, prompt (the goal text handed to JFI), verify.command
  tests/               # optional: hidden_tests_dir -- copied into the project root BEFORE
                        #   JFI launches (so the model can see and run it, per its own
                        #   Testing phase, but is told not to edit it). JFI's final cleanup
                        #   phase is told to leave the deliverable's own tests/ alone, and
                        #   score.py's "hidden_tests" field/flag makes it visible if it
                        #   didn't (moved/deleted before verify.command ran)
  verify/              # optional: verify_files_dir -- copied in AFTER JFI's session ends,
                        #   never seen by the model (e.g. calc's end-to-end checker script)
```

`task.json` fields: `id`, `tier`, `title`, `language`, `prompt` (fed to JFI verbatim as its
goal), `hidden_tests_dir` / `verify_files_dir` (optional, see above), `verify.command` (run
in the project directory after the session; exit 0 = pass), `verify_timeout` (optional,
top-level -- not nested under `verify`; seconds allowed for `verify.command` itself, default
120; bump it for anything that installs packages or runs a real build, e.g. `webapp`'s tasks).

## Running it

```bash
uv run build   # make sure dist/jfi reflects the current source
python3 benchmark/harness.py --tier polyglot --projects-root /tmp/jfi-bench-runs
python3 benchmark/score.py /tmp/jfi-bench-runs
```

Or, to run every tier unattended in the background and score automatically when done, use
`run_all.sh` (logs to `$PROJECTS_ROOT/bench_run.log`, defaults to `benchmark/runs/` --
inside this repo but gitignored, so run output never risks being committed alongside the
benchmark definitions themselves):

```bash
nohup sh benchmark/run_all.sh > /dev/null 2>&1 &
disown
tail -f benchmark/runs/bench_run.log   # watch progress
tmux ls                                 # one bench-<task_id> session per task, live
```

`BENCH_TIER=polyglot sh benchmark/run_all.sh` runs just one tier; `BENCH_TIMEOUT=1200` lowers
the per-task timeout (default 2700s/45min).

`harness.py` drives JFI through a real tmux pty (its console is a full-screen TUI with no
non-interactive mode), answers the two startup prompts (session name, goal), and -- this is
the part that actually matters for an *unattended* run -- auto-answers two blocking menus
that would otherwise silently stall a run until timeout: the "Retry, or stop the run?" menu
after an LLM-backend failure, and JFI's own `execute_command` approval gate. Both were found
live, mid-development-of-this-benchmark, running the exact same task against a local model.

`score.py` then combines the harness's pass/fail with metrics scraped from JFI's own
artifacts (`run.log`, `history.jsonl.gz`, `plan.md`) -- see its docstring for exactly which
metrics and why. As one concrete example of why the process metrics matter even when the
final pass/fail is all that "counts": a real session run against `google/gemma-4-12b`
during this work never finished, and `score.py` reconstructs the whole failure automatically
from the raw artifacts -- 19 wholesale rewrites of `plan.md` instead of resuming it, 10
reasoning-only dead turns, 9 local-LLM-backend crashes -- the same diagnosis that took manual
transcript reading to find the first time, now a five-second `score.py` call. A task that
merely reports PASS/FAIL would have logged this run as an unremarkable failure and thrown
away the only interesting part.

## Evaluation methodology -- how each tier is actually graded, and how honest that grading is

Every tier reports a hard exit-code pass/fail from `verify.command`, but "objective" doesn't
mean "equally meaningful" across tiers. In decreasing order of how much a PASS actually
tells you:

- **`polyglot`**: the strongest guarantee here. A hidden test suite with known-correct
  expected values (verified against a reference implementation before being committed to
  this benchmark, not just written and trusted) either passes or doesn't. A PASS means the
  implementation is correct for every case the suite checks. No judgment calls anywhere in
  the grading path. Same guarantee on both the Python and JavaScript side -- the JS suites
  use Node's built-in `node:test`/`node:assert`, not a hand-rolled checker.

- **`data_engineering`**: the same strength as `polyglot` -- exact expected values checked
  against a fixed, hidden input dataset, computed by actually running a reference
  implementation rather than hand-derived. The one added variable is `sales_summary`'s pandas
  dependency, which `verify.command` installs itself (`pip install -q pandas`) so a PASS/FAIL
  never hinges on whether the model remembered to.

- **`terminal`** (`calc`): also a real functional guarantee, but a narrower one --
  `verify_calc.py` drives a handful of representative expressions through real stdin/stdout,
  not an exhaustive input space. A PASS means "the calculator handles the cases we thought to
  check," not "the calculator is bug-free." Good enough to catch "doesn't work at all" and
  "crashes on bad input," not subtle arithmetic edge cases.

- **`html`**: objective and deterministic (parsed via stdlib `html.parser`, no flaky headless
  browser), but structural rather than behavioral or visual. `data-*` attribute hooks confirm
  the required *content and DOM shape* exist -- 3 pricing tiers, 5 FAQ items each with a
  real answer, etc. -- but nothing here renders the page or judges whether it's actually
  well laid out, legible, or attractive. `faq_accordion` is the one case where structure and
  behavior coincide (native `<details>`/`<summary>` means correct markup *is* correct
  expand/collapse), which is why that task was designed around it deliberately rather than
  a JS-driven accordion nothing here could click-test.

- **`webapp`**: mixed rigor by task, and the tier where "objective" costs the most to get.
  `react_counter` is graded *behaviorally* -- `jsdom` executes the real page (including its
  CDN-hosted React/ReactDOM scripts) and drives it with real synthetic clicks, so a PASS means
  the interaction genuinely works, not just that the right tags are present. `nextjs_static`'s
  PASS means two things stacked: `npm run build` actually compiled the project (a real
  correctness bar an LLM can fail in ways `html` never exposes -- bad imports, config errors,
  React/Next API misuse), and *then* the exported static HTML has the required structural
  hooks (checked the same `data-*`-attribute way as `html`, just applied to framework output
  instead of hand-written markup). Neither task renders anything visually or judges layout --
  see Known limitations.

- **`story`**: the honest weak point, and worth being direct about rather than dressing it
  up. There is no ground truth for whether a short story is *good* -- `verify_story.py` only
  gates things that are objectively checkable (the file exists, has a title, is in the right
  length range, actually engages with the premise's concrete nouns, isn't contaminated with
  leaked chat-template tokens). A PASS means "the model did not skip or badly botch the
  assignment." It says nothing about prose quality, pacing, characterization, or whether the
  ending actually lands. The real industry answer here is LLM-as-judge scoring (as
  MT-Bench/Chatbot-Arena-style evals do) -- deliberately **not** implemented yet, since a
  judge call adds a second model's non-determinism and potential bias into what's otherwise
  a reproducible benchmark, and folding that in quietly would make the pass/fail numbers look
  more authoritative than they are. If a quality signal is wanted here, it should be a
  clearly-separate, clearly-labeled score, not blended into the same PASS/FAIL as everything
  else.

- **`swebench`**: not gradeable at all yet -- there's nothing populated to grade. Once real
  instances are added, this tier would actually leapfrog `polyglot` in rigor, since its
  FAIL_TO_PASS/PASS_TO_PASS tests are the same held-out oracle the entire field grades
  frontier agents against.

## Known limitations

- `polyglot` now spans Python and JavaScript (5 problems x 2 languages), but Aider's actual
  benchmark spans 6 languages specifically to separate "can't code" from "can't code in this
  language." A third language (Go or Rust would add a compiled-language signal neither current
  language has) is the next highest-value step.
- `swebench` has no real instances yet -- it's a documented extension point, not a working
  tier. See `tasks/swebench/README.md`.
- `story` has no quality signal, only assignment-completion gates -- see Evaluation
  methodology above. Its PASS rate measures "didn't skip/botch it," not "wrote well."
- `html` and `webapp`'s checks are structural/behavioral, not visual -- a page or component
  that passes every check could still be ugly or badly laid out. Nothing here renders a
  screenshot or judges appearance (JFI's own `capture_screenshot`/`view_image` tools could
  feed a future visual check, but that's not built).
- `webapp` is the most infrastructure-dependent tier: `react_counter`'s verify needs network
  access twice (once for the model's own CDN script tags to work at all, once for `jsdom` to
  fetch the same CDN URLs at grading time), and `nextjs_static`'s verify needs network access
  for `npm install` plus tens of seconds for a real compile. Both are self-tested against a
  correct and a deliberately-wrong reference solution (see below), but they're the least
  reproducible tasks here if the sandbox has no internet access or npm registry is unreachable
  -- unlike every other tier, which runs fully offline once the model's own session is done.
- `data_engineering`'s `sales_summary` needs `pip install pandas` to succeed at verify time
  (network-dependent, though typically already cached); `log_pipeline` has no such dependency
  by design, so the tier isn't a single point of failure.
- Only one dataset/task per `data_engineering` scenario exists so far (no variance data on a
  given fixed CSV/log) -- same "single run, not a pass rate" caveat as the rest of this
  benchmark; see `PLAN.md`'s "known gaps" for the project-wide version of this point.
- `verify.command` is trusted to run arbitrary shell in the project directory. Fine for a
  local sandboxed benchmark run; do not point this at untrusted task definitions.
