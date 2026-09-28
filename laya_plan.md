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

Laya judges every unjudged node, **in process**. The model is loaded once
per session, on first use, and **stays loaded**:

```python
from laya import Router

router = Router()                                   # once per session (lazy: loads on first predict)
results = router.predict_batch(                     # each judge step: every unjudged node at once
    [{"state": state_for(node), "questions": QUESTIONS, "model": LAYA_MODEL}
     for node in unjudged_nodes])
```

- **One batch per judge step, not one call per node.** The judge collects
  every unjudged node in the current step and scores them in **one**
  `predict_batch` call.
- **Two checkpoints, no multilingual:** JFI uses `english` and
  `typed-decisions`, created as `Router(max_loaded=2)` and preloaded with
  exactly those two. `multilingual` is never loaded or downloaded. Which
  checkpoint answers which question is open (§11).
- **Kept resident, not torn down.** Measured (§5.1.1): both checkpoints
  together cost about **3.5 GiB** of RAM, which is acceptable. An
  in-process `unload()` doesn't return that memory to the OS anyway: the
  allocator and Laya's tokenizer cache keep it. So tearing down would cost
  a reload on every judge step and save nothing.
- **If RAM ever matters:** running the judge in a **child process** that
  exits does free everything (measured: back to 0.02 GiB). It costs a
  ~4 s reload per judge step. `utils/laya_poc.py child` demonstrates it.
  Not the default.

#### 5.1.1 Measured cost (2026-09-28)

Measured on the development machine: Windows 11, 12 CPU threads, no GPU,
torch 2.14.0+cpu, laya 0.3.21, the `english` checkpoint pinned. One
`predict_batch` of 10 realistic JFI nodes × the 2 §5.1 questions.

| | Process RSS | Laya's share |
|---|---|---|
| Python baseline | 0.02 GiB | |
| after `import laya, torch` | 0.19 GiB | +0.17 GiB (libraries) |
| **model loaded + first batch done (resident)** | **2.01 GiB** | **+1.82 GiB** (model + tokenizer) |
| **peak during load / first predict** | **2.68 GiB** | **+2.49 GiB** momentary |
| after `unload()` + `gc.collect()` | 1.99 GiB | freed almost nothing |

**Both checkpoints** (`english` + `typed-decisions`, from
`utils/laya_poc.py`, same machine, cache warm):

| | In process | Child process |
|---|---|---|
| after `import laya` (lazy, torch not loaded yet) | 0.02 GiB | 0.02 GiB |
| **both loaded** (`preload`) | **3.48 GiB** (4.3 s) | **3.47 GiB** (4.1 s) |
| one `predict` per checkpoint | 0.2 s each | 0.2 s each |
| **after unload** | **3.49 GiB**: `unload()` + gc frees nothing | **0.02 GiB**: child exit frees everything |

- **`UNLOAD_LLM_BEFORE_LAYA=1`** (D35) does exactly that, and also swaps
  LM Studio's models out for the duration: snapshot, `lms unload --all`,
  Laya in a child process, then reload every model with its original
  context length, parallelism, identifier and TTL (reload also runs when
  Laya fails). For machines that can't hold the LLM and Laya at once.
- **Budget for it:** about **2.0 GiB resident** while JFI runs, with a
  momentary **2.7 GiB peak** at first load. The peak is the checkpoint
  being converted to fp32 on CPU; on disk it's stored as 16-bit
  (`model.safetensors` = 842,609,210 bytes).
- **Speed:** the first batch took 9.8 s including the load; a warm batch of
  10 nodes took about 7 s on CPU (≈0.35 s per node × question).
- **Disk:** Laya downloads **only the checkpoints it loads**. `Agent`
  restricts `snapshot_download` to the requested subfolder via
  `allow_patterns`. For `english` + `typed-decisions` that's **1.69 GB**
  (842,609,210 + 842,609,220 bytes of `model.safetensors`, plus
  tokenizers) in `~/.cache/huggingface`. (A multilingual copy found in the
  cache on this machine predates this measurement; nothing JFI does
  fetches it.)
- **Zero-shot quality, first look:** every answer's confidence was 0.41–0.59
  (below any sensible `LAYA_MIN_CONFIDENCE`), and the operational node
  "run the server with uv run uvicorn …" was *not* flagged REDO, though
  `redo_reason` did pick "operational" at 0.64. This matches Laya's README
  (near chance zero-shot) and confirms the fallback rule (§5.2) will be
  doing most of the judging until Laya is calibrated or fine-tuned.
- **Always name the checkpoint:** every request passes `model="english"`
  or `model="typed-decisions"` explicitly. The router's automatic language
  routing is never used, so it can't reach for `multilingual`.
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

The full database changes (every new table and column, and how old DBs
get them) are in **§13**.

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
| `src/JFI/planner/` (new) | `loop.py` (§2 loop + checks), `roles.py` (Architect create/redo/extend, Lead, Task prompts), `judge.py` (Laya questions, confidence gate, fallbacks), `laya_judge.py` (lazy `import laya`; one `Router()` per session, kept loaded; one `predict_batch` per judge step; `None` on any failure), `episode.py` (per-role packages, token counting against the budget). |
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

**Laya runs in process** (§5.1): the optional `laya` extra in
`pyproject.toml`, imported lazily, loaded once per session and kept
resident. It costs about 2.0 GiB RAM (§5.1.1) and about 0.35 s per node ×
question on CPU, warm.

## 9A. Tests (real SQLite, real tmp dirs, observed behavior)

Laya is faked by monkeypatching `laya.Router` with a stub that records
calls and returns scripted answers. The LLM gets scripted responses, as in
today's `test_tiered_planner.py`. No model weights are needed in CI.

