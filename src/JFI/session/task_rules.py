"""
Task-type detection and per-type planning rules, used by
AdaptiveSessionManager instead of one generic rule block for every goal.

Two real problems this exists to fix, both observed in practice:

1. Context cost. JFI is already token-hungry by design (the whole session
   history is re-sent every turn) -- paying for javascript-componentization
   guidance on a pure-python task, or python's dispatch-table guidance on a
   short story, is waste a smaller model's context window can't afford. The
   generic PLAN_FORMAT_RULES block has to stay broad enough to say something
   useful about every goal shape at once; a task-type-specific block only
   has to be useful for one.

2. Missing architecture guidance. The generic rules are about the plan
   FILE's mechanical shape (headers, checkboxes, numbering) -- they say
   nothing about code architecture. A javascript goal can satisfy every
   mechanical rule while still producing one monolithic main.js with no
   components (observed on a real run: a portfolio dashboard ported to
   Vite came out as a single src/main.js handling every section, with
   nothing split into reusable pieces). CORE_PLAN_RULES stays purely
   mechanical; the architecture opinion lives in each type's own addendum
   instead, where it can actually be specific.

detect_task_type() is a cheap, deterministic keyword read of the goal
text -- no extra LLM call, no extra context cost, and (unlike an
LLM-judged classification) always gives the same answer for the same goal.
An unmatched goal returns "generic", and callers are expected to fall back
to the existing, already-validated PLAN_FORMAT_RULES unchanged -- new
behavior only kicks in where there's real signal to act on.

Current type roster (see TYPE_ADDENDA): python, javascript, html_css,
story, data_engineering, go, sql. Adding a new one means: a hint regex with low
false-positive risk, an entry in _TYPE_CHECKS (ordered narrower-domain
first when it could collide with an existing type -- see the comment
above _TYPE_CHECKS), an *_ADDENDUM constant with a worked example, and a
TYPE_ADDENDA entry. Each addendum should earn its place with a real
decomposition failure mode specific to that domain -- not just restate
CORE_PLAN_RULES in different words.
"""
import re

_PYTHON_HINTS = re.compile(
    r"\bpython\b|\.py\b|\bdjango\b|\bflask\b|\bfastapi\b|\bpytest\b",
    re.I,
)
_JAVASCRIPT_HINTS = re.compile(
    r"\bjavascript\b|\btypescript\b|\bnode(?:\.js)?\b|\bnpm\b|\breact\b|\bvue\b|"
    r"\bnext\.js\b|\bvite\b|\.jsx?\b|\.tsx?\b|\bfrontend\b|"
    r"\bweb ?app\b|\bwebpage\b|\bwebsite\b|\bdom\b",
    re.I,
)
# html/css moved out of _JAVASCRIPT_HINTS into their own type: a STATIC
# page (no JS framework/tooling, no build step) has a genuinely different
# decomposition pattern (structural markup, no components/state) from a
# real JS app -- see HTML_CSS_ADDENDUM. Checked AFTER javascript so a goal
# that also names a framework/build tool ("Vite" + "HTML and CSS") still
# resolves to javascript, matching this project's own webapp/html
# benchmark-tier split (a plain static page vs. real framework tooling).
_HTML_CSS_HINTS = re.compile(
    r"\bhtml\b|\bcss\b|\bsemantic markup\b|\baccessib(?:le|ility)\b|"
    r"\bresponsive design\b|\bstatic (?:page|site)\b",
    re.I,
)
_STORY_HINTS = re.compile(
    r"\bshort story\b|\bstory\b|\bnarrative\b|\bnovel\b|\bfiction\b|\bprose\b|"
    r"\bcharacter card\b|\bchapter\b",
    re.I,
)
_SQL_HINTS = re.compile(
    r"\bsql\b|\bpostgres(?:ql)?\b|\bmysql\b|\bsqlite\b|\bschema migration\b|"
    r"\bdatabase schema\b|\b(?:create|alter) table\b",
    re.I,
)
# Deliberately NOT "csv" alone -- a plain CSV mention is common in
# unrelated tasks (the StockUI data-service work fetched CSVs from a
# javascript frontend, not a data-engineering goal). These are specific
# enough to the pandas/ETL domain that a false positive is unlikely.
_DATA_ENGINEERING_HINTS = re.compile(
    r"\bpandas\b|\bdataframe\b|\bnumpy\b|\betl\b|\bdata[- ]pipeline\b|"
    r"\bdata engineering\b|\bparquet\b",
    re.I,
)
_GO_HINTS = re.compile(
    r"\bgolang\b|\.go\b|\bgo\.mod\b|\bgoroutine|\bgo build\b|\bgo test\b|\bgo vet\b",
    re.I,
)

