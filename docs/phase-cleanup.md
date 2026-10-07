# Phase: `cleanup`

Tidies the working directory after the review. Code: `run_cleanup` in
`src/JFI/review/`, the prompt in `src/JFI/review/prompts.py`;
`runner._run_cleanup` runs it.

One scoped episode (`role="cleanup"`, model `CLEANUP_*` → shared) with
`execute_command`, `list_dir` and `finish`. It:

- **never** deletes, moves or overwrites anything under `.jfi/` (the project's
  database, shared by every session, and the ground-truth evidence in
  `.jfi/evidence/`);
- keeps every file a plan node created: scaffolded source, tests, configs and docs
  are the deliverable;
- moves anything worth keeping for reference (a screenshot, a debug script) into
  `.jfi/`, deletes pure noise, and leaves anything it's unsure about;
- never touches `.git`, dependency folders or tracked files.

`CLEANUP_COMPLETE` is appended when the episode ends. A cleanup that doesn't
finish is reported but doesn't stop the run.