- **Load once:** a session creates exactly **one** `Router`, lazily on
  the first judge step; each judge step makes **one** `predict_batch` call
  covering every unjudged node.
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
| D21 | **Laya runs in process with two checkpoints, `english` + `typed-decisions`, no multilingual, kept loaded for the session** (lazy, on first judge step; one `predict_batch` per judge step). About 3.5 GiB resident; no teardown. A child-process judge frees everything if RAM ever matters (§5.1.1). |
| D22 | **Laya stays out of the PyInstaller binary.** `dist/jfi` uses the fallback rule; Laya is available when running from source with `--extra laya`. |
| D23 | **G1 fix:** every brief starts with a never-trimmed **scope anchor**; each role has a short prompt and a fixed core tool set; Laya picks 0–3 optional tools per node at judge time (stored on the node); `load_tool` stays as an escape hatch for the optional pool (§12, G1). |
| D24 | **G2 fix: non-function files are Lead's scope.** Lead defines and skeletons manifests, config, fixtures, templates, migrations and docs as `artifact` file nodes; their leaves are verified by a mechanical check instead of a unit test. Project-wide files belong to a `project` component (§12, G2). |
| D25 | **G3 fix: existing code is marked, not stubbed.** Lead's `mark_change` puts `JFI-CHANGE:` / `JFI-DELETE:` above existing symbols; Task makes `modify` / `delete` leaves; Dev updates existing tests + adds one new test; bug fixes are `modify` leaves whose `done_when` is the failing case. |
| D26 | **G4 fix: non-code goals take a document path** (outline → sections → passages, mechanical checks instead of unit tests, the reviewer reads the whole document). |
| D27 | **Phase completion is leaf state, not model text.** Planning is complete when Laya has marked every node `GOOD`; imp when every leaf is `done`; the reviewer when every leaf is `passed`. Every leaf must reach a done state. `<PHASE>_COMPLETE` markers are written by the runner from the DB. |
| D28 | **G6:** Dev's queue is a topological order over the whole tree; `depends_on` is validated acyclic; a test failing on *another* stub's not-implemented body re-queues the leaf instead of "fixing" it. |
| D29 | **G7:** forced directives are stored per node and delivered in that node's next episode; queued requests go to Architect extend mode at idle; skip and pause work between episodes. |
| D30 | **G8:** review failures re-open the owning done leaves with a `fix_note` (no re-planning); extend mode only for genuinely missing work. |
| D31 | **G9:** a GOOD node at any level is a valid Dev leaf (no stub → edit in place; no unit test → mechanical check). |
| D32 | **G10:** the duplicate check runs inside the Laya judge step (embedding similarity between siblings). |
| D33 | **G11–G15, G17–G23** as written in §12. **G12:** the e2e can be a single command (`uv run pytest`, `npm run test`). **G13:** a crashed Dev leaf restarts fresh, with 3 attempts before it's split. **G16:** option (a): old sessions finish on the old pipeline (`pipeline_version`). |
| D34 | **Database:** every new table and column in §13, added for SQLite via `_ensure_columns`, and via an explicit migration for MySQL/Postgres. |
| D35 | **`UNLOAD_LLM_BEFORE_LAYA`** (`.env`, off by default): before each Laya judge step, unload every LM Studio model (`lms ps --json` snapshot, `lms unload --all`), run Laya in a child process so its memory is really freed, then reload each model with its `modelKey`, context length, parallelism, identifier and TTL, also when Laya fails. Implemented in `JFI.llm.lmstudio_control` + `JFI.planner.judge`. |

## 11. Questions for you

1. **Which checkpoint judges what?** `english` is the general base model;
   `typed-decisions` is fine-tuned on typed-decision workflows (Laya's
   README: 0.77 vs 0.36 on its benchmark). Proposal: during phase 8 tuning,
   **ask both** on every node and log both in `PlannerVerdict`; then keep
   whichever agrees better with real outcomes (or use `typed-decisions`
   for the verdict and `english` for embeddings: duplicates and tool
   picks). OK?
2. **Skipped leaves (G5):** a leaf the user skips (Ctrl+K/Q) is terminal
   but not `done`. Should a phase with skipped leaves still count as
   complete (the user chose to skip), or stay open until someone un-skips
   or deletes them?

**Tuned during implementation** (agreed: fix as we go, not blockers):

1. **Laya zero-shot quality** on real JFI nodes. Laya's README says base
   checkpoints are near chance on new domains. The `PlannerVerdict` log
   shows how often it's right; `LAYA_MIN_CONFIDENCE`, the question wording
   and, if needed, fine-tuning get adjusted from that.
2. **The 20k budget** on real goals: how many Dev episodes overflow, and
   how often Lead/Task escalate. `EPISODE_TOKEN_BUDGET`,
   `TOOL_RESULT_MAX_TOKENS` and the role prompts get adjusted from what
   real runs show.

## 12. Gap review

This section is a check of the plan against the current code, looking for
things today's JFI does that the plan never accounts for, and for places
where the plan contradicts itself. Each gap has a **proposed fix**, to be
confirmed before implementation. Ordered by severity.

### A. Blockers: the design doesn't work without these

**G1. The fixed overhead alone would eat most of the 20k budget.**
- **Measured:** today's system prompts are about **5,000 tokens** on their
  own (imp 5,003; reviewer 4,603; planner 4,676; chars/4). The shared rule
  blocks (`PLAN_FORMAT_RULES` 1,042 + `CONTEXT_CACHE_RULES` 652 +
  `VERIFICATION_RULES` 1,274) are in every prompt. The deferred tool
  schemas add about **5,900 tokens** once all 32 are unlocked, and today an
  unlocked tool stays in every request for the rest of the session.