# Checked in this order because a goal can legitimately mention more than
# one hint (e.g. "a Flask backend for a React frontend" matches both
# python and javascript) -- the more visual-surface type usually carries
# the bigger architecture risk (a wall of unstructured markup/JS is a much
# easier failure mode than an unstructured Flask route), so javascript
# wins that particular tie. story is checked first since a creative-
# writing goal that happens to mention "python" as a character's name or
# similar is far less likely than the reverse. html_css is checked right
# after javascript (narrower: wins only when no framework/tooling hint
# already matched javascript first). data_engineering, go, and sql are
# checked before the generic python check since they're all
# python-adjacent-or-narrower domains with their own decomposition pattern
# (data_engineering literally runs on python, so it must win the tie).
_TYPE_CHECKS = (
    ("story", _STORY_HINTS),
    ("javascript", _JAVASCRIPT_HINTS),
    ("html_css", _HTML_CSS_HINTS),
    ("data_engineering", _DATA_ENGINEERING_HINTS),
    ("go", _GO_HINTS),
    ("sql", _SQL_HINTS),
    ("python", _PYTHON_HINTS),
)


def detect_task_type(goal_text: str) -> str:
    """Returns "python", "javascript", "html_css", "story",
    "data_engineering", "go", "sql", or "generic" (no confident match --
    callers fall back to the existing generic rules)."""
    for task_type, pattern in _TYPE_CHECKS:
        if pattern.search(goal_text or ""):
            return task_type
    return "generic"


CORE_PLAN_RULES = """
    PLAN FILE FORMAT (mandatory, no exceptions):
    - The plan file is exactly: {plan_path}
    - Exactly two "##" section headers are mechanically required, spelled
      exactly like this and nothing else: "## Implementation" and
      "## Testing" -- JFI's own tooling scans for a header starting with
      "Implementation"/"Testing" to build each phase's own work queue and
      progress bar. However many logical groups your own project needs,
      they ALL nest as plain bullets under those two headers -- never
      their own "##"/"###" headers (any markdown header, any level,
      anywhere in the file silently ends the section right there,
      dropping everything below it from the work queue even though it's
      still in the file). Architecture notes, data models, or other
      context go under "## Context and Prerequisites" instead.
    - {plan_path}'s own directory is internal bookkeeping ONLY -- never
      put deliverables (source, tests, docs) inside it just because the
      plan happens to live there. A project-scaffolding command that needs
      an empty target directory (`npm create vite`, `django-admin
      startproject`, ...) will see this folder already sitting in the
      working directory and refuse to run here -- expected, not a real
      error: scaffold into a throwaway subdirectory instead, then move
      everything generated up into the working directory root (`mv
      temp-app/* . ...; rmdir temp-app` or equivalent), leaving this
      folder untouched.
    - The plan is a TREE, not a flat list: break every task into the
      smallest doable pieces, recursing as many levels as it takes (2, 3,
      4+) -- stop nesting a branch only once its leaves are each small
      enough to finish and verify in one focused step. Only LEAF items
      (not broken down further) get a checkbox:
          - [ ] 1.1.1 Short description of the smallest step
      Parents are plain bullets, NO checkbox: `- 1.1 Description`. A
      checkbox on a parent hands the implementer a fake duplicate task
      alongside its own real children.
    - Numbering shows a leaf's full path from its section root (1.1, then
      1.1.1, then 1.1.1.1, ...) -- see the worked example below for this
      kind of project's own typical shape.
    - "- [ ]" = not started, "- [x]" = done, "- [○]" = user-skipped
      (Ctrl+K -- never write this yourself; treat it exactly like done,
      never redo or flag it). No other marker, ever -- no ballot boxes, no
      emoji ticks, no checkbox tables. Tick a box by changing ONLY the
      space inside the brackets to an x -- byte-identical otherwise, so a
      targeted replace can find it.
    - A task with only one obvious, already-small action underneath it can
      stay a single leaf -- don't split for the sake of splitting. The
      goal is the smallest task that is still genuinely one task, not
      maximum depth.
"""

