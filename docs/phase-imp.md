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

`IMP_COMPLETE` is written from DB state (every leaf finished), never from model
text.

## The queue (`queue.py`)

Every real leaf (no children) the planner settled as GOOD, in tree order. A leaf
waits for its own `depends_on` (Task orders leaves inside one file) and for the
`depends_on` of every ancestor (Lead orders files, Architect orders components),
so `list_todos()` is never built before the `get_session()` it calls.

## One episode

Dev gets the leaf's scope (description, `done_when`, files), the runbook and
design indexes, and a role prompt by kind (implement, integrate, modify, delete,
fill, generic). Its tools include `read_symbol` / `replace_symbol`, `copy_lines`,
`outline_file`, `execute_command` and `add_reviewer_note`.

The episode ends with **`mark_leaf_done`**, which is gated: it runs the leaf's own
unit test (the runbook's `test_one`) and refuses until it passes.

## Recovery

- The gate's test hit **another** stub's `NotImplementedError`: a missing
  dependency, not a bug. The leaf is re-queued after the leaf that owns that
  symbol (up to `MAX_DEFERRALS`).
- An episode that ended without finishing (a crash, stop or restart) starts over
  fresh, told a previous attempt may have partly edited its files. At
  `MAX_DEV_ATTEMPTS` (3) it's treated as an overflow.
- **Overflow** (the budget or turn cap ran out) means the leaf was too big. It
  goes back to Task for a split, and the planner settles the pieces. A leaf the
  split can't break up is skipped with a reviewer note, so every leaf reaches a
  finished state.

## Controls

Ctrl+P pauses between turns. Forced input (`!text`) goes into the episode that's
running, before its next LLM call, as a `USER INTERJECTION`, and is recorded as a
`Directive` row delivered to that episode (`JFI.episode.engine`). There is no
per-leaf skip key (v1's Ctrl+K / Ctrl+Q were removed with v1).