- **Result:** a Dev episode could start at 8–11k before reading a single
  line of code.
- **Fix (agreed):** three layers, in this order.

**G1.1 The scope anchor: never trimmed.** The constraint on everything
below is that an episode must **never lose the context of its own scope**.
So every brief starts with a fixed **scope anchor**. It's built in code,
always present, and never cut to save tokens:

```
SCOPE
  role:      Dev (implement one leaf)          ← or Architect / Lead / Task
  node:      [id=42] implement get_session() -> Iterator[Session] in app/db/session.py
  done_when: get_session() yields a Session that can query Todo
  files:     app/db/session.py, tests/db/test_session.py
  path:      persistence  >  app/db/session.py          (ancestor descriptions, one line each)
  may:       edit the files above; add one unit test; pull design/runbook/code with tools
  must not:  change other files' behaviour, other nodes, the design, the plan
  finish:    mark_leaf_done(42)                  ← or finish(node_id)
```

- **Size:** about 150–250 tokens.
- **Budget:** counted first; the budget check never removes it.
- **Everything else is trimmed or pulled around it:** role rules, tool
  schemas, pulled context.
- **Redos:** the anchor also carries Laya's reason.
- **Escalation:** the anchor says which node was escalated.

**G1.2 Per-role prompts and a fixed core tool set.**

- **Short prompts:** each role gets its own short prompt (≤ ~1.5k tokens).
  The long shared blocks (`PLAN_FORMAT_RULES`, `CONTEXT_CACHE_RULES`,
  `VERIFICATION_RULES`) are rewritten per role, keeping only the rules that
  role acts on. Architect doesn't carry Dev's testing rules; Dev doesn't
  carry plan-shaping rules.
- **A fixed core tool set per role,** defined in code (≤ ~1.5k tokens of
  schemas). It replaces session-wide `load_tool` unlocking, where an
  unlocked tool stayed in every request for the rest of the session.

  | Role | Core tools (always present) |
  |---|---|
  | Architect | `get_plan`, `get_leaf`, `add_leaf`, `update_leaf`, `delete_leaf`, `design_set/get`, `runbook_set/get`, `list_dir`, `read_file`, `search_code`, `finish` |
  | Lead | `get_leaf`, `add_leaf`, `design_get`, `runbook_get`, `list_dir`, `read_symbol`, `scaffold_file`, `unscaffold_file`, `escalate`, `finish` |
  | Task | `get_leaf`, `add_leaf`, `list_symbols`, `read_symbol`, `scaffold_file`, `design_get`, `escalate`, `finish` |
  | Dev | `read_symbol`, `replace_symbol`, `list_symbols`, `read_file`, `write_file`, `search_code`, `design_get`, `runbook_get`, `execute_command`, `add_reviewer_note`, `mark_leaf_done` |
  | Reviewer | `runbook_get`, `start_background_process`, `stop_background_process`, `execute_command`, `read_file`, `get_reviewer_notes`, `write_review_report`, `finish` |

  Lists are indicative. The exact sets are fixed during implementation,
  under the cap.

**G1.3 Laya picks optional extras; `load_tool` is the escape hatch.**

- **The optional pool:** tools most leaves don't need, e.g. browser,
  `capture_screenshot` / `view_image`, `extract_video_frames`,
  `fetch_webpage_images`, `ask_llm`, background processes (for Dev),
  `context_save` / `context_lookup`.