PYTHON_ADDENDUM = """
    PYTHON-SPECIFIC DECOMPOSITION:
    - Group near-identical operations behind ONE mechanism instead of one
      leaf each: calculator operators, CRUD verbs, format converters, and
      similar small-variant operations belong in a single dispatch
      dict/registry plus one generic function that looks an entry up --
      that's one or two leaves (build the table, build the dispatcher),
      not one leaf per operation. Splitting those apart is busywork, not
      finer decomposition.
    - Still split by REAL seams within a module: parsing/validation, the
      core computation, error handling, and the I/O/entry-point layer (a
      REPL, a CLI, a route handler) are usually separate leaves even
      inside one file -- each is independently testable and independently
      breakable.
    - Package layout: a script stays one file; anything with more than one
      real concern gets a package (`__init__.py` plus one module per
      concern), one leaf per module -- not one leaf that writes every file
      in the package at once.
    - Tests live under `tests/`, one leaf per logical group of cases
      (the happy path, each distinct error type, any edge case the brief
      names explicitly) -- not one leaf that writes the entire test file
      blind, and not one leaf per single assert either.
    - Worked example, "a calculator with + - * / ^":
          - 1. Core arithmetic
            - [ ] 1.1 Build the OPERATORS dispatch table
            - [ ] 1.2 Add evaluate() that parses input and looks the op up
            - [ ] 1.3 Add the division-by-zero / parse-error contract
          - [ ] 2. REPL entry point (small enough as one leaf)
"""

JAVASCRIPT_ADDENDUM = """
    JAVASCRIPT/FRONTEND-SPECIFIC DECOMPOSITION:
    - This applies just as much to PORTING/MIGRATING an existing script as
      to writing new code -- a goal that says "extract the inline <script>
      block into src/main.js" or "migrate this file to Vite" is telling
      you WHERE the behavior ends up and that it must keep working
      identically, not that the extraction has to land in one leaf or one
      file. If the source script (even a single monolithic inline
      <script> block being migrated out of a static HTML file) itself
      handles more than one independent concern, splitting it into
      multiple leaves/files during the port is still required -- "preserve
      the same behavior" is a constraint on OUTPUT behavior, never a
      license to keep the internal code organization monolithic. A single
      leaf that says "write the extracted JavaScript to src/main.js" is
      not a leaf yet if that script does more than one thing: split it
      into one extraction+placement leaf per concern instead (e.g. "move
      the sidebar-rendering code into src/components/Sidebar.js", "move
      the chart code into src/components/Chart.js", ...), then a final
      leaf that wires them together from a slim main.js/entry point.
    - A single monolithic file handling every concern (rendering every
      section, owning all state, wiring every event listener) is itself a
      sign of under-decomposition once the UI has more than ~2-3
      independent regions (a sidebar, a chart, a table, a modal, a nav) --
      the same way one leaf naming five deliverables is not a leaf yet.
      Each independent region/concern is its OWN leaf, even in a plain
      vanilla-JS project with no framework and no bundler for real
      components: a named function per region (`renderSidebar()`,
      `renderChart()`, `wireThemeToggle()`, ...), each its own leaf --
      never one leaf that writes the entire script.
    - Prefer one file/module per component over one script that does
      everything, once the app is more than a trivial single-view page:
      `src/components/Sidebar.js`, `src/components/Chart.js`, etc. (or the
      framework's own convention -- one React/Vue component per file).
      State/data plumbing shared across components is its own leaf/module
      (e.g. `src/state.js`), not duplicated into each component file.
    - Split by lifecycle too: initial render, event wiring, and any
      data-fetching/update logic are usually separate leaves even within
      one component -- each is independently testable.
    - Worked example, "a dashboard with a sidebar, a chart, and a settings
      toggle":
          - 1. Sidebar component
            - [ ] 1.1 Build src/components/Sidebar.js (render + nav items)
            - [ ] 1.2 Wire sidebar click handlers to the view-switch state
          - 2. Chart component
            - [ ] 2.1 Build src/components/Chart.js
          - [ ] 3. Settings toggle (small enough as one leaf)
"""

