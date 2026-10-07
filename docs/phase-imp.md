# Phase: imp

Code: `src/JFI/imp/` (`dev.py` the phase, `queue.py` the work queue, `prompts.py`
the Dev prompts). `runner._run_imp` hands the phase to `Imp.run()`.

One fresh, short **Dev episode** per GOOD leaf, never one long conversation:

```
setup      the runbook's `setup` runs once; if it fails, one Dev episode fixes it
the queue  next_leaf(), one episode each
finish-up  `build` once, plus the JFI: marker scan; one Dev episode for what's left,
           the rest goes to the reviewer notes
```

`IMP_COMPLETE` is written from DB state (every node finished), never from model
text.

## The queue (`queue.py`)

Every node the planner settled as GOOD -- leaves and the parents above them --
bottom-up (`post_order`): 1.1.1, 1.1.2, 1.1.3, then 1.1, then 1. Only imp works
bottom-up; planning stays top-down. A node waits for everything under it, its
own `depends_on` (Task orders leaves inside one file) and the `depends_on` of
every ancestor (Lead orders files, Architect orders components), so
`list_todos()` is never built before the `get_session()` it calls.

A **parent's turn** (`VERIFY` prompt, episode mode `check`) is Dev checking the
part as a whole once its sub-tasks are done: its `done_when`, the tests of its
files, its cases' evidence; it fixes what doesn't fit together. Its gate is the
same `mark_leaf_done`. A parent that runs out of attempts is skipped with a
reviewer note (it is already split). A node reopened later (by the reviewer, or
because its evidence changed), or a new part added under a finished parent,
sends every finished parent above it back to the queue (`reopen_with_ancestors`).

## One episode

Dev gets the leaf's scope (description, `done_when`, files, and the node's
`notes` and `references` -- design entries named there are inlined), the runbook
and design indexes, and a role prompt by kind (implement, integrate, modify,
delete, fill, generic). Its tools include `read_symbol` / `replace_symbol`,
`apply_patch`, `find_references`, `copy_lines`, `outline_file`,
`execute_command` and `add_reviewer_note`.

The rules: build only what SCOPE names (on the calc run a "CalcError" leaf also
wrote the next leaf's `evaluate()`); run the leaf's test while working with the
runbook's `test_one` through `execute_command`, so it iterates on exactly the
gate's command; scratch code goes in a file under `.jfi/scratch/` run with the
runbook's `script` entry, never inline `python -c` / `node -e` / heredocs (the
QA machine's stui run had dozens, each quoting differently per shell). A UI
leaf may `load_tool("browser")` after its test passes, to click through what it
built and screenshot it.

The episode ends with **`mark_leaf_done`**, which is gated: it runs the leaf's own
unit test (the runbook's `test_one`) and refuses until it passes. `test_id` is
only the id the template expects: a whole command or a path (QA run: `npx vitest
run __tests__/loader.test.js`, which the gate turned into a garbled command) is
refused with the template and its example id.

Two turns before the turn cap the episode is told so (`TURNS_LEFT_WARNING` in
`episode/engine.py`), the way the token budget already warned.

**Evidence** ([`old_new.md`](old_new.md)): a node with `cases` (any level,
under a ground truth) is compared with them by the same gate, after its test or
check passes -- the new code on the evidence's inputs (the case's own `compare`
command, or the runbook's `compare_one`), or the new app screenshotted in the
case's state. It is done only when every case matches; with cases, the
comparison alone is enough proof. Each comparison is kept as that node's own
evidence (`<number>_<case>.result.txt`, `.new.png`, `.compare.png`). A
difference that is intended, or belongs to a later task (a part of the page not
built yet), can be accepted with `accept_difference="<why>"`: it goes to the
reviewer notes, and the reviewer compares every case again over the finished
build. Evidence that changes during the episode is refused (the evidence is the
ground truth; Dev fixes the code). A node whose evidence is later edited or
re-captured is re-queued with its parents (`_requeue_changed_evidence`, by the
`evidence_hash` it matched). Sessions planned before this have `compare`
leaves; they keep their own prompt (`COMPARE`) and are skipped, never split,
when they run out of attempts.

With `EVIDENCE_REVIEW=1` (off by default) imp first waits for a person to
accept the evidence (`runner._evidence_review`), and asks again only when it
changed since.

## Git checkpoints (`tool/checkpoint_tools.py`)

When `mark_leaf_done` passes, the project is committed to a private repository,
`.jfi/checkpoints.git`, with the project as its work tree, and the commit's sha
is stored on the leaf (`Leaf.checkpoint`). `Imp.run` first takes a baseline, so
the first leaf's diff is only its own. The project's own git is never touched
(its index, branch and refs stay as they were), a project without git gets
checkpoints too, `.gitignore` still applies and `.jfi/` plus dependency folders
are excluded. Without `git` installed, checkpoints are off. The reviewer reads
them with `leaf_diff`.

## Recovery

- The gate's test hit **another** stub's `NotImplementedError`: a missing
  dependency, not a bug. The leaf is re-queued after the leaf that owns that
  symbol (up to `MAX_DEFERRALS`).
- An episode that ended without finishing (a crash, stop or restart) starts over
  fresh, told a previous attempt may have partly edited its files. At
  `MAX_DEV_ATTEMPTS` (3) it's treated as an overflow.
- **Overflow** (the budget or turn cap ran out): first one short wrap-up
  episode (`WRAP_UP_TURNS` = 3) that runs the test and calls `mark_leaf_done`
  if the work is done -- on the calc run a leaf whose test had passed was
  re-split after its episode hit the turn cap, ~7 minutes lost. Otherwise the
  leaf was too big. It goes back to Task for a split, and the planner settles the pieces. A leaf the
  split can't break up is skipped with a reviewer note, so every leaf reaches a
  finished state.

## Controls

Ctrl+P pauses between turns. Forced input (`!text`) goes into the episode that's
running, before its next LLM call, as a `USER INTERJECTION`, and is recorded as a
`Directive` row delivered to that episode (`JFI.episode.engine`). There is no
per-leaf skip key (v1's Ctrl+K / Ctrl+Q were removed with v1).