- **Laya picks 0–3 of them per node,** by embedding shortlist
  (`predict_shortlist` + `cached_embed_fn`: cosine similarity between the
  node text and each tool's one-line description). It's one cheap pass,
  not one question per tool.
- **No extra model load.** The pick runs **in the same judge step that
  marks the node `GOOD`**, while Laya is already loaded, and is stored on
  the node (new `tools` column, JSON list). Dev reads it when its episode
  starts.
- **Picks only add.** Laya can never remove a core tool, so a wrong pick
  costs a few hundred tokens at most, never a missing capability.
- **Escape hatch:** `load_tool` stays, scoped to the optional pool. If
  Laya missed something, the episode loads it itself; a miss costs one
  tool call, never a stuck leaf. Loaded tools last for that episode only.
- **Logged in `PlannerVerdict`:** each pick, whether the picked tool was
  used, and every `load_tool` of a tool Laya didn't pick. That's the
  accuracy signal and the fine-tuning data, the same as the verdict log.
- **Laya off / unavailable:** no extras are picked; the episode uses
  `load_tool` as needed.

**Expected result:** fixed overhead drops from 8–11k to about 3k (anchor +
prompt + core tools). Laya's picks save another 0.5–1.5k on leaves that
would otherwise load heavy tools "just in case".

**Tests:**
- every role's anchor + prompt + core schemas stays under
  `ROLE_OVERHEAD_MAX_TOKENS` (e.g. 3,500);
- the anchor is present and unchanged in every episode's first message,
  including after budget trimming, redos and escalations;
- Laya picks can't remove core tools;
- `load_tool` works for pool tools, refuses anything outside the pool, and
  expires with the episode;
- with Laya off, no extras are picked and the episode still completes.

**G2. Most of the product isn't functions: config, dependencies, fixtures,
assets, docs.** The stub → implement → unit test model only covers code
functions. A real app also needs:
- `pyproject.toml` / `package.json` **with dependencies** (the runbook's
  `setup: uv sync` needs them to exist);
- `.env.example`, a `Dockerfile`, DB migrations / SQL schema;
- HTML/CSS templates and static files;
- shared test fixtures (`conftest.py`, a test DB);
- a README.

None of these has a function to stub or a unit test to write.
- **Fix (agreed): defining them is part of Lead's scope.** Lead already
  owns "folders, modules, files" for its component. Non-function files are
  files too, so Lead defines them alongside the source files:
  - **What Lead decides:** which non-function files the component needs
    (the manifest and its dependency list, `.env.example` keys, the
    Dockerfile, migration files, templates/static files, test fixtures such
    as `conftest.py`, the README), where they go, and what each must
    contain.
  - **How it writes them:** with `scaffold_file` too, as a **skeleton**:
    - the file's structure in its own format (a `pyproject.toml` with
      `[project]` and the dependency list the design calls for; a
      `conftest.py` with the fixture's signature; a template with its
      blocks);
    - plus `JFI:` lines saying what's left to fill in, in the format's
      comment syntax (`#` for TOML/YAML/Dockerfile/`.env`, `<!-- -->` for
      HTML, `--` for SQL).
  - **One node per file,** like source files, with a `kind`:
    - `code`: functions with stubs (as before);
    - `artifact`: everything else.

    Task turns an `artifact` file node into fill-in leaves, e.g. `fill
    DATABASE_URL and API_KEY in .env.example`, `add the test-DB fixture to
    tests/conftest.py`. A small artifact Laya judges GOOD goes straight to
    Dev.
  - **Verification instead of a unit test:** an `artifact` leaf's
    `done_when` is a **mechanical check**, run by Dev like a unit test:
    - `uv sync` exits 0;
    - the file parses (TOML/YAML/JSON);
    - `alembic upgrade head` runs;
    - `docker build .` succeeds;
    - the template renders;
    - pytest collects the fixture.

    D9 ("every implementation has a test attached") holds: the check *is*
    the attached test.
  - **Dependencies and setup order:** the manifest is the first node
    under its component (`depends_on` from every code file that imports
    from it). Its leaf's check is the runbook's `setup` entry, so Dev
    verifies `setup` as soon as the dependencies exist, before any code
    that needs them.
  - **Cross-component artifacts** (the one project-wide `pyproject.toml`,
    the root README, a Dockerfile for the whole app): Architect lists them
    as a **`project` component**, so exactly one Lead owns them. That
    prevents two Leads each scaffolding their own manifest.

**G3. Changes to existing code (brownfield) have no path.** Stubs work for
new functions. They don't cover modifying, renaming or deleting an
existing function, fixing a bug, or refactoring. Those are most real goals
on an existing repo. `scaffold_file` only appends new stubs.

Example goal on an existing FastAPI app: *"Todos should have a due date,
and `GET /todos` should return overdue ones first."* The work is: change
the existing `Todo` model, change the existing `list_todos()`, add a new
`is_overdue()`, delete the now-unused `sort_by_created()`, and update
`list_todos()`'s existing test. Only the new function has a path today.