DATA_ENGINEERING_ADDENDUM = """
    DATA-ENGINEERING-SPECIFIC DECOMPOSITION:
    - Decompose by PIPELINE STAGE, not by file: extract/load the raw
      input, validate/clean it (schema check, null/type handling, dedup),
      transform/aggregate it, then output/report the result -- each stage
      is its own leaf (or its own small group of leaves), never one leaf
      that writes the whole pipeline in one function. A leaf that loads
      AND cleans AND aggregates AND writes output is four leaves wearing a
      trenchcoat, the same way a multi-deliverable leaf is in any other
      domain.
    - Validate assumptions about the input EXPLICITLY as their own leaf
      before transforming: column names/types present, no unexpected
      nulls in required fields, row count sane -- don't let a silent
      schema mismatch produce a wrong-but-plausible-looking aggregate
      three stages later with no leaf that would have caught it.
    - One leaf per distinct transformation/aggregation (a groupby, a join,
      a derived column, a filter), not one leaf covering all of them --
      each is independently wrong-able and independently testable against
      a known expected value.
    - Testing leaves check EXACT expected values computed by actually
      running a correct reference implementation against the real fixed
      input (never hand-derived/guessed) -- this project's own
      data_engineering benchmark tier is graded exactly this way. Compare
      against a specific number/row, not "looks about right."
    - Worked example, "clean sales.csv and report monthly revenue by
      region":
          - 1. Load and validate
            - [ ] 1.1 Load sales.csv into a DataFrame, confirm expected columns/dtypes
            - [ ] 1.2 Handle nulls/duplicates per the brief's stated rule
          - 2. Transform
            - [ ] 2.1 Derive a month column from the date field
            - [ ] 2.2 Group by (month, region) and sum revenue
          - [ ] 3. Write the report CSV/summary (small enough as one leaf)
"""

GO_ADDENDUM = """
    GO-SPECIFIC DECOMPOSITION:
    - Package layout: one package per real concern (not one giant
      `main.go` past trivial size) -- a leaf per package/file, same as any
      other language's "one module per concern" rule.
    - Idiomatic error handling is not optional: a function that can fail
      returns `(result, error)`, callers check it immediately, and a
      wrapping call adds context with `fmt.Errorf("doing X: %w", err)`
      rather than swallowing or panicking on an expected failure (reserve
      `panic` for programmer errors, never for normal failure paths like
      bad input or a missing file). Note this in the leaf that touches
      fallible operations (I/O, parsing, network) so it isn't silently
      skipped.
    - Table-driven tests are Go's idiomatic unit -- a `[]struct{...}` of
      cases run through one `for range` loop with `t.Run` per case IS one
      leaf even though it covers many inputs, unlike a language where
      bundling many cases in one leaf would be under-decomposition: the
      table itself is the natural, single unit of coverage here.
    - Testing leaves, cheapest-first: `go build ./...` (catches
      compile-time errors across every package fast), then `go vet
      ./...`, then `go test ./...` -- in that order, same
      cheapest-and-most-certain-first principle as any other language.
    - Worked example, "a CLI that fetches and formats weather data":
          - 1. weather package
            - [ ] 1.1 Add Fetch(city string) (Weather, error) -- HTTP call + error wrapping
            - [ ] 1.2 Add table-driven tests for Fetch's parsing logic (success + malformed response cases)
          - [ ] 2. cmd/weather main.go: flag parsing, call weather.Fetch, print result (small enough as one leaf)
"""

HTML_CSS_ADDENDUM = """
    HTML/CSS-SPECIFIC DECOMPOSITION (static page, no JS framework or build
    step -- see the javascript addendum instead once a framework, bundler,
    or real interactivity beyond `<details>`/`:hover`/native form
    validation enters the picture):
    - One leaf per distinct STRUCTURAL SECTION/COMPONENT (a header, a
      pricing table, an FAQ list, a footer), not one leaf for "write the
      HTML" and a separate one for "write the CSS" as two giant blobs --
      markup and its styling for the same section are usually one leaf
      together, split further only when the section itself has more than
      one independent piece.
    - For any REPEATED unit the brief names a count for ("3 pricing
      tiers", "5 FAQ items"), that count is part of the leaf's own
      definition, not something to discover later -- a leaf covering "the
      pricing tiers" must produce exactly that many, and the plan should
      say so explicitly so it is checkable.
    - Use semantic elements as the default, not an afterthought: `nav`,
      `header`, `main`, `section`, `article`, `footer`; a real `button`
      for an action, not a `div` with a click handler; a `label` tied to
      every form input. Note this in the leaf that builds each section
      rather than as a separate "add semantics" pass at the end.
    - Add `data-*` attribute hooks deliberately wherever a later
      mechanical check will need to find/count something (a specific
      tier, a specific FAQ item) -- see this project's own html-tier
      benchmark tasks, which grade exactly this way (structural presence
      via `data-*` hooks, not visual judgment).
    - Testing leaves here are STRUCTURAL, not JS-driven: parse the file
      (stdlib `html.parser`, or a grep/count check) and confirm the
      required elements/counts/attributes exist -- no dev server or
      browse_webpage needed for a page with no JS. The one exception is
      `<details>`/`<summary>` for an accordion: correct markup already IS
      correct expand/collapse behavior, native to the browser, so a
      structural check covers behavior too for that specific pattern.
    - Worked example, "an FAQ accordion with 5 questions":
          - 1. Page shell (head, container, heading)
            - [ ] 1.1 Build the HTML shell + CSS reset/base styles
          - 2. FAQ accordion
            - [ ] 2.1 Build 5 `<details data-faq-item>` entries, each with a `<summary>` question and an answer paragraph
            - [ ] 2.2 Style the accordion (open/closed states, spacing) -- native `<details>` behavior needs no JS
"""

