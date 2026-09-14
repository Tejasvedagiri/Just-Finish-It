# Benchmark status and next steps

Status: `benchmark/` has been run against JFI for real, multiple times, against two different
local models. The context-size mismatch described below (the original reason for this doc)
was found and fixed; a full 9-task run against `google/gemma-4-12b` (correctly reloaded at
80128 context) completed with real crash-loop/recovery behavior observed and self-healed via
`STREAM_OUTPUT_CAP`/`REASONING_OUTPUT_CAP`. A second full run against
`qwen3.8-27b-ultra-uncensored-heretic-native-mtp-preserved` (40000 context) passed all 9
tasks cleanly -- 0 plan rewrites, 0 stalls, 0 crashes across every task. Since then the
benchmark has been substantially expanded: `polyglot` now spans Python + JavaScript (10
tasks, up from 3), and two new tiers were added -- `webapp` (React via CDN + jsdom behavioral
grading, and a real Next.js `npm run build` + static export) and `data_engineering` (pandas +
stdlib ETL tasks graded against a fixed dataset). 7 tiers, 20 tasks total
(`polyglot` x10, `terminal` x1, `html` x3, `webapp` x2, `data_engineering` x2, `story` x2;
`swebench` documented but still empty). Every new task/oracle was self-tested against both a
correct and a deliberately-wrong reference solution before being committed, same bar as the
original set.

Run output lives in `benchmark/runs/` (inside this repo, gitignored) by default now -- see
`run_all.sh`'s own comments for why that changed from an external path.

## 0. The context-size lesson (historical, already fixed -- kept for the next person who hits it)

This was the root cause of every task failing in an early ad-hoc eval (the old `test/_queue`
run, not this benchmark) and again in this benchmark's first real run: LM Studio loaded a
model with a context window smaller than what `.env`'s `CONTEXT_SIZE` told JFI to assume. JFI
compresses history against the ceiling it's *told*, so real requests blew straight through
the model's actual limit -- a clean 400, or a crashed backend serving an HTML 500 mid-session.

If a run degenerates into repeated `HTTP 500 -- the server returned an HTML error page`
events, check this first:

```bash
curl -s http://127.0.0.1:1234/api/v0/models | python3 -m json.tool
# look for "state": "loaded" and check the context length actually applied
```

Fix in whichever direction is easier: reload the model in LM Studio with an explicit context
length matching `.env`'s `CONTEXT_SIZE`, or lower `CONTEXT_SIZE` to match whatever the model
is actually loaded with. What matters is that the two agree.

## 1. Run the benchmark

```bash
cd /home/tejas/Desktop/git/Just-Finish-It
uv run build   # make sure dist/jfi reflects current source before trusting a run
nohup sh benchmark/run_all.sh > /dev/null 2>&1 &
disown
tail -f benchmark/runs/bench_run.log
```

Or drive tiers by hand, in order of cheapest/most-trustworthy first:

```bash
python3 benchmark/harness.py --tier polyglot         --projects-root benchmark/runs   # 10 tasks, strongest guarantee
python3 benchmark/harness.py --tier data_engineering --projects-root benchmark/runs   # 2 tasks, exact-value grading
python3 benchmark/harness.py --tier html             --projects-root benchmark/runs   # 3 tasks, no build tooling in the way
python3 benchmark/harness.py --tier webapp           --projects-root benchmark/runs   # 2 tasks, real npm/build tooling -- slowest, least reproducible
python3 benchmark/harness.py --tier terminal         --projects-root benchmark/runs   # 1 task
python3 benchmark/harness.py --tier story            --projects-root benchmark/runs   # 2 tasks, weakest grading -- see README's Evaluation methodology
python3 benchmark/score.py benchmark/runs
```

`--timeout` defaults to 2700s/task; the `webapp` tier's `nextjs_static` task in particular
needs its own `verify_timeout` (already set in its `task.json`, 300s) for `npm install && npm
run build` to finish -- don't confuse that with harness.py's session timeout, they're
different things (session timeout covers JFI actually doing the work; `verify_timeout` covers
`verify.command` itself, run after the session ends).

## 2. Read score.py's output, not just harness.py's pass/fail

The scorecard's `flags` list is the point of this benchmark over the old approach --
`plan_rewrite_count`, `stall_nudges`, and `crash_events` will immediately show whether a
failure is a genuine model-capability gap or an infrastructure problem (like #0 above) before
spending time re-reading raw transcripts.

## 3. Known gaps to close after the next real run

- **`swebench` has zero real instances.** `tasks/swebench/README.md` already documents the
  task shape and exactly what's missing (repo checkout/setup step, a real SWE-bench-Lite
  instance pulled from Hugging Face, a decision about how JFI's planner should behave
  against a pre-existing repo rather than an empty project root). Doing this properly is a
  session of its own, not a quick add.
- **No multi-run variance data yet.** One run per task says "did it work once," not
  "how reliable is this." Running each task 3x and reporting a pass rate (not just
  pass/fail) would be the honest next step before trusting any single number.
- **`story` has no quality signal, only completion gates.** See README's Evaluation
  methodology section -- an LLM-judge score is the real next step there, but deliberately
  not added yet since it introduces a second model's non-determinism into the grading.
- **`html`'s (and now `webapp`'s) checks are structural/behavioral, not visual.** A page or
  component could pass every check and still be ugly. A future visual check would mean
  rendering the page (a headless browser, or a screenshot + JFI's own `view_image`-style
  inspection) -- not attempted yet.
- **A third `polyglot` language** (Go or Rust, for a compiled-language signal neither Python
  nor JavaScript provides) is the next highest-value addition to that tier.

## Explicitly not doing yet

- Not touching the old `test/_queue` ad-hoc eval -- it's superseded by this benchmark, not
  merged into it.
- Not attempting the `swebench` tier's real instances until the above is done.