- **Fix (agreed): Lead marks existing code the way stubs mark new code.**
  - **Lead marks it** with a new tool, `mark_change(file, symbol, kind,
    what)`. It puts a marker comment directly above the existing symbol
    and never touches its body:

    ```python
    # JFI-CHANGE: sort overdue todos first (due_date < today), then by created_at
    def list_todos(session: Session) -> list[Todo]:
        ...existing code, untouched...
    ```

    `kind` is `change` (`JFI-CHANGE:`) or `delete` (`JFI-DELETE: <why>`).
    Like `scaffold_file`, it refuses paths outside the project and any
    symbol it can't find.
  - **The file node** covers everything in that file: new stubs, change
    markers and delete markers.
  - **Task reads all three markers** and creates one leaf per marker, with
    a leaf `kind`:
    - `implement`: a `JFI:` stub (as before);
    - `modify`: a `JFI-CHANGE:` marker;
    - `delete`: a `JFI-DELETE:` marker.
  - **Dev handles each kind:**
    - **`implement`:** replace the stub, add one unit test (as before).
    - **`modify`:** change the symbol and remove its `JFI-CHANGE:` line.
      Run the symbol's **existing** tests, updating them only where the
      intended behaviour changes, and add **one** new test for the new
      behaviour (the leaf's `done_when`).
    - **`delete`:** `search_code` for callers first. If anything still
      calls it, don't delete: add a reviewer note and leave the leaf for
      the reviewer. Otherwise delete the symbol and its tests.
  - **Bug fixes** are `modify` leaves whose `done_when` is the failing
    case, e.g. "`POST /login` with an empty password returns 422, not 500".
    That case becomes the new unit test.
  - **Nothing is forgotten silently:** the end-of-imp scan looks for all
    three markers (`JFI:`, `JFI-CHANGE:`, `JFI-DELETE:`). Any left over is
    unfinished work.

**G4. Non-code goals.** `task_rules.py` detects `story` (and prose-heavy
data-eng) goals today. Architect/Lead/Task, stubs and unit tests make no
sense for "write a short story".
- **Fix (agreed): a document path.** When
  `AdaptiveSessionManager.task_type()` is `story` (or another prose type),
  the same loop, stages and Laya judging run with document-shaped roles:

  | Code path | Document path |
  |---|---|
  | Architect: stack, components, contracts | Architect: premise, audience, tone, length, **outline** (the sections), recorded as design entries; `project` artifacts become the output file(s) |
  | Lead: files + stubs per component | Lead: one **section** per node, creating the output file with a heading and a `JFI:` placeholder per section (`scaffold_file` in Markdown: `<!-- JFI: ... -->`) |
  | Task: implement / modify / delete leaves | Task: one leaf per **passage** (a scene, a paragraph group) |
  | Dev: one function + one unit test | Dev: writes one passage in place of its placeholder |
  | unit test | a **mechanical check** in `done_when`: word count within range, required points/names present, the placeholder gone |
  | reviewer e2e: run the app | reviewer e2e: read the whole document once against the outline, tone and length, then report |

  - **No unit tests, no stubs of code, no runbook** beyond "where the
    output file lives".
  - **Laya's questions are unchanged.** "Smallest sensible unit" now means
    one passage.
  - **Mixed goals** (e.g. a data pipeline plus a written report) stay on
    the code path. The report is a `project` artifact.

**G5. What ends an episode, and what ends a phase.** Today phases end on
`<PHASE>_COMPLETE` marker lines the **model** writes, with "please
continue… output 'X'" nudges. The plan never says what ends a Lead, Task,
Architect or Dev episode.

- **Fix (agreed): a phase is complete when all of its leaves are in a done
  state, never because the model said so.** Every leaf carries a state for
  each phase that works on leaves, and the runner checks the DB:

  | Phase | Per-leaf state | Done state | Phase complete when |
  |---|---|---|---|
  | planner | `plan_status` | `GOOD` (set by Laya or its fallback) | **every** node is `GOOD` |
  | imp (Dev) | `status` | `done` (via `mark_leaf_done`, after its test/check passed) | **every** leaf is `done` |
  | reviewer | `review_status` (reused column) | `passed` | **every** leaf is `passed` (the e2e passed; see below) |
  | cleanup | — (not leaf-based) | — | its one episode calls `finish` |

  - **"All the leaves must have a done state":** a leaf left in any
    non-done state keeps its phase open. There's no "mostly done".
  - **Reviewer per-leaf state.** When the e2e passes, the runner marks
    every leaf `passed`. When it fails, the reviewer names the failing
    file/symbol, the leaves that own it become `failed` and are
    **re-opened** for Dev (G8), and the phase isn't complete. After the fix
    and a re-run of the e2e, `passed` is written again.
  - **Leaves the user skipped (Ctrl+K/Q):** `skipped` is a terminal state,
    but **not** `done`. See §11 for whether a skipped leaf still lets the
    phase complete.
  - **Markers become derived.** `PLANNER_COMPLETE`, `IMP_COMPLETE`,
    `REVIEWER_COMPLETE` are appended by the **runner** when the DB says the
    phase is complete, never taken from model text. They stay only so
    `get_remaining_phases` and old sessions keep working. A model writing a
    marker line no longer ends anything.
  - **Resume** reads the same per-leaf states: the current phase is the
    first one whose leaves aren't all done. That also fixes today's
    "resume in iteration 2+" problem ("Known gaps" in `docs/pipeline.md`), where old
    markers from iteration 1 made every phase look complete.
- **What ends an episode** (inside a phase):
  - Dev: `mark_leaf_done(id)`, which is refused unless the leaf's test or
    check has passed in this episode.
  - Architect / Lead / Task: `finish(node_id, summary)`.
  - Reviewer: `write_review_report` (fail) or `finish` (pass).
  - Cleanup: `finish`.
  - The runner also enforces `MAX_EPISODE_TURNS` (e.g. 15). An episode
    that hasn't finished by then is stopped and handled like a budget
    overflow (§5.3). The no-tool-call nudge is reworded to "call
    `finish`".

**G6. Dev ordering across files will make unit tests fail.** Task sees only
one file, so it can't set `depends_on` to leaves in *other* files. Dev
could then implement `list_todos()` in `api/` before `get_session()` in
`db/`, and `list_todos`'s unit test would hit `NotImplementedError`.
- **Fix (agreed):**
  - **Dev's queue is a topological order over the whole tree:** a leaf
    waits for every leaf under any node its ancestors `depends_on`
    (component → component set by Architect, file → file by Lead, leaf →
    leaf by Task).
  - Plus a mechanical check: if a leaf's test fails with the stub
    body's `NotImplementedError` from another symbol, that's a *missing
    dependency*, not a bug. The leaf is re-queued after the named symbol's
    leaf instead of being "fixed".
  - `depends_on` is also validated as **acyclic**.

**G7. Mid-run user input goes nowhere.** Today the user can **queue** a
request, **force** a directive (added to history, `drain_forced_input`),
**skip** a task (Ctrl+K/Q) and **pause** (Ctrl+P), all through the one
shared history. With scoped episodes, a forced directive written to
session history never reaches the next episode's brief.
- **Fix (agreed):**
  - **Forced directives** during planning go to the *next* episode's brief
    (and are stored on the node being worked on). During Dev they're
    attached to the current leaf's next episode.
  - **Queued requests** are held until the run reaches idle, as today, then
    go to Architect extend mode.
  - **Skip** marks the current Dev leaf skipped and moves on.
  - **Pause** waits between episodes.

### B. Serious: works, but badly or inconsistently

**G8. Review failures are routed through the heaviest path.** A failed e2e
is usually a bug in one function. The plan sends it to Architect extend
mode, and "done leaves are never rewritten". So fixing a one-line bug
means Architect → Laya → Lead → Laya → Task → Laya → Dev, and can't touch
the leaf that has the bug.
- **Fix (agreed):** a **fix path.** The reviewer's report names the failing
  file/symbol. Architect (who sees everything) either:
  - **re-opens the done leaf(s)** that own it: Dev status → todo, with the
    report attached as a `fix_note`, which skips planning entirely; or
  - uses extend mode only for genuinely missing work.

  Re-opening a done leaf becomes an explicit, logged operation.

**G9. Small goals.** "Fix the typo in README" → Architect makes one
component node → Laya says GOOD. The plan then has a GOOD **component**
leaf with no files, no stub and no unit test going to Dev. Rules like
"every leaf has a unit test" and "Dev replaces a stub" don't fit.
- **Fix (agreed):** a GOOD leaf at any level is a valid Dev leaf. Dev's
  prompt handles "no stub" (edit in place) and "no unit test possible"
  (`done_when` is a mechanical check, as in G2).

**G10. Duplicates are effectively undetectable.** Laya judges one node at a
time and never sees siblings (token budget), so its `duplicate` reason has
nothing to compare against. That leaves only the role's self-check.
- **Fix (agreed): the duplicate check lives in the Laya judge step.** It's a
  **mechanical sibling-similarity check**, using the Laya model that's
  already loaded. `laya.embed_fn_from_agent`
  mean-pools its own encoder, so embedding every sibling's description and
  flagging pairs above a cosine threshold costs no extra model. A flagged
  pair makes the later-created node `REDO` (reason `duplicate`).

**G11. Tests: stub test files, and failing stubs.**
- A Lead-created *test* file with `raise NotImplementedError` stubs would
  make the whole suite fail or error until every test is written.
- **Fix (agreed):**
  - test files are scaffolded with imports only, no test stubs;
  - Dev's `test_one` run selects only its own test;
  - `test_one` must support selecting one test in each stack's framework
    (pytest node ids, `vitest -t`, `go test -run`), which is a runbook
    responsibility Architect sets per stack.

**G12. The reviewer and cleanup break the 20k rule; what the e2e is.** §0 says every LLM
call fits in 20k; §0 also says history compression "stays for the
reviewer and cleanup", i.e. long conversations.
- **Fix (agreed):** the reviewer runs the e2e as one scoped episode (brief
  = the `e2e` entry + the runbook index + reviewer notes), and cleanup as
  one scoped episode. Compression is then unused by the new pipeline; it
  stays only for old-pipeline sessions (G16).
- **The e2e can be as simple as one command.** The runbook's `e2e` entry
  is whatever exercises the whole app end to end for this stack:
  - often just the full test suite: `uv run pytest`, `npm run test`,
    `go test ./...`;
  - a scripted scenario when the goal needs it (start, request, check,
    stop).

  Architect picks it (§4.3). The reviewer runs it, and falls back to
  step-by-step only when it's a scenario. A one-command e2e keeps the
  reviewer episode tiny.

**G13. Resuming a Dev leaf mid-episode.** *(Decided: restart fresh, as
below.)* Dev episodes aren't resumable
conversations, and a crash mid-leaf leaves partial edits on disk.
- **Fix (agreed):** a leaf that was started but not finished restarts as a
  fresh episode. Its brief says "a previous attempt may have partially
  edited `<files>`: check the current state first". (Its `JFI:` marker
  tells it whether the stub was already replaced.)
  - Each restart increments the leaf's `attempt_count`. At 3 attempts the
    leaf is treated like a budget overflow: split by Task (§5.3), instead
    of retrying the same thing.

