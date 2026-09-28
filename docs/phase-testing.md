# Phase: `testing`

Works through the pending `testing` leaves. It runs every check for real
and fixes the implementation until each check passes.

- **Code:** the generic `runner.run_phase` → `_drive_turn_loop`. The
  completion marker is `TESTING_COMPLETE`.
- **Prompt:** `get_system_message("testing")` plus the same inline work
  queue as `imp`, filtered to `testing` leaves. It shares the
  `VERIFICATION_RULES` block with every phase.
- **Trigger:** "Implementation is done. Work through the pending testing
  leaves one at a time…"

## The per-leaf loop the prompt asks for

1. **Pick the first leaf in the queue.**
2. **`start_leaf(id)`,** then announce it as `**[CURRENT TEST: 2.1]**`.
3. **Run the check with `execute_command`.** Only read files instead when
   there's truly nothing to execute (prose, for example).
4. **On failure,** fix the implementation with the file tools and re-run
   until it passes. Fixes happen here; they don't go back to `imp`.
5. **`mark_leaf_done(id)` immediately.**
6. **Repeat** until nothing is pending, then output `TESTING_COMPLETE`.

## `VERIFICATION_RULES`, in short

These rules matter most in this phase:

- **What counts as verified:** a mechanical check that actually ran. Code
  reading counts only when no check is possible.
- **Real entry points:** exercise the CLI main loop, a real HTTP request,
  the public API. Unit-testing helpers alone doesn't verify the goal.
- **Long-running processes:** use `start_background_process` /
  `stop_background_process` (handle-based), never `&` plus
  `pkill`-by-name. For a GUI, call `capture_screenshot` and then
  `view_image` as separate tool calls.
- **Order:** run cheap, deterministic checks (build/typecheck) first and
  fragile ones (browser, network, new system tooling) last.
- **Unavailable tooling isn't a blocker.** Try once, note it, and fall back
  to whatever checks can run.

Testing leaves come from the planner. Dev and Task Planner are told to
include at least one command-named mechanical check.
