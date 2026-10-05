# Old vs new: checking every task against a ground truth

**Status:** proposal, for review. Nothing here is built yet.

## The idea

Many goals come with a **ground truth**: something the new code has to match.
It might be a command (`bc`), the original HTML page, a screenshot or mockup,
an input file with its expected output, API docs, or the old program being
replaced.

When a goal has one, the planner plans how **every task** will be checked
against it, layer by layer, the way it already plans the work: the Architect
sets the guideline, each Lead refines it for its component, and Task puts each
check on the task that builds it. Then, in the imp phase, each time Dev finishes a task, that task's
code is run on the same input as the ground truth and the two answers are
compared. The task is done only when they agree, the same way it's done today
only when its unit test passes.

**The calculator's `add` task, against `bc`.** Dev writes `add`, its unit
test passes, and `mark_leaf_done` runs the task's case against `bc`:

```
case add
  1 + 1         new: 2         bc: 2         match
  2.5 + 0.25    new: 2.75      bc: 2.75      match
  -3 + 10       new: 7         bc: 7         match
add: 3 of 3 match -- leaf done
```

If `add` is wrong, the task is refused while Dev is still on it:

```
case add
  1 + 1         new: 2         bc: 2         match
  2.5 + 0.25    new: 2.7       bc: 2.75      MISMATCH
add: 1 of 3 differ -- leaf not done; fix it and call mark_leaf_done again
```

**A page's header, against a screenshot of the original.** Both are
screenshotted the same way (same width, same state), and Dev sees the pair:

```
case header (1280x800)
  old:  .jfi/screens/compare/header/old.png   (the original page)
  new:  .jfi/screens/compare/header/new.png   (what Dev just built)
  diff: 3% of pixels; text: same ("Portfolio", "Watchlist", "Holdings")
  [both screenshots attached]
```

The same layout, text and colours means the task is done. A missing menu item
or the wrong colour means Dev fixes it first.

**A revenue column, against the expected CSV.** The script runs on
`orders.csv`, and its `revenue` column is compared with the expected file's:

```
case revenue
  2026-01   new: 1204.50   expected: 1204.50   match
  2026-02   new:  988.10   expected:  988.10   match
revenue: 2 of 2 match -- leaf done
```

At the end, the reviewer runs every case again over the finished build, so a
later task that broke an earlier one is caught too.

## Why

JFI doesn't record a goal's ground truth today, or how to compare the new
work against it.

- The **Architect** records the stack, the components, the contracts and the
  runbook (`setup`, `run`, `view`, `test`, `e2e`, ...). Nothing says "this is
  the original, and this is how to check against it". Lead, Task and Dev each
  see only one node, so the Dev building a view has no pointer to the part of
  the original it replaces.
- **Dev** finishes a task when its own unit test passes. That test is written
  by the same model from the same understanding, so a misread requirement
  passes it.
- The **reviewer** checks that the build works (`e2e`, `check_page` console
  errors, `browser` clicks), not that it matches the original. A rebuilt page
  can pass with the wrong layout, missing cards or the wrong text, and by
  then many tasks have been built on top of the mistake.
- The model has tried to record a ground truth and had nowhere to put it. On
  the stui run (gemma) the Architect wrote `e2e = "Compare current view with
  portfolio-dashboard.html visually"`. That's a sentence, not a command, and
  `runbook_set` now refuses it (`runbook_tools.py`).

## The user gives the input; the Architect works out the rest

The user doesn't write a comparison spec. They name the ground truth, or
leave it in the project, in whatever form they have it:

| The user gives | The Architect works out |
|---|---|
| "Build a calculator. Compare it with `bc`." | `bc` is a command on PATH, so the check is behavioural. Which flags (`-l`), how its output looks, where it differs from what the goal wants, and expressions covering every operator and error case. |
| "Turn `portfolio-dashboard.html` into a Vite app." | A page, so the check is visual. Its views and states (tabs, cards, tables), which component rebuilds each, the viewport it was made for, which parts are sample data. |
| `input.csv` and `expected_output.csv` | A golden pair. Which program produces the output and how to compare two CSVs: by column name or position, whether row order counts, how close numbers must be, how blank cells and number formats compare. |
| A folder of PNG mockups, and "build this" | Which screen each one is (from the file names or the image), the viewport from the image size, and the route each one maps to. |
| "Make it look like https://example.com/pricing." | A live page. Screenshot it once at the start, so the ground truth doesn't change during the run, and keep that as the old side. |
| "Implement the API in `docs/api.md`." | Docs. Every endpoint's documented request and response is a case. |
| "Rewrite `legacy/report.sh` in Python." | An old program that still runs. Its inputs (`samples/`, or new ones), and its outputs captured once as the expected results. |
| "Same as the old one", with nothing named | Look for it: a `legacy/`, `old/` or `v1/` folder, another implementation, the git history. Ask if there's no candidate or more than one. |

The Architect's steps, before it records anything:

1. **Find the ground truth:** files and URLs the goal names; commands it
   names that are on PATH (`bc`, `jq`, `sqlite3`); reference-like files in
   the project (pages, images, PDFs, docs, input/expected pairs, a `legacy/`
   folder).
2. **Work out what kind it is.** A page, an image or a design export is
   **visual**. A command, docs, a schema, an old program or a golden pair is
   **behavioural**.
3. **Probe it** with a few real calls: run `bc` on a couple of expressions,
   screenshot the page, run the old script on one sample, read the CSV
   headers and first rows. This shows that it can be used here and how it
   behaves.
4. **Decide what must match and what may differ**, from the goal and the
   probes. `bc -l` prints `3.50000000000000000000` where Python prints `3.5`,
   and `4` where Python prints `4.0`; it truncates a fractional exponent with
   only a warning (`2 ^ 0.5` prints `1`); and its error wording is its own.
   Unless the goal says otherwise, all of that goes under "may differ", or
   every case would fail on differences nobody cares about. (Checked with
   GNU `bc`.)
5. **Set the guideline, not the cases.** For each component that rebuilds
   part of the ground truth, say which part it covers: the evaluator covers
   every operator and its errors; the watchlist view covers the Watchlist
   tab. Breaking that into cases is the Lead's and Task's job (section 3),
   the same way the Architect names components and leaves the files and
   functions to them.
6. **Record it** (below): the reference entry, `compare_one` and
   `compare_all`, and each component's part in its node's `cases` field
   (section 3).
7. **Ask only when it can't work it out:** no ground truth where the goal
   clearly expects one, two candidates, a Figma link with no export, a page
   behind a login. Without an answer, it records its best guess as an
   assumption, and the reviewer reports the unconfirmed cases as *not
   checked*, never as passed.

## The design

### 1. The ground truth: a `reference` design entry

There's a new design kind, `reference`, next to `stack`, `component` and
`contract` (`design_tools.KINDS`). Each entry records:

- **where** the ground truth is (a path, a URL, a command);
- **which part each component covers** (the guideline; the cases come
  later, from Lead and Task);
- **what must match** and **what may differ**.

```
design_set("reference", "bc",
  "The bc command (bc -l). The evaluator covers + - * / ^ and its errors (divide by zero, bad "
  "input); the REPL in main.py covers the loop and exit. Must match: each result to 10 decimal "
  "places; an error is one line, no traceback. May differ: 4 vs 4.0, trailing zeros, error "
  "wording, a fractional ^ (bc truncates it).")

design_set("reference", "dashboard_page",
  "portfolio-dashboard.html (project root). Viewport 1280x800. The shell covers the header; the "
  "summary view the summary cards; the watchlist view the Watchlist tab (after 'click Watchlist'); "
  "the holdings view the Holdings tab (after 'click Holdings'). Must match: the layout, the text, "
  "the colours. May differ: the numbers (sample data).")

design_set("reference", "expected_summary",
  "data/orders.csv -> data/expected_summary.csv. The aggregation component covers the month, orders "
  "and revenue columns; the writer and main.py cover the whole file. Must match: the rows, sorted by "
  "month; each value; revenue to 0.01. May differ: column order, line endings.")
```

Nodes cite it in `references`, the way they already cite contracts
(`reference:dashboard_page`, plus the original's lines, e.g.
`portfolio-dashboard.html L120-188`). That's how Dev knows what it's
rebuilding without reading the whole original.