**G14. Token counting accuracy.** JFI estimates tokens as characters/4,
which can be 20–30% off for code. The budget is a hard stop, so a bad
estimate either stops good episodes early or lets real ones overflow the
model's window.
- **Fix (agreed):** use the server-reported `usage` when available. The
  console already reads `chunk.usage` when a server sends it
  (`pt_console_manager.print_agent_response`), but
  `openai_compatable_stream.py` never requests it: it needs
  `stream_options={"include_usage": True}`. Use chars/4 only as the
  fallback. Keep `EPISODE_TOKEN_BUDGET` ≤ 0.7 × window as the margin for
  estimate error.

**G15. Per-role models.** `PHASE_ENV_PREFIX` gives per-phase models
(`PLANNER_MODEL`, `IMP_MODEL`). Architect (design) likely wants a stronger
model than Task (mechanical ticketing).
- **Fix (agreed):** role prefixes `ARCHITECT_*`, `LEAD_*`, `TASK_*` via
  the existing `phase_env`, falling back to `PLANNER_*`, then the shared
  default.

**G16. Sessions in flight when the upgrade lands.** An existing session
has leaves with no `level` / `plan_status`, `testing` leaves, a history
positioned mid-`testing` or mid-`product_owner`, and review columns in
use.
- **Fix (agreed): option (a).**
  - **(a)** old sessions keep running on the old pipeline until they
    finish (both code paths kept for one release);
  - **(b)** migrate: existing un-done leaves become GOOD `task` leaves,
    `testing` leaves become Dev leaves, and the session resumes at Dev.
    Old completed work stays done.

  **Decided: (a).** Each session records which pipeline it runs on
  (`SessionRecord.pipeline_version`, §13):
  - sessions created before the upgrade stay on `v1` and keep the old
    code path until they finish;
  - new sessions start on `v2`.
  - The old path is removed in a later release, once no `v1` sessions are
    expected.

### C. Smaller: needs a line in the plan

**G17. `read_symbol` / `replace_symbol` for non-Python languages.**
*(Agreed: tree-sitter where available, marker-bounded fallback otherwise;
the dependency choice is confirmed at implementation.)* "A
brace/indent scan" is fragile for TS/JS/Go (nested braces in strings,
decorators, overloads). **Fix (agreed):** tree-sitter if acceptable as a
dependency; otherwise the fallback edits only between the stub's own
`JFI:` markers (always well-delimited, since the tool generated them),
with `read_file` ranges for everything else.

**G18. `scaffold_file` path safety.** It writes to disk from model input.
It must refuse paths outside the project root and anything under
`.jfi/` / `.git/`.

