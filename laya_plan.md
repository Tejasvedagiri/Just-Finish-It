# Planner rewrite: Architect → Lead → Task, every node judged by Laya

Status: **draft for review.** Nothing is implemented. Decisions made so far
are in §10; open questions are in §11.

The whole pipeline after this change:

```
planner   Architect (base + design) → Lead (folders, files, stubs)
          → Task (implement / integrate leaves), every node judged by Laya
   → imp (Dev)  one function + its unit test per leaf, one small episode each (§6)
   → reviewer   one end-to-end test of the app (§7)
   → cleanup
```

There's no separate Program Manager phase and no separate testing phase.

Today's planner, for comparison: [docs/phase-planner.md](docs/phase-planner.md).

## 0. Core objective: every LLM call fits in 20k tokens

**Every LLM episode, whether planner role or Dev, must fit in
`EPISODE_TOKEN_BUDGET` (default 20,000) tokens.** That covers system
prompt, brief, tool results and the model's own turns.

Everything else in this design serves that goal:

- **Small scope.** Roles see only their one node, not a shared, growing
  history (§4.1).
- **Pull, don't push.** An episode starts with a **small brief**: its role
  prompt, the one node, and a short **index** of what exists (runbook entry
  names, design keys, the node's files). Everything else it **pulls with
  tools**, only when it needs it: design entries, runbook commands, one
  function out of a file, a grep. Nothing is pre-loaded "just in case"
  (§4.8).
- **Written handoffs.** What a later role needs is written down (leaf
  fields, the design and runbook tables, stubs on disk), where it can be
  pulled, never left in a conversation.
- **Laya's rule for GOOD.** It keeps breaking a node down until it's the
  smallest sensible unit. A GOOD leaf is one Dev can finish, with its unit
  test, inside one 20k episode.
- **A hard budget in code.** The runner counts every episode's tokens. An
  episode that hits the budget is stopped, and that is treated as proof
  its node was too big (§5.3). A token count is a fact; Laya's opinion
  isn't.

**Why 20k:** the models JFI runs against usually have a **32k or 40k**
context window (today's `CONTEXT_SIZE` default is 32768). 20k leaves room
for the model's own reply and for token-estimate error. The budget must
stay below the window: at startup, `EPISODE_TOKEN_BUDGET` is checked
against `CONTEXT_SIZE × CONTEXT_RATIO` (today's `DEFAULT_CONTEXT_RATIO`,
0.7). If it's higher, the lower value is used and a warning is shown.

Today's JFI re-sends one growing history and compresses it
(`KEEP_RECENT_BLOCKS`, digests). With 20k episodes that machinery is
mostly unnecessary for the planner and Dev. It stays for the reviewer and
cleanup.

## 1. The three layers

Each role works at a different layer of the design, not just a smaller size
of the same thing.

| Role | Decides | Writes to disk? | Its child nodes |
|---|---|---|---|
| **Architect** | the **base and design**: language + version, frameworks, database, ORM, package manager, test framework, components, contracts between components, the runbook, the e2e scenario | no | one node per **component** |
| **Lead** | the **structure** of one component: folders, modules, file names (source + test), and which functions go in each file | **yes:** creates the folders and files with **stubs** (§4.5) | one node per **file** |
| **Task** | the **work items** for one file: implement each stubbed function, integrate it where it's used | only to add stubs when splitting a function (§4.6) | one leaf per **function** or **integration step** |

**Worked example: "a FastAPI todo service"**

- **Architect** records the design:
  - Python 3.12, FastAPI, SQLite via SQLModel, uv, pytest;
  - the components: `persistence`, `api`, `app entry`;
  - the contracts: the `Todo` fields, and that `api` gets a DB session
    from `persistence.get_session()`;
  - the runbook: `setup: uv sync`, `run: uv run uvicorn app.main:app
    --port 8000`, `stop`, `view: http://localhost:8000/docs`,
    `test: uv run pytest`;
  - the e2e scenario: "POST /todos, then GET /todos lists it, stop".
- **Lead**, for the `persistence` component:
  - creates `app/db/models.py`, `app/db/session.py` and
    `tests/db/test_session.py`;
  - fills them with **stubs**, e.g.

    ```python
    def get_session() -> Iterator[Session]:
        """JFI: yield a SQLModel session bound to the engine"""
        raise NotImplementedError
    ```

  - adds one node per file.
- **Task**, for `app/db/session.py`:
  - reads the stubs;
  - creates `implement get_session() -> Iterator[Session] in
    app/db/session.py` and `implement init_db() -> None in
    app/db/session.py`.
- **Task**, for `app/main.py`: creates `integrate todos router into app
  in app/main.py`.
- **Dev**, per leaf: replaces one stub body with the real code, writes its
  unit test, and runs that test.
- **Reviewer:** starts the app, runs the e2e scenario, stops it.

## 2. The loop

Every node carries a **planning status**: `GOOD`, `BREAKDOWN` or `REDO`.
A node with no status yet is **unjudged**.

Planning runs as **three gated stages, one per layer.** A layer must be
fully settled before the next layer starts. **Settled** means every node at
that layer is `GOOD` or `BREAKDOWN`, with no `REDO` and nothing unjudged.
Each role only ever redoes **its own** nodes: Architect never touches a
Lead file, and Lead never touches a Task leaf.

```
STAGE 1 · ARCHITECT
  Architect creates the component nodes (+ design, contracts, runbook, e2e)
  loop:  Laya judges every unjudged component node
         REDO → Architect redoes ONLY that node (may read other nodes)
  until every component node is GOOD or BREAKDOWN          ── gate ──┐
                                                                      ▼
STAGE 2 · LEAD            (never starts while any component node is REDO)
  for each BREAKDOWN component node:
         Lead scaffolds its files (stubs on disk), one file node per file
         → the component node becomes GOOD
  loop:  Laya judges every unjudged file node
         REDO → LEAD redoes ONLY that file node, fixing its own stubs
  until every file node is GOOD or BREAKDOWN               ── gate ──┐
                                                                      ▼
STAGE 3 · TASK            (never starts while any file node is REDO)
  for each BREAKDOWN file node:
         Task creates implement / integrate leaves
         → the file node becomes GOOD
  loop:  Laya judges every unjudged task leaf
         REDO      → TASK redoes ONLY that leaf
         BREAKDOWN → TASK splits it into helper functions (new stubs +
                     leaves) → the leaf becomes GOOD, its children unjudged
  until every node is GOOD                                 ── gate ──┐
                                                                      ▼
DEV (imp) starts
```

In words:

1. **Stage 1, Architect.** Architect writes the component nodes. Laya
   judges them. Architect redoes only its own REDO nodes, one at a time,
   until every component node is `GOOD` or `BREAKDOWN`. **Only then** does
   anything go to Lead.
2. **Stage 2, Lead.**
   - Each `BREAKDOWN` component node goes to Lead, which sees only that
     node plus the global context. Lead scaffolds its files and adds one
     file node per file. The component node becomes `GOOD`: its children
     now carry the work.
   - Laya judges the file nodes. A `REDO` file node goes back to **Lead**,
     which redoes only that node and fixes its own stub file (§4.7).
   - This repeats until every file node is `GOOD` or `BREAKDOWN`. **Only
     then** does anything go to Task.
3. **Stage 3, Task.**
   - Each `BREAKDOWN` file node goes to Task, which reads the file's stubs
     and creates implement/integrate leaves. The file node becomes `GOOD`.
   - Laya judges the leaves:
     - `REDO` → Task redoes only that leaf;
     - `BREAKDOWN` → Task splits it into helper functions, the leaf becomes
       `GOOD`, and its new children are judged.
   - This repeats until every node is `GOOD`.
4. **Done** when **every node is `GOOD`**, parents and leaves alike. Only
   then does Dev start. Dev's work queue is the GOOD **leaves** (nodes with
   no children), in `depends_on` order.

No minimum number of children at any stage; one is fine. Laya decides when
a node is the smallest it should be.

**Who does what is decided by the node's `level`** (who created it), not
by Laya:

| `level` | BREAKDOWN goes to | REDO goes to |
|---|---|---|
| `architect` | Lead | Architect |
| `lead` | Task | Lead |
| `task` | Task | Task |

Laya decides only the status: is this node right, and is it the smallest
it should be?

**Why the gates:** a lower layer builds on the one above it. Lead
scaffolding files for a component that's about to be redone would waste
an episode and leave stale stubs on disk. Settling each layer first means
every Lead and Task episode starts from a design that won't change under
it.

## 3. Tasks vs. the runbook: the key rule

**A task is a piece of the product to build.** At Task level that means
"implement `search()` in `app/search.py`" or "integrate the todos router in
`app/main.py`".

**How to operate the app is not a task.** It's **runbook** knowledge.

| ✅ Planned work | ❌ Not a task (goes in the runbook) |
|---|---|
| Architect: "use SQLite via SQLModel" (a design decision) | `uv sync` / `pip install` |
| Lead: create `app/db/session.py` with stubs | `uv run uvicorn app.main:app` |
| Task: implement `get_session()` in `app/db/session.py` | kill the server / `kill <pid>` |
| Task: integrate the todos router in `app/main.py` | open `http://localhost:8000/docs` |
| | `uv run pytest` |

- **No separate test tasks.** Every implement and integrate leaf carries
  its own unit test (§6). Lead creates the test *files* with stubs; each
  leaf names its test file.
- **Laya checks the rule.** A node that's really an operational step gets
  **REDO** with reason "operational". Architect moves it into the runbook
  and deletes the node.

### 3.1 The runbook table (global context)

**Everything needed to run the code**, in its own DB table
(`RunbookEntry`, keyed by `session_id`):

| Field | Example |
|---|---|
| `name` | `setup`, `run`, `stop`, `view`, `test`, `test_one`, `build`, `logs`, `e2e`, … |
| `command` | `uv run uvicorn app.main:app --port 8000` |
| `notes` | "needs `.env` with `DATABASE_URL`; serves on :8000" |
| `verified` | true once a phase has actually run it successfully |

- **`test_one`** is how to run a single test, e.g. `uv run pytest {test_id}`.
  Dev uses it for its one-test-per-leaf runs.
- **Who writes it:** Architect drafts it; Lead/Task can add entries; Dev and
  the reviewer **correct and verify** entries the first time they really
  run them.
- **Who reads it:** every role and phase, **on demand**. The brief carries
  only the index (entry names, one line); the model pulls the entry it
  needs with `runbook_get(name)`.
- **Tools:** `runbook_set(name, command, notes="")` (upsert),
  `runbook_get(name=None)` (one entry, or the index).
- **What it replaces:** the `run_commands` key that today's
  `CONTEXT_CACHE_RULES` asks the model to `context_save`.

### 3.2 The design table (global context)

Same idea for the design, in its own table (`DesignEntry`, keyed by
`session_id`), instead of scattered `context_save` keys:

| `kind` | Example |
|---|---|
| `stack` | "Python 3.12, FastAPI, SQLite via SQLModel, uv, pytest" |
| `component` | "persistence: models + session handling, in `app/db/`" |
| `contract` | "`api` gets a session via `persistence.get_session() -> Iterator[Session]`" |
| `convention` | "tests mirror `app/` under `tests/`; snake_case modules" |
| `assumption` | "single-user; no auth" |
| `out_of_scope` | "no frontend" |

- **Written by** Architect (Lead may add `convention` entries).
- **Pulled on demand:** the brief carries only the index (kinds + keys);
  the model reads what it needs with `design_get(kind=None, key=None)`.
- **Tools:** `design_set(kind, key, text)` and `design_get(...)`.
- **Why it's a table:** Lead, Task and Dev see only one node, so the design
  and the runbook are their *only* view of the whole app. Being keyed means
  an episode can pull one contract instead of the whole design.

## 4. The roles in detail

### 4.1 What each role call can see

**Breaking down:**

| Part | Architect (create) | Lead (one component node) | Task (one file or task node) |
|---|---|---|---|
| The goal | full | — | — |
| The node being worked on | — | ✅ | ✅ |
| Other nodes | via `get_plan` | ❌ | ❌ |
| Design + runbook | writes them | pulls what it needs | pulls what it needs |
| Repo | may read | may read; **creates** its files with stubs | reads its file (the stubs are the brief) |

**Redoing** (each role only redoes nodes it created, §4.7):

| Part | Architect redo (a component node) | Lead redo (a file node) | Task redo (a task leaf) |
|---|---|---|---|
| The flagged node + Laya's reason | ✅ | ✅ | ✅ |
| Its parent | — (top level) | ✅ the component node it came from | ✅ the file node it came from |
| Its siblings | ✅ all component nodes | ✅ the other file nodes from the same component (Lead's own output) | ✅ the other leaves from the same file (Task's own output) |
| Design + runbook | pulls, may update | pulls what it needs | pulls what it needs |
| Repo | may read | its own stub file (fix via `scaffold_file`) | the file's stubs |

- **Why a redo gets the parent and siblings:** a redo's most common
  reasons are "duplicate" and "doesn't fit". Neither can be fixed without
  seeing what's around the node. The siblings are the role's own output,
  so it's not seeing anything outside its layer.
- **Cross-component problems** (a file that really belongs to *another*
  component, or a missing contract) can't be fixed by Lead or Task. They
  **escalate** one layer up instead (§4.7).
- **"✅" means available, not pre-loaded.** Only the node itself (plus
  Laya's reason on a redo) is in the brief. Parents, siblings, files and
  design entries are pulled with tools (§4.8).
- **Budget:** one module (`planner/episode.py`) builds every brief and
  counts every episode's tokens against the budget.

### 4.2 Rules for every role

1. **Fixed node format:** `<verb> <what> in <where>: <expected result>`.
   Laya judges only this text, and for Lead/Task it's the whole brief.
2. **Length limit:** `PLANNER_ITEM_MAX_CHARS` (**`.env` variable**, default
   200), enforced in `add_leaf` / `update_leaf`.
3. **A `done_when` on every node.** On implement/integrate leaves it's the
   **unit test case** (§4.6).
4. **No minimum number of children.** Split into as many as the work
   really has, one included. Laya decides if a child needs splitting again.
5. **Self-check before finishing:** no operational steps; every node has
   `done_when` and `files`.
6. **Carried over:** one deliverable per node; investigation work split at
   most once (`MAX_INVESTIGATION_DEPTH` stays in code); never touch done
   work.

### 4.3 Architect: base and design

1. **Look at the repo first.** Empty or existing? For existing code, record
   its conventions (`design_set("convention", …)`).
2. **Decide the stack** (`design_set("stack", …)`).
3. **Split into components** in build order, with `depends_on` between
   them. One component node each, plus a `component` design entry.
4. **Contracts** at component boundaries (`design_set("contract", …)`).
   They're how Lead/Task/Dev know how their piece connects.
5. **The runbook:** `setup`, `run`, `stop`, `view`, `test`, `test_one`,
   `build`, unverified.
6. **The e2e scenario** (runbook `e2e`): one realistic user flow with
   expected results, for the reviewer (§7).
7. **Goal coverage self-check:** every requirement in the goal maps to a
   component. Laya can't check this (§5.4).
8. **Assumptions and out-of-scope** as design entries.
9. **Writes nothing to disk.**
10. **Redo mode:** §4.7. **Extend mode (re-plan):** §8.

### 4.4 Lead: folders, files and stubs

Lead runs once per component node Laya marked `BREAKDOWN`.

1. **Design the structure** of this component (folders, modules, source
   and test file names), following the design (pull the `stack`,
   `convention` and relevant `contract` entries).
2. **Create every file with `scaffold_file`** (§4.5), never with free-form
   `write_file`. Each file gets a purpose line and one **stub** per
   function: real signature, a one-line `JFI:` docstring or comment, and
   a not-implemented body.
3. **In an existing repo,** `scaffold_file` on an existing file **appends**
   the new stubs and never touches existing code.
4. **One child node per source file:** `scaffold app/db/session.py:
   session handling`, with `files` = the source file + its test file, and
   `done_when` = every stub in it is implemented and tested.
5. **No real code.** `scaffold_file` enforces it: it only writes stubs.

**Why stubs (not comments):**

- imports between files work from the start;
- signatures are real, and type-checkable, before Dev begins;
- Dev fills in bodies instead of inventing structure.

### 4.5 `scaffold_file`: how Lead (and Task) write to disk

```
scaffold_file(path, purpose, stubs=[{"signature": "...", "does": "..."}])
```

- **Stub bodies are generated by the tool from the language, never
  written by the model:**

  | Language | Stub body |
  |---|---|
  | Python | `raise NotImplementedError` |
  | TypeScript / JavaScript | `throw new Error("not implemented")` |
  | Go | `panic("not implemented")` |
  | Rust | `todo!()` |
  | Others (e.g. SQL) | comment-only fallback |

- **`signature`** is the declaration line in the file's language (`def
  get_session() -> Iterator[Session]:`, `export function search(q: string):
  Todo[]`). The tool validates it's a single declaration line.
- **`does`** becomes a `JFI: <does>` docstring/comment: the greppable
  marker Dev removes when it implements the stub.
- **Folders** are created as needed.
- **On an existing file,** the tool appends. It never overwrites.
- **Returns** the rendered stubs, so Lead sees exactly what Task will see.
- **Available to** Lead and Task only.

Why a dedicated tool: prompt text alone won't stop a model from writing
the function while it's in the file. Generating the body in code is the
backstop, the same reasoning as `MAX_LEAF_DEPTH` living in code.

### 4.6 Task: implement and integrate leaves

Task runs once per file node (or oversized task leaf) Laya marked
`BREAKDOWN`.

1. **Read the file.** Its `JFI:` stubs are the brief.
2. **One implement leaf per stub:** `implement <signature> in <file>:
   <does>`, with:
   - `files`: the source file + its test file;
   - `done_when`: **the unit test case**, one concrete input → expected
     output (e.g. `binary_sort([3,1,2]) -> [1,2,3]`).
3. **Integration leaves** where the file wires things together:
   `integrate <what> into <where> in <file>`. These have a unit test too
   (e.g. `TestClient(app).get("/todos").status_code == 200`).
4. **Order** leaves with `depends_on`: implement before integrate, and
   helpers before callers.
5. **A task leaf Laya marks `BREAKDOWN`** (a function too big for one Dev
   episode) comes back to Task. Task adds helper stubs to the file with
   `scaffold_file`, and creates one implement leaf per helper plus the
   original (now smaller).
6. **Reuse before inventing:** in an existing repo, a leaf may say "reuse
   `x()`" instead of planning a duplicate.

### 4.7 Redo: each role redoes only its own nodes

A `REDO` node goes back to **the role that created it**. Architect never
touches a Lead file, and Lead never touches a Task leaf. The redo episode
gets the flagged node, Laya's reason, the node's parent and siblings
(§4.1), and "this node is badly designed; redo only this node". It changes
**only that node** (plus `depends_on` pointing at it).

| Reason | Architect (component node) | Lead (file node) | Task (task leaf) |
|---|---|---|---|
| operational | `runbook_set` the command, delete the node | same | same |
| vague | rewrite with a concrete deliverable + `done_when` | rewrite the node, and re-`scaffold_file` its stubs | rewrite the leaf (signature, test case) |
| duplicate | delete it, or rewrite it to cover what the other doesn't | delete the node **and its stub file**, or merge its stubs into the sibling's file | delete it, or rewrite it |
| doesn't fit the design | rewrite it, or `design_set` a missing contract | rewrite it within the design (Lead can't change the design) | rewrite it within the file's stubs |

**Lead's redo fixes its own files on disk.** Lead created the stubs before
Laya judged the file node, so a Lead redo also fixes the file:

- `scaffold_file` (re-)writes stubs;
- a new stub-only `unscaffold_file(path)` removes a file (or stubs) Lead
  created. It **refuses** if the file contains anything besides JFI stubs,
  so it can never delete real code.

Stale stubs are cleaned up by the role that wrote them, in the same
episode.

- **Afterwards:** the rewritten node becomes unjudged and Laya re-judges it
  in the same stage.
- **Redo cap:** `PLANNER_REDO_CAP` (default 2) per node. The next REDO sets
  it `GOOD` with a reviewer note, so a stage can always settle and its
  gate can open.
- **Escalation, when a fix needs the layer above.** Some problems can't
  be fixed at the layer where they're found. Lead can't change the design;
  Task can't move a function to another file. The redo role then calls
  `escalate(node_id, why)` instead of rewriting:
  - the node's **parent** is set `REDO` for *its* creator, with `why` as
    the reason: a file node escalates to its component (Architect); a
    task leaf escalates to its file (Lead);
  - that parent's un-done subtree is **paused**: nothing under it is
    judged, broken down or implemented until the parent is settled again;
  - once the creator has redone the parent, its old children are
    re-judged. Children that no longer fit are removed by the role that
    created them (Lead removes its stubs with `unscaffold_file`).
- **Escalation is expected to be rare.** It's the exception path, not
  routine. Each escalation is logged in `PlannerVerdict` with its reason,
  and the escalation rate is a planning-quality signal: a high rate means
  the upper layer is under-specifying (for example, Architect missing
  contracts).
- **Escalation cap:** `PLANNER_ESCALATION_CAP` (default 1) per parent node.
  A second escalation of the same parent is accepted as-is with a reviewer
  note, so a component can't bounce between layers forever.

### 4.8 Tools for pulling context

Every episode gets the tools below instead of pre-loaded context. Each
read tool returns at most `TOOL_RESULT_MAX_TOKENS` and says how to get
more, so pulling stays cheap.

| Tool | Returns | Typical use |
|---|---|---|
| `get_leaf(id)` | one node's fields | a redo pulling the parent or a sibling |
| `list_nodes(parent_id)` | children of one node, one line each | a redo checking siblings for duplicates |
| `design_get(kind=None, key=None)` | the index, or one design entry | "what's the `Todo` contract?" |
| `runbook_get(name=None)` | the index, or one runbook entry | "how do I run one test?" |
| `list_dir(path)` | one directory level | Lead checking existing layout |
| `read_symbol(file, name)` | one function/class (a stub or real code) | Dev reading its stub; Task reading a file's stubs |
| `list_symbols(file)` | the file's top-level names + `JFI:` markers | Task seeing what a file needs |
| `read_file(path, start, end)` | a line range | anything `read_symbol` can't do |
| `search_code(pattern, path)` | matching lines with file:line | Dev finding a helper to reuse |
| `context_lookup(key)` | one saved fact | as today |

Write tools stay role-specific: `scaffold_file` / `unscaffold_file` for
Lead and Task, `replace_symbol` plus today's file tools for Dev,
`design_set` / `runbook_set` as in §3.

Most of these already exist in some form (`read_file`, `get_leaf`,
`context_lookup`, `execute_command` + grep). New are `read_symbol`,
`replace_symbol`, `list_symbols`, `list_nodes`, `design_get`,
`runbook_get`, and the size caps.

## 5. Laya: the judge (and the Program Manager replacement)

### 5.1 The questions

Laya judges every unjudged node, **in process**, with the model loaded
only for the moment it's needed:

```python
from laya import Router

router = Router()                                   # load
results = router.predict_batch(                     # judge every unjudged node at once
    [{"state": state_for(node), "questions": QUESTIONS, "model": LAYA_MODEL}
     for node in unjudged_nodes])
router.unload(); del router; gc.collect()           # tear down to free memory
if torch.cuda.is_available(): torch.cuda.empty_cache()
```

- **One load per judge step, not per node.** Loading a checkpoint costs
  seconds (Laya's README: about 7 s on CPU), so the judge collects every
  unjudged node in the current step and scores them in **one**
  `predict_batch` call. It then tears the router down before the next LLM
  episode starts. A single node (e.g. one redo) is still one load +
  `predict` + teardown.
- **Why tear down:** JFI usually runs next to a local LLM (Ollama,
  llama.cpp, vLLM) on the same machine. Keeping Laya's model resident would
  hold RAM/VRAM the LLM needs for its 32k–40k context. Tearing down after
  each judge step gives it back.
- **Pinned checkpoint:** `model=LAYA_MODEL` (default `"english"`) is passed
  on every request. JFI's node text is English, and pinning stops the
  router's language detection from loading the multilingual checkpoint as
  well because of identifiers in the text.
- **Laya is an optional extra** (`uv sync --extra laya`, which adds `laya`
  and pulls in torch + transformers). It's imported lazily inside the
  judge, so JFI without it still runs; every verdict then comes from the
  §5.2 fallback. **The PyInstaller binary doesn't bundle it** (D22):
  `dist/jfi` always uses the fallback rule, and Laya works when JFI runs
  from source with `--extra laya`.
- **First use downloads** the checkpoint from the Hugging Face hub into the
  normal HF cache. If that fails (offline, no access), the judge step falls
  back per §5.2 and says so once.

**State** (JSON, within Laya's ~320-token budget):
- the goal (short excerpt);
- the node's ancestor path;
- the node's description, `done_when` and `files`;
- its **`level`**.

```python
{
  "verdict": {"type": "choice",
    "instructions": "Given its `level`, is `node` the smallest sensible unit, or does it need the next layer?",
    "criteria": {
      "A": "good: smallest sensible unit; one function or integration step with its unit test",
      "B": "breakdown: correct, but needs the next layer (a component that needs files, a file that needs function tickets, or a function too big for one ticket)",
      "C": "redo: badly designed (an operational step, vague, a duplicate, or doesn't fit the design)"}},
  "redo_reason": {"type": "choice",
    "instructions": "If `node` is badly designed, why?",
    "criteria": {
      "A": "operational: installing, running, stopping, viewing or testing the app",
      "B": "vague: no concrete deliverable",
      "C": "duplicate of another task",
      "D": "does not fit the design: a missing contract, wrong component, or wrong place"}},
}
```

Neutral `A`/`B`/`C`/`D` keys are used because Laya is known to follow
`yes`/`no`/`true`/`false` label words instead of the text.

### 5.2 Confidence gate: when Laya is unsure or unavailable

Every Laya answer comes with a probability. Laya's own README warns that
the shipped checkpoints are over-confident and near chance on new domains
until calibrated. So the loop needs a rule for **"Laya answered, but isn't
sure"** (probability below `LAYA_MIN_CONFIDENCE`) and for **"Laya didn't
answer"** (the `laya` extra isn't installed, the checkpoint can't be
downloaded, or loading or prediction raised).

Default rule: **pick the safe choice for the node's level, with no LLM
call.**

| Level | Fallback status | Why it's safe |
|---|---|---|
| `architect` (component) | `BREAKDOWN` | a component always needs Lead's files anyway |
| `lead` (file) | `BREAKDOWN` | a file always needs Task's leaves anyway |
| `task` (function) | `GOOD`, plus a reviewer note | splitting further on a guess could loop; a leaf that really is too big hits the episode budget in Dev and gets split then (§5.3) |

- **REDO is never a fallback.** A redo costs an Architect call, and an
  unsure "badly designed" isn't worth one.
- **Opt-in LLM judge.** `PLANNER_JUDGE_FALLBACK=llm` asks one short LLM call
  instead, for teams that prefer accuracy over zero LLM cost.
- **Everything is logged.** Every verdict and every fallback goes to the
  `PlannerVerdict` table. That shows how often Laya is unsure, and gives
  labeled data (e.g. "Dev had to split a GOOD leaf") for measuring and
  fine-tuning Laya.

### 5.3 Mechanical checks (in code, independent of Laya)

- **Token budget (the core objective):** the runner counts tokens on
  every episode (brief + every tool result + every model turn). At
  `EPISODE_TOKEN_BUDGET` the episode is **stopped**. What happens next:
  - **Dev episode:** the leaf is too big. Its `plan_status` goes to
    `BREAKDOWN`, a short Task episode splits it (helper stubs + leaves,
    §4.6), Laya judges the new leaves, and Dev continues with them. Work
    already written stays on disk.
  - **Lead / Task episode:** the node it was breaking down is too big for
    one pass, so it's set `REDO` for its creator (reason "too big").
  - **Architect episode:** stop, with a visible warning. The goal itself
    needs narrowing.
- **Big tool results are cut, not dumped.** Every read tool has a size cap
  (`TOOL_RESULT_MAX_TOKENS`) and says how to read more (a line range, one
  symbol), so one careless `read_file` can't eat the budget.
- **Redo cap:** §4.7.
- **Depth cap:** `MAX_LEAF_DEPTH` bounds Task-on-Task splits. At the cap the
  node is `GOOD` with a note. `MAX_INVESTIGATION_DEPTH` stays.
- **Episode budget:** `MAX_PLANNER_EPISODES` caps planner LLM calls per
  pass. Anything left is `GOOD` with a visible warning.

### 5.4 Program Manager is replaced by Laya

The `product_owner` phase is **removed**. Laya's status on every node is
the per-node review, with no LLM call.

| Program Manager job | New home |
|---|---|
| per-leaf approve/reject | Laya status on every node |
| send the plan back for rework | REDO → the role that created the node redoes it (§4.7) |
| check the plan against the real repo | Architect's repo survey + Lead creating real stubs in the real repo |
| find goal requirements no node covers | Architect's coverage self-check + the reviewer. **Laya can't do this**, so it's the one real loss. |
| catch ordering bugs | `depends_on` enforced in code |

Changes this implies:

- **`PHASES`** = `["planner", "imp", "reviewer", "cleanup"]`.
- **Kept for old data:** `Phase.PRODUCT_OWNER` / `Phase.TESTING` and their
  display names.
- **Removed:**
  - the Program Manager loop, prompt, trigger and caps;
  - the `review_leaf` / `write_plan_feedback` tools;
  - `PRODUCT_OWNER_*` env vars;
  - `test_product_owner_loop.py`.
- **Fleet dashboard (`Just-Finish-It-Fleet`):** drops the `product_owner`
  and `testing` columns from its `PHASES`.

## 6. Dev (imp): one leaf per episode, one function + its unit test

The testing phase is **removed**. **Every implemented function or
integration step has its unit test attached.** After implementation, the
reviewer runs the end-to-end test (§7).

**Once, first:** run the runbook's `setup` entry, fix it if needed, and mark
it verified.

**Then, one fresh, short episode per GOOD leaf** (in `depends_on` order).
Dev never holds a long conversation. Each episode is one small, concrete
job: "implement `insert()` for the binary tree", "implement
`test_route`", "integrate the todos router". When it's done, the episode
ends and the next leaf starts clean. Today imp is one long conversation;
this replaces it.

**The brief is small; Dev pulls the rest.** Dev starts with only:

- the role prompt;
- the leaf (description, `done_when`, `files`);
- the index (runbook entry names, design keys).

It pulls what it needs with tools (§4.8): typically the stub it's
implementing (`read_symbol`), the relevant contract (`design_get`), the
`test_one` command (`runbook_get`), and maybe one neighbouring function it
calls. It doesn't read whole files it doesn't need. The whole episode
must stay under the 20k budget (§0, §5.3). Inside the episode:

1. `start_leaf(id)`.
2. **Implement:** `read_symbol` the stub, then `replace_symbol` it with the
   real function (the `JFI:` marker goes with the stub). **Integrate:**
   make the wiring change.
3. **Unit test:** write the test from `done_when` into the leaf's test
   file.
4. **Run it** with the runbook's `test_one`. Fix until it passes. If the
   budget runs out first, the leaf is split (§5.3).
5. `mark_leaf_done(id)`. Use `add_reviewer_note` for anything worked
   around.

**After the last leaf** (one short episode):

- run `build` once (whole-project build/typecheck);
- `grep` for leftover `JFI:` markers or not-implemented stubs. Each one is
  planned work nobody finished: fix it or note it.

Then `IMP_COMPLETE`.

**Removed with the testing phase:** its prompt, trigger,
`TESTING_COMPLETE`, `PHASE_SECTION["testing"]`, `TESTING_*` env vars. All
leaves are `phase="imp"`.

## 7. Reviewer: the end-to-end test

1. **Read** the runbook's `e2e` scenario, plus `get_reviewer_notes()`.
2. **Start** the app with `run` (`start_background_process`).
3. **Execute the scenario** the way a user would.
4. **Check** every expected result.
5. **Stop** with `stop` (`stop_background_process`), then
   `clear_finished_processes`.
6. **Verdict:**
   - **Pass:** "Review: PASS", and mark the runbook entries it used as
     verified.
   - **Fail:** `write_review_report` with the failing step, expected vs.
     actual, and likely file(s). This triggers re-plan in Architect extend
     mode (§8), capped by `MAX_REVIEW_ITERATIONS`.
7. **Also checked, without extra runs:** the reviewer notes, leftover
   `JFI:` markers, and goal coverage against the design's
   `assumption` / `out_of_scope` entries.

A scenario that can't run in this environment is reported as not checked,
never as a pass.

## 8. State, resume and re-plan

New `Leaf` columns (plus `_ensure_columns` for old DBs):

| Column | Values |
|---|---|
| `level` | `architect` / `lead` / `task`: who created it; decides who breaks it down |
| `plan_status` | `NULL` (unjudged) / `GOOD` / `BREAKDOWN` / `REDO`. Separate from Dev's todo/done/skipped `status`. |
| `redo_count`, `redo_reason` | §4.7 |
| `done_when` | finish condition; the unit test case on implement/integrate leaves |
| `files` | JSON list: source file(s) + test file |
| `depends_on` | JSON list of node ids |

New tables: `RunbookEntry`, `DesignEntry`, `PlannerVerdict`. All three are
in `export-db`.

- **Planning done:** every node is `GOOD`.
- **Resume:** read the statuses and continue from the same step of the
  loop. The current stage is derived, not stored: the lowest layer that
  isn't settled yet. There are no history markers.
- **Re-plan** (a failed review, or queued requests): Architect runs in
  **extend mode**, adding new component nodes or re-opening un-done ones
  (status cleared). The loop picks them up. **This fixes today's bug where
  re-planning is a no-op.**
- **`PLANNER_COMPLETE`** is still appended for the phase-level resume
  contract.

## 9. What changes in the code

| Area | Change |
|---|---|
| `src/JFI/planner/` (new) | `loop.py` (§2 loop + checks), `roles.py` (Architect create/redo/extend, Lead, Task prompts), `judge.py` (Laya questions, confidence gate, fallbacks), `laya_judge.py` (lazy `import laya`; `Router()` → `predict_batch` → `unload()` per judge step; `None` on any failure), `episode.py` (per-role packages, token counting against the budget). |
| `src/JFI/imp/` (new) or `runner` | Dev as one short, scoped episode per leaf (§6): small brief, pull tools, the token budget, and overflow → split (§5.3). |
| `tool/code_tools.py` (new) | `read_symbol` / `replace_symbol` (§4.8): one function at a time. Python via `ast`; other languages via the stub markers + a brace/indent scan. Every read tool gets a size cap with a "read more" hint. |
| `tool/scaffold_tools.py` (new) | `scaffold_file` (§4.5): language-aware stubs, the `JFI:` marker, append-only. `unscaffold_file` (§4.7): removes Lead's own stubs/files, refusing anything that isn't a pure JFI stub. |
| `tool/runbook_tools.py`, `tool/design_tools.py` (new) | `runbook_set/get`, `design_set/get`. |
| `tool/plan_db_tools.py` (escalation) | `escalate(node_id, why)` (§4.7): sets the parent `REDO`, pauses its subtree, enforces `PLANNER_ESCALATION_CAP`. |
| `tool/plan_db_tools.py` | `add_leaf` / `update_leaf` take `done_when`, `files`, `depends_on`; `level` is set from the calling role; length cap + `depends_on` validation. `get_plan` / `get_leaf` show the new fields. `review_leaf` removed. Lead/Task episodes get **no** `get_plan`. |
| `tool/note_tools.py` | `write_plan_feedback` removed. |
| `runner` | `PHASES = ["planner", "imp", "reviewer", "cleanup"]`. Tiered-planner constants, marker resume, `PLANNER_SINGLE_PASS`, the Program Manager loop and the testing phase are removed. The review report and queued requests go to Architect extend mode. |
| `simple_session_manager.py` | Planner, `product_owner` and `testing` prompts removed or moved; imp and reviewer prompts rewritten; the runbook/design **index** added to briefs (the entries themselves are pulled with tools); `CONTEXT_CACHE_RULES`' `run_commands` paragraph dropped. History compression kept for the reviewer/cleanup. |
| `task_rules.py` | Type addenda feed Architect (stack) and Lead (structure). |
| `models/` | `Leaf` columns + `_ensure_columns`; `RunbookEntry`, `DesignEntry`, `PlannerVerdict`. |
| `src/build_binary/` | Exclude `laya`, `torch` and `transformers` from the PyInstaller build (`--exclude-module`), so `dist/jfi` stays small even when the `laya` extra is synced in the build environment. The binary's startup says Laya is unavailable and the fallback rule is in use. |
| cleanup prompt | Scaffolded files are deliverables, never stray; leftover `JFI:` stubs are reported, not deleted. |
| Env | New: `LAYA_MODEL` (`english`), `LAYA_DEVICE` (unset = Laya's auto), `LAYA_BATCH_SIZE` (bounds one forward pass), `LAYA_MIN_CONFIDENCE`, `EPISODE_TOKEN_BUDGET` (20000), `TOOL_RESULT_MAX_TOKENS` (e.g. 3000), `PLANNER_ITEM_MAX_CHARS` (200), `PLANNER_REDO_CAP` (2), `PLANNER_ESCALATION_CAP` (1), `MAX_PLANNER_EPISODES`, `PLANNER_JUDGE_FALLBACK`. Removed: `PLANNER_SINGLE_PASS`, `PRODUCT_OWNER_*`, `TESTING_*`. |
| Status / fleet | Stage tags `Arc` / `Redo` / `Lead` / `Task` / `Judge`. The fleet repo drops `product_owner` + `testing`. |
| Docs | Rewrite `docs/phase-planner.md`, `phase-imp.md`, `phase-reviewer.md`; delete `phase-product-owner.md`, `phase-testing.md`; update `pipeline.md`, `plan-tree.md`, `AGENTS.md`, `README.md`, `JFI_ENV_TEMPLATE`. |

**Laya runs in process** (§5.1): a new optional extra `laya = ["laya>=0.3.21"]`
in `pyproject.toml`, imported lazily. It's loaded for each judge step and
torn down afterwards. Cost per judge step is one model load (seconds) plus
about 1.2 s per node on CPU, less on GPU and less when batched.

## 9A. Tests (real SQLite, real tmp dirs, observed behavior)

Laya is faked by monkeypatching `laya.Router` with a stub that records
calls and returns scripted answers. The LLM gets scripted responses, as in
today's `test_tiered_planner.py`. No model weights are needed in CI.

- **Load/teardown:** one judge step creates exactly **one** `Router`, makes
  **one** `predict_batch` call covering every unjudged node, and calls
  `unload()` before the next LLM episode, even when prediction raises.
- **Laya missing:** with `laya` not importable, planning still completes
  and every verdict is the §5.2 fallback, recorded as such.
- **Binary:** the build spec excludes `laya` / `torch` / `transformers`
  (checked on the PyInstaller arguments, not by building).

- **Gated stages:**
  - no Lead episode starts while any component node is `REDO` or
    unjudged;
  - no Task episode starts while any file node is;
  - Dev starts only when every node is `GOOD`.
- **Routing by level:**

  | Level | BREAKDOWN → | REDO → |
  |---|---|---|
  | `architect` | Lead | Architect |
  | `lead` | Task | Lead |
  | `task` | Task | Task |

  A broken-down node becomes `GOOD`.
- **Redo scope:** a redo changes only the flagged node (plus `depends_on`
  pointing at it); every other node is byte-identical afterwards. A Lead
  redo can fix its own stub file; Architect has no disk-writing tools.
- **Escalation:**
  - `escalate` sets the parent `REDO` for its creator and pauses the
    parent's subtree;
  - after the parent is redone, its children are re-judged;
  - a second escalation of the same parent hits the cap and becomes a
    reviewer note.
- **Pull, not push:**
  - a Lead/Task/Dev brief contains the node and the index, and **no** file
    contents, design entries or other nodes;
  - `design_get` / `runbook_get` / `read_symbol` return what's asked for;
  - every read tool cuts at `TOOL_RESULT_MAX_TOKENS` with a "read more"
    hint.
- **Token budget:**
  - an episode is stopped at `EPISODE_TOKEN_BUDGET`;
  - a Dev overflow sets the leaf `BREAKDOWN`, runs one Task split, and Dev
    continues with the new leaves, keeping the work already on disk;
  - a Lead/Task overflow sets the node `REDO` for its creator;
  - a budget above `CONTEXT_SIZE × CONTEXT_RATIO` is lowered, with a
    warning.
- **`scaffold_file` / `unscaffold_file`:**
  - stubs are rendered with the right body per language (Python, TS, Go,
    Rust) and carry a `JFI:` marker;
  - the tool appends to existing files without changing existing lines;
  - it refuses anything but a single declaration line;
  - `unscaffold_file` refuses a file containing non-stub code.
- **`read_symbol` / `replace_symbol`:** round-trip one Python function via
  `ast`, and one TS/Go function via the fallback scan, leaving the rest of
  the file byte-identical.
- **Dev:**
  - one fresh episode per leaf, whose messages contain nothing from the
    previous leaf's episode;
  - the leaf's `JFI:` marker is gone after it's done;
  - the end-of-imp scan reports a leftover marker.
- **Confidence gate:**
  - low confidence or Laya down → `BREAKDOWN` for architect/lead nodes,
    `GOOD` + a reviewer note for task leaves, with no LLM call;
  - the opt-in LLM judge path works;
  - all of it is recorded in `PlannerVerdict`.
- **Operational REDO:** a node like "run `uv sync`" becomes a runbook
  `setup` entry and the node is deleted.
- **Resume:** a crash in any stage resumes in the same stage, with nothing
  re-judged, re-split or re-scaffolded.
- **Phases:**
  - `PHASES` is `planner, imp, reviewer, cleanup`;
  - old sessions with `PRODUCT_OWNER_COMPLETE` / `TESTING_COMPLETE` still
    resume;
  - the removed tools are gone from the tool list.
- **Re-plan regression:** a failed review → Architect extend mode → at
  least one episode runs and a new node reaches `GOOD`.
- **Cleanup:** scaffolded files are never moved or deleted.
- **Prompts:** each role prompt string-matches its key rules (node format,
  `done_when`, no operational steps, "only this node", pull with tools);
  the imp and reviewer prompts match §6 and §7.

## 10. Decisions made

| # | Decision |
|---|---|
| D1 | Three planning layers: **Architect** = base + design; **Lead** = folders, modules, files, **with stubs on disk**; **Task** = implement and integrate leaves. |
| D2 | Operational steps are never tasks; they live in the **runbook table**, which every role and phase sees. |
| D3 | **Program Manager is replaced by Laya.** No LLM call. |
| D4 | **Every node has a Laya status:** GOOD, BREAKDOWN or REDO. A broken-down node becomes GOOD once its children exist. **Planning is done when every node is GOOD; only then does Dev start.** |
| D5 | **REDO goes back to the role that created the node,** and it redoes only that node. **Architect never redoes a Lead file**; Lead never redoes a Task leaf. Lead's redo fixes its own stub files. |
| D6 | **BREAKDOWN goes to the next layer** (Architect's node → Lead, Lead's → Task, Task's → Task). Lead and Task see only that one node when breaking it down. |
| D7 | **No minimum number of children.** Laya decides when a node is the smallest possible. |
| D8 | **Core objective: every LLM episode fits in 20k tokens.** Small, well-split tasks are how that's achieved. |
| D9 | **Every implemented function or integration step has a unit test attached.** After implementation, one end-to-end test. |
| D10 | **The testing phase is removed;** the reviewer runs the e2e test. |
| D11 | **Stubs, not comments:** Lead writes real, not-implemented stubs via `scaffold_file`. |
| D12 | **Design and runbook are tables,** keyed so an episode can pull one entry at a time. |
| D13 | **`PLANNER_SINGLE_PASS` is removed.** |
| D14 | **The node length cap is an `.env` variable** (`PLANNER_ITEM_MAX_CHARS`, default 200). |
| D15 | **Gated stages:** nothing goes to Lead until every Architect node is GOOD or BREAKDOWN; nothing goes to Task until every Lead node is GOOD or BREAKDOWN (§2). |
| D16 | **Dev is one short, fresh episode per leaf** ("implement binary tree insert", "implement test_route"), never a long conversation. |
| D17 | **Pull, don't push.** Episodes start with a small brief (role prompt, the node, an index); the model pulls design entries, runbook commands, symbols and files with tools as it needs them. |
| D18 | **Escalation is allowed:** a Lead or Task redo that needs the layer above sends the parent back as REDO (§4.7). It's expected to be rare; its rate is tracked as a quality signal. |
| D19 | **Dev overflow splits the leaf:** a Dev episode that hits the budget sends its leaf back for a Task split, and Dev continues with the pieces (§5.3). |
| D20 | **Budget vs window:** 20k per episode, for models with 32k–40k context windows, and never above `CONTEXT_SIZE × CONTEXT_RATIO`. |
| D21 | **Laya runs in process and is torn down after each judge step:** `Router()` → `predict_batch` over every unjudged node → `unload()`, so its memory goes back to the LLM. |
| D22 | **Laya stays out of the PyInstaller binary.** `dist/jfi` uses the fallback rule; Laya is available when running from source with `--extra laya`. |

## 11. Questions for you

None open. Every question is answered in §10.

**Tuned during implementation** (agreed: fix as we go, not blockers):

1. **Laya zero-shot quality** on real JFI nodes. Laya's README says base
   checkpoints are near chance on new domains. The `PlannerVerdict` log
   shows how often it's right; `LAYA_MIN_CONFIDENCE`, the question wording
   and, if needed, fine-tuning get adjusted from that.
2. **The 20k budget** on real goals: how many Dev episodes overflow, and
   how often Lead/Task escalate. `EPISODE_TOKEN_BUDGET`,
   `TOOL_RESULT_MAX_TOKENS` and the role prompts get adjusted from what
   real runs show.