### 2. The runbook: `compare_one {case}` and `compare_all`

There are two new runbook entries, alongside `test_one` and `e2e`:

- `compare_one`: how to check **one input** of a case against the ground
  truth, with an `{input}` placeholder (and `{case}`, the case's name), the
  way `test_one` has `{test_id}`. The gate runs it once for each of a case's
  inputs;
- `compare_all`: every case at once, for the reviewer.

A **case** is a name plus its inputs: `add` with `1 + 1`, `2.5 + 0.25` and
`-3 + 10`; or, for a page, `watchlist_tab` with the steps `click Watchlist`.
The cases are stored on the leaves (section 3), so the check script needs no
case list of its own.

```
runbook_set("compare_one", "python3 scripts/compare_bc.py {input}",
  notes="One input from a case of reference:bc, e.g. input='1 + 1'. Sends it to `python3 main.py` "
        "and to `bc -l` (scale=10), prints both answers, and exits 1 if they differ.")
runbook_set("compare_all", "python3 scripts/compare_bc.py --all .jfi/compare/cases.json",
  notes="Every leaf's cases and inputs; JFI writes cases.json from the plan before the review.")

runbook_set("compare_one",
  "compare_screens old=file://{project}/portfolio-dashboard.html new={view}/ viewport=1280x800 "
  "case={case} steps={input}",
  notes="One state of reference:dashboard_page, e.g. case=watchlist_tab, input='click Watchlist'.")
```

A project has one `compare_one` and one `compare_all`. A goal with two
ground truths (a page and its API docs) uses a script that sends each case
to the right one, or a `case` prefix (`page:header`, `api:get_user`). That's
decision 1.

### 3. The cases come from the three planning layers

The checks are planned the way the work is. Each layer refines the one
above it and only sees its own node, and the judge between layers
(`docs/phase-planner.md`) looks at its output as it does today.

| Layer | What it plans today | What it adds for the ground truth |
|---|---|---|
| **Architect** | The components, the design, the runbook | **The guideline:** the reference entry (where, what must match, what may differ), `compare_one` / `compare_all`, and in each component's `cases` field the part of the ground truth it covers (`part: + - * / ^ and their errors`). |
| **Lead** (one component) | The component's files, their stubs and test files | **Its own guideline:** the cases for each of its files, named within the part the Architect gave the component, e.g. `evaluator.py`: `add`, `subtract`, `multiply`, `divide`, `power`, `divide_by_zero`, `bad_input`. They go in each file node's new `cases` field. A part it can't cover with its files is an `escalate`, as today. |
| **Task** (one file) | One leaf per stub, each with one test case as `done_when` | **The OK:** each of the file's cases goes to the leaf that builds it, with its inputs (`add`: `1 + 1`, `2.5 + 0.25`, `-3 + 10`). Its `done_when` test case uses one of those inputs, with the ground truth's answer as the expected output (`evaluate("1 + 1") == 2`, as `bc` gives), not a value the model works out itself. A case no stub can build is an `escalate`. |

Each layer's output is checked mechanically, the way the node tools already
check files, `depends_on` and duplicates, so no case is dropped between
layers:

- **Architect's `finish`:** the reference entry, `compare_one` (with
  `{input}`) and `compare_all` exist (when the goal names a ground truth,
  decision 2).
- **Lead's `finish`:** a component with a part (its `cases` field) has at
  least one case on its file nodes, and no case name is used twice.
- **Task's `finish`:** every case on the file node is on exactly one leaf,
  with at least one input. When a leaf is split later (`TASK_SPLIT`), each of
  its cases goes to exactly one new leaf.