**G19. What the reviewer's e2e means for libraries and CLIs.** "Start the
app, stop the app" doesn't apply to a library or a one-shot CLI. Proposed
fix: for those, Architect writes `e2e` as "call the public API / run the
CLI with sample input and check the output"; `run` / `stop` are optional
runbook entries.

**G20. Laya and non-English goals.** *(Superseded, 2026-09-28: JFI uses only
`english` and `typed-decisions`; `multilingual` is dropped.)* Goals are
expected in English. A non-English goal is still judged, just less
reliably, and the §5.2 fallback covers low-confidence answers as it does
for everything else.

**G21. UI and reporting surfaces.** Things that read today's plan and
phase model and need updating:
- the TUI header / `set_status` fields;
- `web/dashboard.py`: it shows the review report and plan feedback;
- `socket_reporter` and the fleet's `plan_markdown` parser;
- `export-db` (new tables, removed note kinds);
- `PHASE_DISPLAY_NAMES`.

The fleet dashboard parses `render_plan_markdown`'s `- [ ] N.M` format. The
new `plan_status` / `level` should show there without breaking that parser.

**G22. Test churn.** 12 test files mention the `testing` / `product_owner`
phases, and many more string-match today's prompts
(`test_tiered_planner.py`, `test_get_system_message.py`,
`test_phase_messages.py`, …). The rewrite deletes or rewrites these.
That's expected, but it should be planned as its own step so the suite is
green at every commit, not broken for the length of the rewrite.

**G23. Implementation order.** *(Agreed.)* The staged plan lives in its
own document, **[laya_impl_phases.md](laya_impl_phases.md)**: 13 phases
(0–12), `v1` and `v2` side by side behind `pipeline_version`, each phase
shipped with the whole suite green. Its **Progress** section is the
source of truth for what's done and what's next.

## 13. Database changes

Everything the design above needs, in one place. The DB stays `.jfi/JFI.db`
(or `DB_BACKEND=mysql|postgres`), one per project, every row keyed by
`session_id`.

### 13.1 Changed tables

**`Leaf`: the plan tree** (existing table, new columns):

| Column | Type | Values / meaning | Used by |
|---|---|---|---|
| `level` | str, indexed | `architect` / `lead` / `task`: which role created it | routing (§2) |
| `kind` | str | component: `component` / `project`; file: `code` / `artifact` / `section`; leaf: `implement` / `modify` / `delete` / `fill` / `passage` | Task, Dev (G2, G3, G4) |
| `plan_status` | str, indexed, null | `NULL` (unjudged) / `GOOD` / `BREAKDOWN` / `REDO` | planner completion (G5) |
| `redo_count` | int, default 0 | redos so far | redo cap (§4.7) |
| `redo_reason` | str, null | `operational` / `vague` / `duplicate` / `design` / `too_big` | redo brief |
| `escalation_count` | int, default 0 | escalations into this node | escalation cap (§4.7) |
| `paused` | bool, default false | subtree paused by an escalation | loop (§4.7) |
| `done_when` | text, null | finish condition; the test/check | Dev, reviewer |
| `files` | JSON list, null | files it creates/changes, plus its test file | Dev brief, scope anchor |
| `depends_on` | JSON list of leaf ids, null | must be done first; validated acyclic | Dev queue order (G6) |
| `tools` | JSON list, null | optional tools Laya picked | Dev episode tool set (G1.3) |
| `attempt_count` | int, default 0 | Dev episodes started on this leaf | restart cap (G13) |
| `fix_note` | text, null | reviewer failure text when the leaf is re-opened | Dev brief (G8) |
| `reopened_count` | int, default 0 | times re-opened by a failed review | reporting |

Existing columns, and how their meaning changes:

| Column | Change |
|---|---|
| `status` (`todo` / `done` / `skipped`) | Unchanged: Dev's per-leaf state. `done` is imp's done state (G5). |
| `review_status` | **Reused** for the reviewer's per-leaf state: `passed` / `failed`. The legacy values `approved` / `rejected` stay readable for `v1` sessions. |
| `review_note`, `rejection_count` | `v1` only; not written by `v2`. |
| `phase` | `v2` writes only `imp`. `testing` / `product_owner` stay valid enum values for `v1` data. |

**`SessionRecord`** (existing table, new columns):

| Column | Type | Meaning |
|---|---|---|
| `pipeline_version` | str, default `v1` | `v1` = old pipeline, `v2` = this design (G16). New sessions write `v2`. |
| `stage` | *(existing)* | now `architect` / `lead` / `task` / `judge` / `dev` / `review` |

**`HistoryMessage`** (existing table, new column):

| Column | Type | Meaning |
|---|---|---|
| `episode_id` | int, null, indexed | which episode a message belongs to. `NULL` for `v1` sessions. An episode's messages are rebuilt from exactly these rows. |

**`UnlockedTool`**: `v1` only. In `v2`, a `load_tool` lasts one episode
and is logged on `Episode.tools_loaded`, not here.

**`DonePhase`**: now written by the runner **from leaf state** (G5): when
every leaf is in the phase's done state. It's never written because the
model said a marker.

### 13.2 New tables

**`Episode`**: one row per LLM episode (planner roles, each Dev leaf, the
reviewer, cleanup):

