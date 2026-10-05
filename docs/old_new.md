# Old vs new: checking the build against the reference it came from

**Status:** proposal, for review. Nothing here is built yet.

## The problem

Many goals arrive with a **reference**: the thing the new code has to match.

- An existing HTML page or prototype to turn into an app (the stui runs: a
  static `portfolio-dashboard.html` rebuilt as a Vite app).
- A mockup: an image, a screenshot, a Figma design.
- API docs (Markdown, OpenAPI) that a Python service has to implement.
- Sample code: an old implementation, in this language or another, that the
  new one replaces.

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

## What we'd add

When the goal has a reference, the **Architect records two things**:

1. **The reference:** what it is and where it lives. This is a design entry,
   so every role can pull it.
2. **How to compare it with the new build:** one runbook entry per
   comparison, the same way `test` and `e2e` say how to test it.

Later roles then use those entries:

- **Dev** checks its own part of the build against the reference.
- **The reviewer** runs every comparison before it passes the project.

### 1. The reference: `design_set("reference", <key>, ...)`

A new design kind, `reference`, next to `stack`, `component`, `contract`, ...
(`design_tools.KINDS`). Each entry records:

- **where** it is: a path in the project, or a URL;
- **what** it is (an HTML page, a PNG mockup, OpenAPI docs, an old CLI);
- **which components** it's the reference for;
- **what must match** (the layout, the text, the colours, the response
  shape, the output) and **what may differ** (live data, timestamps, fonts the
  new stack doesn't ship).

```
design_set("reference", "dashboard_page",
  "portfolio-dashboard.html (project root): the original static page. Reference for the shell, "
  "watchlist and holdings views. Must match: the header, the 4 summary cards in one row, the "
  "watchlist table's columns and order, the colours. May differ: the numbers (live data).")
```

Nodes point at it in `references`, the way they already cite contracts
(`reference:dashboard_page`, plus the source lines, e.g.
`portfolio-dashboard.html L120-188`). That's how a view's Dev knows what it's
rebuilding without reading the whole original.

### 2. How to compare: runbook `compare_<key>` entries

One runbook entry per comparison. The command says how to get the **new**
side; the notes say what to compare it with and how. There are two forms,
depending on the reference.

**Visual (a page, an image, a design export).** The comparison is a pair of
screenshots taken the same way:

```
runbook_set("compare_dashboard",
  "compare_screens old=file://{project}/portfolio-dashboard.html new={view}/ viewport=1280x800",
  notes="reference:dashboard_page. Same viewport, top of the page, no interaction.")

runbook_set("compare_watchlist_tab",
  "compare_screens old=file://{project}/portfolio-dashboard.html new={view}/ viewport=1280x800 "
  "steps='click Watchlist'",
  notes="reference:dashboard_page. The watchlist tab, after one click on both sides.")

runbook_set("compare_login_mockup",
  "compare_screens old=docs/mockups/login.png new={view}/login viewport=390x844",
  notes="reference:login_mockup (a PNG export of the Figma frame). Phone width.")
```

**Behavioural (API docs, sample code).** There's nothing to screenshot; the
comparison is the observable output, so it's a real command whose exit code
is the check:

```
runbook_set("compare_api_docs", "uv run pytest tests/test_docs_examples.py",
  notes="reference:api_docs. Every request example in docs/api.md is sent to the running service; "
        "the status and the response's fields must match the documented example.")

runbook_set("compare_old_cli", "uv run python .jfi/compare/old_vs_new.py",
  notes="reference:old_cli. Runs legacy/report.sh and the new `report` command on the same "
        "samples/ inputs and diffs their output.")
```

The check script (`test_docs_examples.py`, `old_vs_new.py`) is a file, so it's
planned like any other work: a node under the `project` component, with the
reference in its `references`. Following the existing runbook rule,
*running* the comparison is never a node.

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
| **Architect** | A new prompt step: find the references (files the goal names, files in the repo it's converting, URLs), `design_set("reference", ...)` each one, and `runbook_set("compare_<key>", ...)` how to check it. When the goal names a reference, `finish` refuses until it has at least one `compare_` entry, the same way it refuses without `e2e` today. |
| **Lead / Task** | Cite `reference:<key>` and the original's line range in the `references` of every node that rebuilds part of it. |
| **Dev** | For a leaf whose node cites a reference: after its test passes, run the matching `compare_` entry (`compare_screens` is in the optional pool) and fix what doesn't match its part. |
| **Reviewer** | Runs every `compare_` entry after `e2e`. A mismatch is a bug: it reopens the leaf that owns that part, and the fix note names the comparison and the screenshot paths. A comparison that can't run here (no browser, no Figma export) goes into the review report as *not checked*, never as passed. |
| **Dashboard** (later) | `jfi-web` shows each comparison's old / new / diff images. |

## Kinds of reference

| Reference (old) | New | Compared by | Form |
|---|---|---|---|
| Static HTML page | Web app (Vite, React, ...) | Screenshots at one viewport and state, plus a visible-text diff | `compare_screens` |
| Image / mockup | Web app | New screenshot vs the image | `compare_screens` |
| Figma design | Web app | A PNG export of each frame, then as an image (decision 5) | `compare_screens` |
| API docs (Markdown, OpenAPI) | Python (or any) service | The docs' examples sent to the running service; status and fields compared | command |
| Old implementation / sample code | New implementation | Both run on the same inputs; outputs diffed | command |
| Screenshot of an old UI | Terminal UI (prompt_toolkit) | Out of scope for now: no headless TUI capture | — |

## Decisions to review

1. **Where it lives.** The reference as a design entry (`kind="reference"`)
   plus `compare_<key>` runbook entries, as above. The alternative is runbook
   entries only, with the reference described in their notes. *Proposal:*
   both. The design entry is what Lead, Task and Dev cite in `references`;
   the runbook entry is what gets run.
2. **When it's required.** *Proposal:* the Architect's `finish` requires a
   `compare_` entry only when the goal mentions a reference: a file with a
   `.html`, `.png`, `.jpg`, `.svg`, `.pdf`, `.yaml`/`.json` (OpenAPI) or
   `.md` docs extension that exists in the project, or a Figma URL. That's a
   mechanical check, like `entry`, so a prompt rule alone can't skip it.
3. **Pass/fail for screenshots.** A pixel threshold is brittle: fonts,
   anti-aliasing and live data all differ. *Proposal:* the model judges the
   attached pair against the "must match / may differ" text; the pixel share
   and text diff are evidence, and a large text diff (missing headings,
   cards) is flagged as likely missing work.
4. **States beyond the first screen.** Tabs, dialogs, scrolled sections.
   *Proposal:* `steps` as a short list of `browser`-style actions (`click
   <text>`, `type <field> <text>`, `scroll`), replayed on both sides. One
   `compare_` entry per state.
5. **Figma.** A Figma URL needs an API token to export frames. *Proposal:*
   not at first. The Architect asks for (or the goal provides) PNG exports
   under the project, and records the frame names in the reference entry. A
   `FIGMA_TOKEN` export can come later.
6. **Dev's share.** Run the comparison for every UI leaf (slower, catches
   drift early) or only in review (cheaper). *Proposal:* optional for Dev, on
   leaves whose node cites a reference; always in review.
7. **Behavioural checks are plan nodes.** The docs-examples test or the
   old-vs-new script is code, so it's a node like any other. *Proposal:*
   yes, under the `project` component, owned by one leaf, built before the
   review.

## Build notes (once agreed)

- `design_tools.KINDS` gains `reference`.
- `runbook_tools`: `compare_*` names are allowed. Their command starts with
  a program (a script) or with `compare_screens`, which `_starts_with_a_program`
  would have to accept. `{view}` and `{project}` are new placeholders that
  `compare_screens` fills from the runbook's `view` entry and the project root;
  nothing expands one runbook entry into another today.
- `tool/compare_tools.py`: `compare_screens`, built from `check_page`'s
  Playwright code and `view_image`'s attachment; images go in
  `.jfi/screens/compare/<key>/`. It joins the reviewer's core set and Dev's
  optional pool (`episode/roles.py`).
- `planner/prompts.py`: an Architect step for references and their
  comparisons; Lead/Task cite `reference:<key>`. `planner/loop.py`: the
  `finish` check from decision 2.
- `review/prompts.py`: run every `compare_` entry after `e2e`.
- Tests: the stui-shaped case (an HTML file in the project and a goal that
  names it) refuses `finish` without a `compare_` entry; `compare_screens` on
  two local `file://` pages returns both images and a diff; the reviewer
  prompt names the step.
- Docs: `phase-planner.md`, `phase-reviewer.md`, `plan-tree.md` (the
  `reference:` citations).