SQL_ADDENDUM = """
    SQL/DATABASE-SPECIFIC DECOMPOSITION:
    - One leaf per TABLE (its own CREATE TABLE, with its own constraints/
      indexes) and one leaf per distinct QUERY -- not one leaf that
      defines the whole schema, and not one leaf that writes every query
      the brief needs. Each is independently wrong-able (a bad constraint,
      a bad join) and independently testable.
    - Separate SCHEMA DESIGN from DATA SEEDING from QUERY leaves -- seed
      data and the queries that read it are both downstream of the schema
      being right first, so order leaves schema -> seed -> queries, not
      interleaved.
    - Foreign keys, NOT NULL, and other constraints belong in the same
      leaf as the table they constrain, not a separate "add constraints"
      pass at the end -- a constraint discovered missing after seeding is
      a much more expensive fix than one written with the table.
    - NEVER verify a query by reading it and reasoning "this looks
      right" -- always actually RUN it against a real (or in-memory,
      e.g. sqlite for a throwaway check) database with representative
      data and inspect the real result set, per this project's own
      verification standard applied to SQL specifically.
    - Worked example, "users and orders schema with a top-spenders query":
          - 1. Schema
            - [ ] 1.1 CREATE TABLE users (id, name, email UNIQUE, ...)
            - [ ] 1.2 CREATE TABLE orders (id, user_id FK -> users, amount, created_at)
          - [ ] 2. Seed representative test data (small enough as one leaf)
          - [ ] 3. Top-spenders query: JOIN + GROUP BY + ORDER BY, run it against the seeded data and confirm the result matches hand-computed expected rows
"""

STORY_ADDENDUM = """
    CREATIVE-WRITING-SPECIFIC DECOMPOSITION:
    - This is prose, not code -- leaves are NOT "one file each." The
      deliverable is usually still ONE output file (e.g. `story.md`); the
      Implementation section instead breaks the piece into its major
      narrative beats (setup/hook, rising action, turn/complication,
      climax, resolution -- adapt to whatever shape the brief actually
      calls for), one leaf per beat, drafted into that same file as each
      leaf is ticked (append_to_file per beat, not one giant write_file
      for the whole piece).
    - A leaf that says "write the story" is not a leaf -- name the beat. A
      leaf listing several required elements from the brief (a named
      character, a specific object, a required plot point) joined by
      "and"/"+" is still ONE leaf if they all belong to the SAME beat;
      split only when they land in different beats.
    - Testing leaves check what prose can actually be checked
      mechanically: word count within the brief's stated range, every
      concrete noun/name the premise requires actually appears, no
      leftover chat-template/placeholder tokens -- NOT "is it good," which
      no mechanical check here can judge (see VERIFICATION_RULES, and the
      benchmark's own story-tier writeup for why quality itself isn't
      gradeable this way).
"""

# "generic" is deliberately absent: callers fall back to the existing
# PLAN_FORMAT_RULES for any goal that doesn't match a known type.
TYPE_ADDENDA = {
    "python": PYTHON_ADDENDUM,
    "javascript": JAVASCRIPT_ADDENDUM,
    "html_css": HTML_CSS_ADDENDUM,
    "story": STORY_ADDENDUM,
    "data_engineering": DATA_ENGINEERING_ADDENDUM,
    "go": GO_ADDENDUM,
    "sql": SQL_ADDENDUM,
}