A `cases` field is added to `Leaf` (a column, so `_ensure_columns` in
`models/db.py` too). On a component it holds the part (the Architect's
guideline); on a file node, the case names (the Lead's); on a leaf, each case
with its inputs (Task's). A task with no case (shared plumbing, a config file) is
gated by its unit test alone.

The calculator, through the three layers:

| Layer | Output |
|---|---|
| Architect | Components: evaluator ("covers + - * / ^ and its errors, against bc"), REPL ("covers the loop and exit"), project (the check script `scripts/compare_bc.py`). The reference entry and `compare_one` / `compare_all`. |
| Lead (evaluator) | `evaluator.py` with stubs `evaluate`, `parse`; cases `add`, `subtract`, `multiply`, `divide`, `power`, `divide_by_zero`, `bad_input`. |
| Lead (REPL) | `main.py` with stub `main`; cases `repl_loop`, `exit`. |
| Task (`evaluator.py`) | The leaves below, each case on one of them. |

| Leaf (built by Dev) | Unit test (`done_when`, from the ground truth) | Its cases and inputs |
|---|---|---|
| `evaluate()`: + - * / | `evaluate("7 / 2") == 3.5` | `add`: `1 + 1`, `2.5 + 0.25`, `-3 + 10`; `subtract`, `multiply`, `divide`: three each |
| `evaluate()`: ^ | `evaluate("2 ^ 10") == 1024` | `power`: `2 ^ 10`, `3 ^ 0`, `-2 ^ 3` |
| `evaluate()` errors | `evaluate("1 / 0")` prints one error line | `divide_by_zero`: `1 / 0`; `bad_input`: `two + 2`, `1 +` |
| `main()` REPL loop | the loop reads until `exit` | `repl_loop`: three lines then `exit`; `exit`: `quit` |

### 4. The gate: `mark_leaf_done` checks the task against the ground truth

When Dev calls `mark_leaf_done`:

1. Its unit test runs through `test_one`, as today.
2. Then `compare_one` runs for each input of each of the leaf's cases: a
   behavioural case as a command (its exit code), a visual case by calling
   `compare_screens` inside JFI (it's a tool, not a program).
3. Every case matches: the leaf is done. A case differs: the leaf is refused
   with both answers (or both screenshots attached), as in the examples
   above, and Dev fixes it in the same episode. This counts against the
   leaf's attempts like a failing test.
4. A case whose other half isn't built yet (`full` before the CSV writer
   exists, a tab before its view) is re-queued, the way a test failing on
   another function's `NotImplementedError` is today.

### 5. The review: `compare_all`

After `e2e`, the reviewer runs `compare_all`. A failing case names its leaf
(the leaf's `cases`), which the reviewer reopens with the case's output and
screenshot paths in the fix note. A case that can't run here (no browser, no
Figma export) goes in the review report as *not checked*, never as passed.

### 6. The two kinds of check

**Behavioural:** a command whose exit code is the answer, printing both
sides. The script behind it (`scripts/compare_bc.py`,
`tests/test_docs_examples.py`, `scripts/compare_csv.py`) is code, so it's
planned like any other work: a node under the `project` component, built
first (the leaves that use it `depends_on` it). Following the existing
runbook rule, *running* a check is never a node.

**Visual:** a new `compare_screens` tool, built from `check_page`'s headless
browser (Playwright) and `view_image`'s attachment:

1. Render the old side (open the URL or file at the viewport and replay the
   case's steps, or load the image as it is).
2. Render the new side the same way. `{view}` and `{project}` are filled
   from the runbook's `view` entry and the project root.
3. Save `old.png`, `new.png` and a pixel `diff.png` under
   `.jfi/screens/compare/<case>/`.
4. Return the share of pixels that differ, a diff of the visible text, any
   console errors on the new side, and both screenshots attached.

The tool doesn't decide pass or fail on its own. Its numbers are evidence,
and the model judges the pair against "must match / may differ" (decision 3).

## Kinds of ground truth

| Ground truth | New code | One case is | Check |
|---|---|---|---|
| A command (`bc`, `jq`, `sqlite3`) | A program or function | One input through both | behavioural |
| Input and expected output files (CSV, JSON, text) | A pipeline or script | One file pair, or one output column | behavioural |
| Old implementation, in any language | The rewrite | One input through both | behavioural |
| The old implementation's tests | The rewrite | One old test | behavioural |
| API docs (Markdown, OpenAPI) | A service | One documented example | behavioural |
| GraphQL schema, `.proto`, JSON Schema | A service or file output | One operation or file, validated | behavioural |
| Recorded traffic (HAR, request logs) | A service | One recorded request, replayed | behavioural |
| A CLI's `--help`, man page, terminal transcripts | A CLI | One recorded command | behavioural |
| A spreadsheet's formulas | Code | One sheet's inputs | behavioural |
| A database schema or dump | ORM models, migrations | One table, both schemas dumped | behavioural |
| A build config (webpack, `setup.py`) | The migrated build | One output (page, entry point, package file) | behavioural |
| An HTML page or prototype | A web app | One state at one viewport | visual |
| A mockup, screenshot or wireframe photo | A web app | One screen (a wireframe: layout only) | visual |
| A Figma design | A web app | One frame, as a PNG export (decision 5) | visual |
| A live website | The rebuilt site | One page | visual |
| Mobile mockups, breakpoints | A responsive app | One screen at one width | visual |
| A chart image (Excel, matplotlib) | A dashboard chart | One chart | visual |
| A PDF layout (invoice, report) | Generated output | One page, rendered to PNG | visual |
| HTML email templates | New templates | One email at client width | visual |
| Storybook / design-system screenshots | New components | One component state | visual |
| A screen recording or GIF | An interaction | One step (`extract_video_frames`) | visual |
| A screenshot of an old terminal UI | A prompt_toolkit TUI | Out of scope for now: no headless TUI capture | -- |

## The benchmark tasks, each with a ground truth

Every task in `benchmark/tasks/` has a ground truth its goal could name.
Variants that name it are how the feature gets measured (Build notes).

| Task | Ground truth | One case is |
|---|---|---|
| `terminal/calc` | `bc -l` | One operator's expressions, or an error input |
| `data_engineering/log_pipeline` | `grep` / `awk` / `sort \| uniq -c` over the same log | One output key (`total_lines`, errors by service, ...) |
| `data_engineering/sales_summary` | An expected-output file, or `sqlite3` with `SUM(quantity*unit_price) ... GROUP BY region` | One summary value |
| `polyglot/*` | The other language's twin (`_js` vs Python) | One input through both |
| `polyglot/collatz_conjecture` | OEIS A006577 (the step counts for n = 1, 2, 3, ...) | One range of n |
| `polyglot/difference_of_squares` | The closed forms, (n(n+1)/2)² and n(n+1)(2n+1)/6, in `bc` | One function over n = 1..1,000 |
| `polyglot/run_length_encoding` | A one-line `itertools.groupby` encoder | One string (plus `decode(encode(x)) == x`) |
| `polyglot/spiral_matrix` | The spiral printed in the goal (n = 4), or the twin | One n |
| `html/pricing_table`, `recipe_card`, `faq_accordion` | A mockup PNG shipped with the task | One state at 1280 px (the FAQ also with one item open) |
| `webapp/react_counter` | A plain-JS counter page | One state: before, and after 3 clicks |
| `webapp/nextjs_static` | A two-page static HTML version | One page, plus the nav link followed |
| `webapp/music_player` | `music/library.json` and the WAV files | One song's title, artist and duration (vs the WAV's real length) |
| `story/*` | A sample passage in the wanted format (e.g. the `NARATOR:` / `CHAR_1:` script format in TODO item 10) | One section's structure; the tone is the reviewer's read |

## Decisions to review

1. **One `compare_one` per project.** A goal with two ground truths (a page
   and its API docs) prefixes its cases (`page:header`, `api:get_user`), and
   the command sends each to the right check. *Proposal:* yes. One entry
   keeps the gate as simple as `test_one`'s.
2. **When it's required.** *Proposal:* the Architect's `finish` requires
   `compare_one` and `compare_all` only when the goal names a ground truth:
   a file in the project with a `.html`, `.png`, `.jpg`, `.svg`, `.pdf`,
   OpenAPI `.yaml`/`.json` or docs `.md` extension; an input/expected pair; a
   command it names that's on PATH; or a Figma URL. That's a mechanical
   check, like `entry`, so a prompt rule alone can't skip it.
3. **Pass or fail on screenshots.** A pixel threshold is brittle: fonts,
   anti-aliasing and sample data all differ. *Proposal:* the model judges the
   attached pair against "must match / may differ". The pixel share and the
   text diff are evidence, and a large text diff (a missing heading or card)
   is flagged as missing work.
4. **States beyond the first screen** (tabs, dialogs, scrolled sections).
   *Proposal:* a case's `steps`: a short list of `browser`-style actions
   (`click <text>`, `type <field> <text>`, `scroll`), replayed on both sides.
5. **Figma.** Exporting frames from a Figma URL needs an API token.
   *Proposal:* not at first. The goal supplies PNG exports (or the Architect
   asks for them), and the reference entry names the frames. A `FIGMA_TOKEN`
   export can come later.
6. **Where a case's inputs come from.** *Proposal:* Task writes them, since
   it's the layer that sees the stub, and the ground truth gives every
   answer. Nobody writes an expected value by hand, which is the point: a
   hand-written expected value carries the same misunderstanding as the
   code. The exception is a ground truth that is already a list of answers
   (an expected-output CSV, documented examples), where the inputs come with
   it.
7. **Checked at every task, not only in review.** *Proposal:* yes, for every
   leaf with cases; that's the point. The cost is one comparison per leaf:
   a command, or a screenshot pair (a few seconds).
8. **The Architect's probes.** Step 3 needs `execute_command` and a way to
   look at a page, and the Architect has neither today (`episode/roles.py`;
   it "writes nothing to disk"). *Proposal:* give it `execute_command` for
   read-only probes and `compare_screens`, and keep what the probes produce
   (screenshots, the old program's outputs) under `.jfi/`, never in the
   project.
9. **Asking the user.** No model-facing tool asks the user anything today:
   the console asks for the goal at the start, and `!` interjections reach
   whichever episode reads them first. *Proposal:* a narrow `ask_user` for
   the Architect only, one question per run, offering the candidates it
   found. With no answer (or `AUTO_APPROVE_COMMANDS`, meaning nobody is
   watching), it goes on with its best guess, recorded as an assumption.

## Build notes (once agreed)

- `tool/design_tools.py`: `KINDS` gains `reference`.
- `tool/runbook_tools.py`: `compare_one` and `compare_all` join
  `COMMAND_ENTRIES`. `_starts_with_a_program` accepts `compare_screens`.
  `{project}` and `{view}` are new placeholders; nothing expands one runbook
  entry into another today.
- `tool/compare_tools.py`: `compare_screens`, from `check_page`'s Playwright
  code and `view_image`'s attachment, saving under `.jfi/screens/compare/`.
  It goes in the reviewer's and the Architect's core sets.
- `planner/prompts.py`: the Architect sets the guideline (the reference,
  `compare_one` / `compare_all`, each component's part); the Lead names each
  file's cases; Task puts each case on a leaf with its inputs and takes
  `done_when`'s expected output from the ground truth. `planner/loop.py` and
  `planner/nodes.py`: each layer's `finish` check from section 3, and a split
  keeps every case.
- `models/leaf.py`: a `cases` column, plus `_ensure_columns`.
- `imp/dev.py`: `mark_leaf_done` runs `compare_one` for each case after the
  unit test, and refuses or re-queues as in section 4.
- `review/prompts.py`: `compare_all` after `e2e`.
- Tests:
  - a goal naming an HTML file refuses the Architect's `finish` without
    `compare_one`; a Lead whose component has a part but no cases is refused;
    a Task whose file has a case on no leaf is refused;
  - a leaf whose case mismatches is refused by `mark_leaf_done`, and one
    whose case matches is accepted (with a real `bc` call);
  - `compare_screens` on two local `file://` pages returns both images and a
    diff;
  - the role prompts name each step.
- Docs: `phase-planner.md`, `phase-imp.md`, `phase-reviewer.md`,
  `plan-tree.md` (the `cases` field and `reference:` citations).
- Benchmarks: a variant of each task whose goal names its ground truth (calc
  with `bc` and `sales_summary` with an expected-output CSV first). The
  harness checks that the runbook has `compare_one`, that every leaf's cases
  ran at its `mark_leaf_done`, and that the reviewer ran `compare_all`,
  alongside the task's own `verify`.
