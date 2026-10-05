# Old vs new: checking the build against the reference it came from

**Status:** proposal, for review. Nothing here is built yet.

## The objective

When a goal comes with a reference (something the new code has to match),
the **Architect plans how every task will be checked against it once it's
implemented**, the same way it plans today how every task is unit-tested.
That plan is recorded in the runbook. Then:

- each plan item knows which part of the reference it has to match;
- Dev can't mark a task done until that task's own check against the
  reference passes, exactly as it can't today until the task's unit test
  passes;
- the reviewer runs every check again over the finished build.

Every check runs **post-implementation**: on each task right after its code
is written (at its `mark_leaf_done`), then on the whole build in review. The
only thing the Architect runs beforehand is a few probes of the reference
itself, to learn how it behaves. A mismatch is caught on the task that caused
it, while it's being built, not in one big comparison at the end.

The examples below (`bc` for a calculator, an HTML page for a Vite app, an
input CSV and its expected output, API docs for a service) are kinds of
reference; the mechanism is the same for all of them.

## The problem

Many goals arrive with a **reference**: the thing the new code has to match.

Ones you check by **looking** (old and new screenshotted side by side):

- An existing HTML page or prototype to turn into an app (the stui runs: a
  static `portfolio-dashboard.html` rebuilt as a Vite app).
- A mockup: an image, a screenshot, a Figma design, a hand-drawn wireframe
  photo (where only the layout counts).
- A live website to rebuild or migrate: an old jQuery site rebuilt in
  Next.js, compared page by page against its URL.
- Mobile mockups at phone width, or the same page at several breakpoints.
- A chart to reproduce: an Excel or matplotlib chart image that a new
  dashboard's chart has to match.
- A print layout: a PDF invoice, report or certificate that generated output
  has to look like (each PDF page rendered to an image first).
- HTML email templates rebuilt in a new system (e.g. MJML), compared at
  email-client width.
- Component screenshots from a design system or Storybook that new
  components have to match.
- A screen recording or GIF of an interaction to reproduce (a menu opening,
  a form's error states), compared frame by frame with
  `extract_video_frames`.

Ones you check by **behaviour** (same input, compare the output):

- API docs (Markdown, OpenAPI) that a Python service has to implement.
- An API schema to implement: a GraphQL schema, gRPC `.proto` files, a JSON
  Schema that responses or files must validate against.
- Recorded traffic from the old service (a HAR file, request logs) replayed
  against the new one.
- Sample code: an old implementation, in this language or another, that the
  new one replaces (a Bash script rewritten in Python, a Python library
  ported to Rust).
- The old implementation's own test suite, kept and run against the new one.
- A CLI's `--help` text, man page or recorded terminal sessions (commands
  and their output) that a new CLI has to reproduce.
- Golden files for a data pipeline: a sample input CSV and the expected
  output CSV or report.
- A spreadsheet's formulas to port to code: the same inputs have to give the
  same numbers.
- A database schema or dump: new ORM models or migrations must produce the
  same tables, columns and constraints (both schemas dumped and diffed).
- A build config to migrate (webpack to Vite, setup.py to pyproject): the new
  build must produce the same pages, entry points or package contents.

JFI's planner never records that reference, or how to compare the new work
against it:

- The **Architect** records the stack, components, contracts and the runbook
  (`setup`, `run`, `view`, `test`, `e2e`, ...). Nothing in the design or
  runbook says "this is the original; this is how to put old and new side by
  side". Lead, Task and Dev each see only one node, so a Dev building a view
  has no pointer to the part of the original it replaces.
- The **reviewer** checks that the new app works (`e2e`, `check_page`
  console errors, `browser` clicks), not that it matches the original. A
  rebuilt page can pass with the wrong layout, missing cards or the wrong
  text.
- The model tried to record it once and had no place to put it. On the stui
  run (gemma) the Architect wrote `e2e = "Compare current view with
  portfolio-dashboard.html visually"`. That's a sentence, not a command, and
  `runbook_set` now refuses it (`runbook_tools.py`). The intent was right;
  there was nowhere to record it.

## From the user's side: give the input, JFI works out the rest

The user shouldn't have to write a comparison spec. They give a goal and,
somewhere in it or in the project, the reference, in whatever form they have
it:

| The user gives | JFI has to work out |
|---|---|
| "Build a calculator in Python. Compare it with `bc`." | `bc` is a command on PATH, so the comparison is behavioural. Which flags (`-l`), what its output looks like, where it differs from the goal (formatting, fractional `^`), and a set of expressions that covers the goal's operators and its error cases. |
| "Turn `portfolio-dashboard.html` into a Vite app." | The file exists and is a page, so the comparison is visual. Which views and states it has (tabs, the watchlist, the summary cards), which component rebuilds each, what viewport it was designed for, and which parts are live data that may differ. |
| A folder of PNGs dropped in the project, and "build this" | They're mockups, and which screen each one is (from the file names or what's in them). The viewport from the image size. Each one is matched to a route in the new app. |
| "Make it look like https://example.com/pricing." | A live page: open it and screenshot it once at the start (so the reference doesn't change under the run), and save it in the project as the old side. |
| "Here's `input.csv` and the `expected_output.csv` it should produce." | A golden pair: once the code is implemented, run it on the input and compare its output with the expected file. Which program and command produce the output, and how to compare two CSVs: by column name or position, whether row order counts, how close two numbers must be, how blank cells and number formats (`1,000.50`, `1000.5`) compare. |
| "Implement the API in `docs/api.md`." | Markdown docs. Pull out every endpoint and its request and response examples; those examples are the test cases. |
| "Rewrite `legacy/report.sh` in Python." | An old implementation that can still run. Find its inputs (`samples/`, or make some), run it once to capture its outputs as the expected results, and diff the new program against them. |
| "Same as the old one", with no file named | Look for it: a `legacy/`, `old/` or `v1/` folder, a second implementation, the project's git history. Ask only if there's no candidate, or more than one. |

That's the Architect's job, before it records anything. Its steps:

1. **Find the reference.** Files and URLs the goal names; commands it names
   (`bc`, `jq`, `sqlite3`) that are on PATH; reference-like files in the
   project (pages, images, PDFs, OpenAPI/Markdown docs, a `legacy/` folder).
2. **Work out what kind it is.** A page, an image or a design export is
   visual. A command, docs, schemas, an old program or golden files are
   behavioural.
3. **Probe it** with a few real calls before trusting it: open the page and
   screenshot it, run `bc` on a couple of expressions, run the old script on
   one sample, read the docs' first endpoint. That proves the reference can
   be used here, and the probes show how it behaves.
4. **Work out what must match and what may differ** from the goal and the
   probes. With `bc`, the probes show `4` vs `4.0`, the trailing zeros, and
   `2 ^ 0.5` giving `1` with a warning, so those go under "may differ" (or
   the goal says otherwise). On a page, the numbers in its tables are usually
   sample data.
5. **Decide the cases.** The states to screenshot, the expressions to send,
   the inputs to replay: enough to cover everything the goal names, plus its
   error cases.
6. **Record it** (the reference entry and its cases, `compare_one` /
   `compare_all`, the nodes for any check script) and point the components
   at it.
7. **Ask only when it can't work it out:** no reference found where the goal
   clearly expects one, two candidates, a Figma link with no export, a page
   behind a login. Until it gets an answer, the comparison stays recorded as
   an assumption, and the reviewer reports it as *not checked*, never as
   passed (decision 8).

The probes (step 3) are tool calls the Architect already has (`read_file`,
`list_dir`) plus ones it would need: `execute_command` (to run `bc` or the
old script) and `check_page` / `compare_screens` (to look at a page). Today
its core set has neither (`episode/roles.py`).

## What we'd add

When the goal has a reference, the **Architect records three things**:

1. **The reference:** what it is and where it lives. This is a design entry,
   so every role can pull it.
2. **How to compare one case against it:** the runbook's `compare_one`
   entry, with a `{case}` placeholder, exactly like `test_one` and its
   `{test_id}`. That's what makes per-task checking possible (section 3).
3. **The cases**, and which component each belongs to: every screen state,
   expression, endpoint example or input the build has to match. These are
   listed in the reference entry; Lead and Task hand them down to the leaves
   that build them.

Later roles then use those entries:

- **Dev** runs its leaf's case (`compare_one`) before `mark_leaf_done`
  accepts the leaf.
- **The reviewer** runs every case (`compare_all`) over the finished build.

### 1. The reference: `design_set("reference", <key>, ...)`

A new design kind, `reference`, next to `stack`, `component`, `contract`, ...
(`design_tools.KINDS`). Each entry records:

- **where** it is: a path in the project, or a URL;
- **what** it is (an HTML page, a PNG mockup, OpenAPI docs, an old CLI);
- **which components** it's the reference for;
- **what must match** (the layout, the text, the colours, the response
  shape, the output) and **what may differ** (live data, timestamps, fonts the
  new stack doesn't ship).

Examples, with their cases, are in section 2.

Nodes point at it in `references`, the way they already cite contracts
(`reference:dashboard_page`, plus the source lines, e.g.
`portfolio-dashboard.html L120-188`). That's how a view's Dev knows what it's
rebuilding without reading the whole original.

### 2. The cases, and the two kinds of check

A **case** is one thing the build has to match: one screen state, one
expression, one documented request, one input file. Each is named (`divide`,
`watchlist_tab`), listed in the reference entry against the component that
builds it, and run on its own by `compare_one` (section 3). There are two
kinds, depending on the reference.

**Visual (a page, an image, a design export).** A case is one state, shot
the same way on both sides: a viewport, and the steps to reach it.

```
design_set("reference", "dashboard_page",
  "portfolio-dashboard.html (project root), the original static page. Viewport 1280x800. Cases: "
  "header (the shell); summary_cards (the summary view); watchlist_tab, steps 'click Watchlist' "
  "(the watchlist view); holdings_tab, steps 'click Holdings' (the holdings view). Must match: the "
  "layout, the text, the colours. May differ: the numbers (live data).")

design_set("reference", "login_mockup",
  "docs/mockups/login.png, a PNG export of the Figma frame 'Login'. Viewport 390x844 (phone). "
  "Cases: login (the login view, /login).")
```

**Behavioural (a command, API docs, an old program).** There's nothing to
screenshot; a case is one input, sent to both sides (or checked against the
documented result), and the check is a real command whose exit code is the
answer:

```
design_set("reference", "api_docs",
  "docs/api.md, the API to implement. One case per documented example: get_user, get_user_404, "
  "create_user, create_user_invalid, list_users_paged; each against the endpoint's component. "
  "Must match: the status and the response's fields. May differ: ids and timestamps.")

design_set("reference", "old_cli",
  "legacy/report.sh, the program being replaced. One case per file in samples/ (small, empty, "
  "unicode, huge); all against the report component. Must match: the output, byte for byte.")
```

The check script behind a behavioural `compare_one` (`scripts/compare_bc.py`,
`tests/test_docs_examples.py`, `scripts/compare_old_cli.py`) is a file, so it's
planned like any other work: a node under the `project` component, with the
reference in its `references`, built before the leaves that use it (their
`depends_on`). Following the existing runbook rule, *running* a comparison
is never a node.

### 3. Every task's check: `compare_one {case}`

`test_one` already gives every leaf a check of its own: the Architect writes
`test_one` with a `{test_id}` placeholder, each Dev passes its test's id, and
`mark_leaf_done` runs it and refuses the leaf if it fails. The comparison
works the same way:

```
runbook_set("compare_one", "python3 scripts/compare_bc.py {case}",
  notes="One case from reference:bc, e.g. case=divide. The cases: add, subtract, multiply, "
        "divide, power, divide_by_zero, bad_input, repl_loop, exit.")
runbook_set("compare_all", "python3 scripts/compare_bc.py --all", notes="Every case; the reviewer runs it.")
```

or, for a page:

```
runbook_set("compare_one",
  "compare_screens old=file://{project}/portfolio-dashboard.html new={view}/ viewport=1280x800 case={case}",
  notes="One state of reference:dashboard_page. The cases (each with its steps): header, summary_cards, "
        "watchlist_tab ('click Watchlist'), holdings_tab ('click Holdings').")
```

How a case reaches the task that builds it:

1. **Architect:** lists the cases in the reference entry, each against the
   component that builds it (`divide`, `divide_by_zero` -> the evaluator;
   `bad_input`, `exit` -> the REPL). `finish` refuses while a case has no
   component.
2. **Lead / Task:** each leaf that builds a case gets it in a new `cases`
   field (a `Leaf` column, so `_ensure_columns` in `models/db.py` too), next
   to its `done_when`. A case can't be dropped: when Task splits a node, each
   of its cases goes to one child, the same way the node tools already check
   files and `depends_on`.
3. **Dev:** `mark_leaf_done` runs the unit test as today, then `compare_one`
   for each of the leaf's cases: a behavioural one as a command (its exit
   code), a visual one by calling `compare_screens` in-process, since it's a
   JFI tool rather than a program. A failing case refuses the leaf with the
   comparison's output (and, for a page, the two screenshots attached), so
   Dev fixes it in the same episode. A case another leaf hasn't built yet
   (the watchlist tab before its view exists) is re-queued, the way a test
   failing on another function's `NotImplementedError` is today.
4. **Reviewer:** `compare_all` after `e2e`. A failing case maps straight to
   the leaf that owns it, which it reopens.

So each task is checked against the reference when it's implemented, and the
reviewer's pass checks that nothing later broke it.

### The `compare_screens` tool

A new tool for the visual form. It reuses `check_page`'s headless browser
(Playwright) and `view_image`'s attachment:

1. Render the **old** side: open the URL or file at the viewport and replay
   `steps`, or load the image as it is.
2. Render the **new** side the same way. `{view}` and `{project}` come from
   the runbook, so the comparison follows the app if its port changes.
3. Save `old.png`, `new.png` and a pixel `diff.png` under
   `.jfi/screens/compare/<key>/`.
4. Return the pixel-difference share, a diff of the visible text (when both
   sides are pages), any console errors on the new side, and **both
   screenshots attached** for the model to look at.

The tool doesn't decide pass or fail. Its numbers are evidence; the model
judges the attached pair against the reference's "must match / may differ"
(see decision 3).

## Who does what

| Role | What changes |
|---|---|
| **Architect** | A new prompt step: find the references (files the goal names, files in the repo it's converting, URLs), `design_set("reference", ...)` each one with its cases, each case against a component, and `runbook_set` `compare_one` (with `{case}`) and `compare_all`. When the goal names a reference, `finish` refuses until both entries exist and every case has a component, the same way it refuses without `test_one` today. |
| **Lead / Task** | Hand each case down to the leaf that builds it (the leaf's `cases`), and cite `reference:<key>` and the original's line range in its `references`. A split keeps every case. |
| **Dev** | `mark_leaf_done` runs the leaf's unit test, then `compare_one` for each of its cases, and refuses the leaf on a mismatch. |
| **Reviewer** | Runs `compare_all` after `e2e`. A mismatch is a bug: it reopens the leaf that owns that case, and the fix note names the case and the screenshot paths. A case that can't run here (no browser, no Figma export) goes into the review report as *not checked*, never as passed. |
| **Dashboard** (later) | `jfi-web` shows each comparison's old / new / diff images. |

## Kinds of reference

| Reference (old) | New | Compared by | Form |
|---|---|---|---|
| Static HTML page | Web app (Vite, React, ...) | Screenshots at one viewport and state, plus a visible-text diff | `compare_screens` |
| Image / mockup | Web app | New screenshot vs the image | `compare_screens` |
| Figma design | Web app | A PNG export of each frame, then as an image (decision 5) | `compare_screens` |
| API docs (Markdown, OpenAPI) | Python (or any) service | The docs' examples sent to the running service; status and fields compared | command |
| Wireframe photo | Web app | New screenshot vs the photo, layout only (the "may differ" says so) | `compare_screens` |
| Live website (URL) | Rebuilt site | Each page's URL vs the same route on the new site | `compare_screens` |
| Mobile mockups / breakpoints | Responsive web app | One case per viewport | `compare_screens` |
| Chart image (Excel, matplotlib) | Dashboard chart | Screenshot of the chart's element vs the image | `compare_screens` |
| PDF layout | Generated PDF or page | Both PDFs rendered to PNG per page, then compared | `compare_screens` |
| HTML email template | New email template | Both rendered at email-client width | `compare_screens` |
| Storybook / design-system screenshots | New components | Each component's page vs its screenshot | `compare_screens` |
| Screen recording / GIF | Interaction in the app | `extract_video_frames` on the recording; the same steps replayed and screenshotted | `compare_screens` with `steps` |
| GraphQL schema, `.proto`, JSON Schema | Service or file output | Responses or files validated against the schema | command |
| HAR file / request logs | New service | Recorded requests replayed; status and body shape compared | command |
| Old implementation / sample code | New implementation | Both run on the same inputs; outputs diffed | command |
| Old test suite | New implementation | The old tests run against the new code | command |
| CLI `--help`, man page, terminal transcripts | New CLI | Each recorded command re-run; output compared | command |
| Golden input/output files | Data pipeline | The sample input run through; output diffed with the expected file | command |
| Spreadsheet formulas | Code | The same inputs through both; numbers compared | command |
| Database schema / dump | ORM models, migrations | Both schemas dumped and diffed | command |
| Build config (webpack, setup.py) | Migrated build | Both builds' output listed and diffed | command |
| Screenshot of an old UI | Terminal UI (prompt_toolkit) | Out of scope for now: no headless TUI capture | — |

## The benchmark tasks, each with a reference

Every task in `benchmark/tasks/` has something the goal could name as its
reference, the way you'd say "build a calculator, and compare it with `bc`".
This doubles as the test bed for the feature (see Build notes).

### Worked example: `terminal/calc` against `bc`

Goal: *"Build a command-line calculator ... compare it with the `bc`
command."* The Architect would record the reference with its cases, each
against the component that builds it:

```
design_set("reference", "bc",
  "The bc command (bc -l), the reference calculator. Cases: add, subtract, multiply, divide, "
  "power (the evaluator); divide_by_zero, bad_input (the evaluator's errors); repl_loop, exit (the "
  "REPL in main.py). Must match: each result to 10 decimal places; an error is one line, no "
  "traceback. May differ: integer formatting (4 vs 4.0), trailing zeros (bc -l prints "
  "3.50000000000000000000), error wording, and ^ with a fractional exponent, which bc truncates "
  "to an integer with only a warning (2 ^ 0.5 prints 1).")

runbook_set("compare_one", "python3 scripts/compare_bc.py {case}",
  notes="One case from reference:bc, e.g. case=divide. Its expressions are in "
        "samples/cases/<case>.txt; each goes to `python3 main.py` and to `bc -l` (scale=10).")
runbook_set("compare_all", "python3 scripts/compare_bc.py --all")
```

plus a node under `project` for `scripts/compare_bc.py` and `samples/cases/`.
Then, down the plan:

| Leaf (built by Dev) | Unit test (`test_one`) | Its cases (`compare_one`) |
|---|---|---|
| `evaluate()`: + - * / | `test_evaluate` | `add`, `subtract`, `multiply`, `divide` |
| `evaluate()`: ^ | `test_power` | `power` |
| `evaluate()` errors | `test_errors` | `divide_by_zero`, `bad_input` |
| `main.py` REPL loop | `test_repl` | `repl_loop`, `exit` |

`mark_leaf_done` on the `^` leaf runs `test_power`, then `compare_one power`.
If `2 ^ 10` gives `1024.0001`, that leaf is refused there and then, not
found by the reviewer three leaves later.

The "may differ" text is the part that matters: `bc` has no fractional `^`
(it truncates the exponent, with only a warning), prints `4` where Python
prints `4.0` and `3.50000000000000000000` where Python prints `3.5`, and words
its errors differently. Without saying so, the cases fail on differences the
goal doesn't care about. Checked with GNU `bc`: `2 ^ 0.5` prints a "non-zero
scale in exponent" warning and then `1`, and `1 / 0` is a "Divide by zero"
runtime error.

### Worked example: input and expected output CSVs

Goal: *"Write a Python script that turns `data/orders.csv` into a monthly
summary. `data/expected_summary.csv` is what it should produce."* There's no
old program and nothing to look at, only a golden pair. After the code is
implemented, running it on the input must give the expected file. The
Architect would:

1. **Read both files' headers and a few rows** (`read_file` on each). That
   shows the columns, the formats, and which output columns come from which
   input columns: `month` from `order_date`, `revenue` = Σ `qty` × `price`.
2. **Probe the comparison rules from the expected file:** rows sorted by
   `month`; `revenue` with 2 decimals; no blank cells. Those become "must
   match". Column order, the line ending and a trailing newline go under "may
   differ" unless the goal says otherwise.
3. **Make the cases** small enough to land on single tasks. The whole file is
   one case (`full`), plus one per output column, so a leaf that builds one
   column is checked on that column alone. With more than one pair (`data/`
   holds several inputs, each with its expected file), each pair is a case.

```
design_set("reference", "expected_summary",
  "data/orders.csv -> data/expected_summary.csv, the golden pair. Cases: month (the date grouping), "
  "orders (the count), revenue (the sum), full (the whole file; the writer and main.py). Must match: "
  "the rows and their order (sorted by month), each value, revenue to 0.01. May differ: column order, "
  "line endings, a trailing newline.")

runbook_set("compare_one", "python3 scripts/compare_csv.py {case}",
  notes="One case from reference:expected_summary, e.g. case=revenue. Runs `python3 summary.py "
        "data/orders.csv .jfi/compare/out.csv`, then compares that column (or, for full, the whole file) "
        "with data/expected_summary.csv by column name, and prints the first differing row.")
runbook_set("compare_all", "python3 scripts/compare_csv.py --all")
```

| Leaf | Its cases (`compare_one`), run by `mark_leaf_done` after its unit test |
|---|---|
| group orders by month | `month` |
| count orders per month | `orders` |
| sum revenue per month | `revenue` |
| write the CSV, `main()` | `full` |

When `sum revenue` rounds too early and gives `1204.49` where the expected
file has `1204.50`, `compare_one revenue` refuses that leaf with the row and
both values, while Dev is still on it. A column whose leaf isn't built yet
(the writer, for `full`) re-queues, as above.

### Every task

| Task | Reference the goal could name | Compared by | Form |
|---|---|---|---|
| `terminal/calc` | `bc -l` | The same expressions through both; results compared to 10 places | command |
| `data_engineering/log_pipeline` | `grep` / `awk` / `sort \| uniq -c` over the same log | Each count (`total_lines`, errors by service, ...) computed both ways | command |
| `data_engineering/sales_summary` | `sqlite3`: the CSV imported, then `SUM(quantity*unit_price) ... GROUP BY region` | Every summary value compared with the SQL result | command |
| `polyglot/*` (Python) | The `_js` twin, or the other way round | The same inputs through both modules; outputs compared | command |
| `polyglot/collatz_conjecture` | OEIS A006577 (step counts for n = 1, 2, 3, ...) | `steps(n)` for the first 1,000 n vs the published list | command |
| `polyglot/difference_of_squares` | The closed forms, (n(n+1)/2)² and n(n+1)(2n+1)/6, in `bc` | Both functions for n = 1..1,000 vs the formulas | command |
| `polyglot/run_length_encoding` | A one-line `itertools.groupby` encoder | Random letter strings through both; `decode(encode(x)) == x` too | command |
| `polyglot/spiral_matrix` | The spiral printed in the goal (e.g. n = 4), or the `_js` twin | The printed matrices compared exactly | command |
| `html/pricing_table`, `recipe_card`, `faq_accordion` | A mockup PNG shipped with the task (or a real pricing / recipe / FAQ page's screenshot) | Screenshots at 1280 px wide; the FAQ also with one item opened (`steps='click <question>'`) | `compare_screens` |
| `webapp/react_counter` | A plain-JS counter page, or the React docs' counter | Screenshots before and after 3 clicks on both | `compare_screens` with `steps` |
| `webapp/nextjs_static` | A two-page static HTML version of the site | `out/index.html` and `out/about.html` vs the two pages; the nav link followed on both | `compare_screens` with `steps` |
| `webapp/music_player` | `music/library.json` and the WAV files themselves | Every title, artist and duration shown vs the JSON; each duration vs the WAV's real length (`python -m wave` / `soxi`) | command (plus `compare_screens` against a mockup) |
| `story/*` | A sample passage in the wanted format and tone (e.g. TODO item 10's `NARATOR:` / `CHAR_1:` script format) | Structure checked (headings, the format's parts in order); the tone read by the reviewer | command plus the reviewer's read |

## Decisions to review

1. **Where it lives.** The reference as a design entry (`kind="reference"`)
   with its cases, plus the runbook's `compare_one` / `compare_all`, as
   above. The alternative is runbook entries only, with the reference
   described in their notes. *Proposal:*
   both. The design entry is what Lead, Task and Dev cite in `references`;
   the runbook entry is what gets run.
2. **When it's required.** *Proposal:* the Architect's `finish` requires
   `compare_one` only when the goal mentions a reference: a file that exists
   in the project with a `.html`, `.png`, `.jpg`, `.svg`, `.pdf`,
   `.yaml`/`.json` (OpenAPI) or `.md` docs extension; an input/expected pair
   (`.csv`, `.json`, `.txt` files whose names say input/expected/output); a
   command it names that's on PATH (`bc`); or a Figma URL. That's a
   mechanical check, like `entry`, so a prompt rule alone can't skip it.
3. **Pass/fail for screenshots.** A pixel threshold is brittle: fonts,
   anti-aliasing and live data all differ. *Proposal:* the model judges the
   attached pair against the "must match / may differ" text; the pixel share
   and text diff are evidence, and a large text diff (missing headings,
   cards) is flagged as likely missing work.
4. **States beyond the first screen.** Tabs, dialogs, scrolled sections.
   *Proposal:* `steps` as a short list of `browser`-style actions (`click
   <text>`, `type <field> <text>`, `scroll`), replayed on both sides. One
   case per state.
5. **Figma.** A Figma URL needs an API token to export frames. *Proposal:*
   not at first. The Architect asks for (or the goal provides) PNG exports
   under the project, and records the frame names in the reference entry. A
   `FIGMA_TOKEN` export can come later.
6. **Checked at every task, not only in review.** That's the objective:
   `mark_leaf_done` runs the leaf's cases and refuses a mismatch. The cost is
   one comparison per leaf (a screenshot pair takes a few seconds; a
   behavioural case is one command). *Proposal:* yes, for every leaf with
   cases; a leaf with none (shared plumbing) is gated by its unit test alone.
7. **Behavioural checks are plan nodes.** The docs-examples test or the
   old-vs-new script is code, so it's a node like any other. *Proposal:*
   yes, under the `project` component, owned by one leaf, built before the
   review.
8. **Asking the user.** No model-facing tool asks the user anything today:
   the console asks for the goal at the start, and `!` interjections reach
   whichever episode reads them first. *Proposal:* a narrow `ask_user`
   (Architect only, at most one question per run, with the options it found)
   for step 7's cases. Without an answer (or with `AUTO_APPROVE_COMMANDS`,
   which means nobody is watching), it goes ahead on its best guess and
   records that as an assumption.
9. **The Architect's probes.** Step 3 needs `execute_command` and a way to
   look at a page, which the Architect doesn't have (it "writes nothing to
   disk"). *Proposal:* give it `execute_command` for read-only probes, plus
   `compare_screens` / `check_page`, and save what the probes produce (the
   reference screenshots, the old program's outputs) under
   `.jfi/screens/compare/` and `.jfi/compare/`, never in the project.

## Build notes (once agreed)

- `design_tools.KINDS` gains `reference`.
- `runbook_tools`: `compare_one` and `compare_all` join `COMMAND_ENTRIES`.
  Their command starts with a program (a script) or with `compare_screens`,
  which `_starts_with_a_program` would have to accept. `{view}` and `{project}` are new placeholders that
  `compare_screens` fills from the runbook's `view` entry and the project root;
  nothing expands one runbook entry into another today.
- `tool/compare_tools.py`: `compare_screens`, built from `check_page`'s
  Playwright code and `view_image`'s attachment; images go in
  `.jfi/screens/compare/<key>/`. It joins the reviewer's core set and Dev's
  optional pool (`episode/roles.py`).
- `planner/prompts.py`: an Architect step for references, their cases and
  `compare_one` / `compare_all`; Lead/Task hand cases down and cite
  `reference:<key>`. `planner/loop.py`: the `finish` checks (both entries,
  `{case}` in `compare_one`, every case on a component). `planner/nodes.py`:
  a split keeps every case.
- `models/leaf.py`: a `cases` column (and `_ensure_columns`). `imp/dev.py`:
  `mark_leaf_done` runs `compare_one` per case after the unit test.
- `review/prompts.py`: run `compare_all` after `e2e`.
- Tests: the stui-shaped case (an HTML file in the project and a goal that
  names it) refuses `finish` without `compare_one`, or with a case on no
  component; a leaf whose case fails is refused by `mark_leaf_done`, and one
  whose case passes is accepted; `compare_screens` on two local `file://`
  pages returns both images and a diff; the reviewer prompt names the step.
- Docs: `phase-planner.md`, `phase-reviewer.md`, `plan-tree.md` (the
  `reference:` citations).
- Benchmarks: a variant of each task whose prompt names its reference from the
  table above (calc first: "compare it with `bc`"; `sales_summary` with an
  input and expected-output CSV). The harness then checks that the run's
  runbook has `compare_one`, that every leaf's cases ran at its
  `mark_leaf_done`, and that the reviewer ran `compare_all`, as well as the
  task's own `verify`.