| Column | Type | Meaning |
|---|---|---|
| `id` | int PK | |
| `session_id` | str, indexed | |
| `node_id` | int, null | the leaf/node it works on (null for cleanup) |
| `role` | str | `architect` / `lead` / `task` / `dev` / `reviewer` / `cleanup` |
| `mode` | str | `create` / `breakdown` / `redo` / `extend` / `implement` / `fix` / `e2e` |
| `started_at`, `ended_at` | datetime | |
| `turns` | int | model turns used |
| `tokens` | int | tokens counted (server `usage` if reported, else estimate: G14) |
| `end_reason` | str | `finish` / `done` / `budget` / `turn_cap` / `error` / `stopped` |
| `tools_loaded` | JSON list | optional tools loaded via `load_tool` this episode |
| `tools_used` | JSON list | tools actually called |

It drives the budget and turn caps, the G13 restart, the Laya tool-pick
accuracy (`tools` on the leaf vs `tools_used` / `tools_loaded`), and the
UI's "what's running now".

**`PlannerVerdict`**: one row per judged node:

| Column | Type | Meaning |
|---|---|---|
| `id` | int PK | |
| `session_id`, `node_id` | | |
| `level` | str | |
| `judged_at` | datetime | |
| `laya_model` | str, null | checkpoint used (null when unavailable) |
| `laya_verdict`, `laya_redo_reason` | str, null | Laya's raw answer |
| `probabilities` | JSON | per-option probabilities for both questions |
| `answer_confidence` | float, null | |
| `fallback` | str | `none` / `conservative` / `llm` / `unavailable` |
| `duplicate_of` | int, null | sibling it was flagged against (G10) |
| `duplicate_score` | float, null | cosine similarity |
| `budget_override` | bool | status forced by the token check (§5.3) |
| `final_status` | str | the `plan_status` actually written |

**`PlanEvent`**: an append-only log of plan state changes that aren't
verdicts:

| Column | Type | Meaning |
|---|---|---|
| `id`, `session_id`, `node_id`, `created_at` | | |
| `type` | str | `redo` / `escalate` / `breakdown` / `reopen` / `overflow` / `restart` / `skip` / `cap_reached` / `directive` |
| `detail` | text | reason, the escalation `why`, the reviewer failure, … |
| `episode_id` | int, null | the episode that caused it |

This is the audit trail behind "why is this leaf here". It feeds the
escalation-rate signal (§4.7) and `export-db`.

**`RunbookEntry`** (§3.1):

| Column | Type | Meaning |
|---|---|---|
| `id`, `session_id` | | unique on (`session_id`, `name`) |
| `name` | str | `setup` / `run` / `stop` / `view` / `test` / `test_one` / `build` / `logs` / `e2e` / … |
| `command` | text | may contain `{test_id}`-style placeholders |
| `notes` | text | |
| `verified` | bool | |
| `verified_at` | datetime, null | |
| `updated_by` | str | role that last wrote it |
| `updated_at` | datetime | |

**`DesignEntry`** (§3.2):

| Column | Type | Meaning |
|---|---|---|
| `id`, `session_id` | | unique on (`session_id`, `kind`, `key`) |
| `kind` | str | `stack` / `component` / `contract` / `convention` / `assumption` / `out_of_scope` / `outline` (G4) |
| `key` | str | e.g. `persistence`, `api->persistence` |
| `text` | text | |
| `created_by` | str | role |
| `updated_at` | datetime | |

**`Directive`** (G7): a user directive forced mid-run:

| Column | Type | Meaning |
|---|---|---|
| `id`, `session_id`, `created_at` | | |
| `node_id` | int, null | node it applies to (the one in progress when it was typed); null = next episode of any kind |
| `text` | text | |
| `consumed_episode_id` | int, null | episode that received it; null = still pending |

### 13.3 `SessionNote` kinds

| Kind | `v2` |
|---|---|
| `review_report` | kept: the reviewer's failure report (G8 uses it to re-open leaves) |
| `reviewer_notes` | kept: Dev's notes for the reviewer |
| `plan_feedback` | `v1` only (Program Manager is gone) |

### 13.4 Getting the schema onto existing DBs

- **New tables:** `create_all` creates them, as today.
- **New columns on existing tables (SQLite):** added to `_ensure_columns`
  in `models/db.py` (`Leaf`, `SessionRecord`, `HistoryMessage`), as
  `AGENTS.md` requires.
- **MySQL / Postgres:** `_ensure_columns` is SQLite-only today, so a
  project on those backends with an existing DB would **not** get the new
  columns. Fix: extend `_ensure_columns` with the `ALTER TABLE … ADD
  COLUMN` syntax for both (all new columns are nullable or have defaults,
  so it's a plain add), and test it against both dialects' DDL.
- **No data migration.** `v1` sessions keep their rows as they are
  (G16a). `v2` sessions write the new columns from the start.
- **`export-db`:** dumps every new table (`Episode`, `PlannerVerdict`,
  `PlanEvent`, `RunbookEntry`, `DesignEntry`, `Directive`), and the plan
  render shows `level`, `kind`, `plan_status`, `done_when` and
  `depends_on`.
- **Web dashboard / fleet** (G21): read `pipeline_version` to know which
  layout to show; `render_plan_markdown` keeps its `- [ ] N.M` line format
  so the fleet parser doesn't break, with the new fields appended as a
  suffix.

### 13.5 DB tests

- **Fresh DB:** every new table and column exists after `get_engine`.
- **Old SQLite DB** (a fixture built with today's schema): gains every new
  column and table on open; old rows read back unchanged.
- **MySQL/Postgres DDL:** the generated `ALTER TABLE` statements are
  correct per dialect (checked on the SQL, no server needed).
- **Constraints:** uniqueness on `RunbookEntry` (`session_id`, `name`) and
  `DesignEntry` (`session_id`, `kind`, `key`); `depends_on` cycles are
  rejected on write.
- **`v1` session:** still loads and resumes on the old path with the new
  columns present but unused.
- **`export-db`:** includes the new tables.
