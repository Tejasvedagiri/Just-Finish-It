# Phase: `cleanup`

Tidies the working directory after each pass. It never changes the
deliverable's logic or content.

- **Code:** the generic `runner.run_phase`. The completion marker is
  `CLEANUP_COMPLETE`. It's the last phase before `collect_next_iteration`.
- **Prompt:** `get_system_message("cleanup")`. `session_dir` is the parent
  of `plan_path`, which is `.jfi`.
- **Trigger:** "Review has landed for this pass. Scan the working directory
  (excluding .git and .jfi)…"

## What the prompt asks for

1. **Never delete, move or overwrite anything in `.jfi/`.** It holds the
   shared `JFI.db` (every session's plan, history and notes for this
   project), `.lock` and `llm_debug.jsonl`. The prompt stresses it because
   an unexplained hidden folder looks like clutter in a scan.
2. **Scan the rest** with `find` / `ls -la` / `git status --short` for
   stray files: debug scripts, screenshots, scratch notes, leftover
   scaffolding, logs.
3. **Worth keeping:** `mv` it into `.jfi/`. **Pure noise:** `rm` it.
4. **Never touch** `.git`, `.jfi/`, tracked source/tests/docs, dependency
   directories, or anything uncertain.
5. End with `CLEANUP_COMPLETE`, even if nothing needed doing.

Cleanup runs **after** the reviewer. It's the only phase that deletes files
on purpose, so any change to its prompt should keep the `.jfi/` protection
first and explicit.
