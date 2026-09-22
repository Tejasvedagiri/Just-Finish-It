---
name: run-jfi
description: "Drive JFI (Just-Finish-It), an autonomous multi-phase coding agent CLI, to implement real code changes in a target repo instead of editing files directly. Use whenever the user says 'using JFI', 'use JFI', or asks to delegate an implementation task to JFI. Covers: launching/resuming JFI sessions via tmux (it needs a real TTY), writing goals that get well-decomposed plans, monitoring the planner/imp/testing/reviewer/cleanup pipeline, handling review-fail loops and LLM-request-failed prompts, correcting a session mid-run with forced directives, recovering from crashes, and — critically — never trusting a session's own 'PIPELINE COMPLETE' claim without independent verification."
---

# Running JFI (Just-Finish-It)

JFI is a full-screen `prompt_toolkit` TUI agent that plans and implements a
coding goal through five phases: **planner → imp (implement) → testing →
reviewer → cleanup**. It requires a real TTY, so it must run inside `tmux`,
never as a plain piped/backgrounded process.

You (Claude) are the *supervisor* of JFI sessions: you don't write the
target repo's code yourself when the user says "use JFI" — you research the
ground truth, write a precise goal, launch/monitor JFI, intervene when it
goes off track, and independently re-verify its "done" claims. Treat JFI
like a capable but occasionally overconfident junior engineer you're
pairing with over tmux, not a black box you fire-and-forget.

## 1. Launching a session

```bash
tmux new-session -d -s <tmux-session-name> -x 220 -y 50 \
  -c /path/to/target/repo \
  /path/to/Just-Finish-It/dist/jfi
sleep 2
tmux capture-pane -t <tmux-session-name> -p | tail -15
```

It will prompt for a session name, then a goal:

```bash
tmux send-keys -t <tmux-session-name> "<jfi-session-name>" Enter
sleep 1
# For long goals, write the text via python3 (avoids shell-quoting pitfalls
# with tmux send-keys on apostrophes/quotes in a long paragraph), THEN send
# a bare Enter separately:
python3 - <<'PYEOF'
import subprocess
goal = "…detailed goal text…"
subprocess.run(["tmux", "send-keys", "-t", "<tmux-session-name>", goal])
PYEOF
sleep 1
tmux send-keys -t <tmux-session-name> Enter
```

One tmux session per target repo. If you're running JFI against two repos
at once (e.g. a backend + frontend pair), use two separate tmux sessions —
but see §7 on why running two concurrently can starve each other.

## 2. Writing a goal that produces a good plan

JFI's planner is only as good as what you feed it. Before writing the goal:

- **Do your own research first.** Query the real database/API/files
  yourself and put verified facts, exact numbers, and worked examples
  directly in the goal — don't make JFI re-derive things you can just tell
  it. A goal like "compute real dividend totals" produces worse work than
  one that says "dividendsTotal must equal $466.26, verified via this exact
  SQL query — cross-check your engine's output against it."
- **For large/multi-part tasks, write a spec file instead of a giant goal
  string.** Create e.g. `todo_fix.md` in the target repo with sections,
  hand-verified expected values, and explicit out-of-scope boundaries, then
  give JFI a short goal that says "read and follow `todo_fix.md` exactly."
  This keeps the goal message itself small (reduces context/reasoning-cap
  errors — see §8) while still giving JFI a rigorous spec.
- **State what NOT to touch explicitly** (other repos, specific files,
  existing working logic) — JFI will not infer scope boundaries on its own.
- **Bake in the verification bar.** Tell it exactly how to prove the work
  is real: fresh-process server restarts (not stale curl), specific
  hand-computed numbers to assert against, "assert none of these fake
  values appear anymore" checks. JFI's own reviewer is only as rigorous as
  what you ask for.
- **Split cross-cutting work by repo/session**, and prefer backend-then-
  frontend ordering when one depends on the other's output shape.

## 3. Monitoring

Everything JFI writes lives in ONE flat, hidden `.jfi/` folder at the
target repo's root — not a per-session subfolder, and not a `JFI/<name>/`
folder (that layout is retired; see the target repo's own
`src/JFI/session/simple_session_manager.py::__init__` if you need the
exact rationale). Only one JFI session can run against a given repo at a
time, so `.jfi/`'s live-state files always describe whichever session is
currently running:

```bash
cat /path/to/repo/.jfi/web_status.json | python3 -m json.tool
```

Key fields: `session` (the running session's name — check this matches
the one you launched), `phase` (planner/imp/testing/reviewer/cleanup),
`done_phases`, `state` (`streaming`/`running tools`/`thinking`/
`idle · queue empty`/`awaiting review approval`), `plan: [done, total]`
leaf counts, `awaiting` (non-null when it's blocked on a prompt —
retry/approve decision, or a skip/stop confirmation).

For deeper context (what it's actually doing/why), use
`tmux capture-pane -t <tmux-session-name> -p | tail -N` — the reasoning
traces there are how you catch it going off track (see §6) or falsely
marking work complete (see §9).

The plan itself is DB-backed (`.jfi/JFI.db`, a shared SQLite file — every
session ever run against this repo is a row in it, keyed by session_id),
not a file — there is no `plan.md` to `cat` or read directly. To review a
generated plan before trusting it for anything non-trivial (does it
correctly decompose into small leaves, does it correctly capture the facts
you fed it, does it plan real verification rather than superficial
checks), either watch it build live in `tmux capture-pane`, or run
`uv run export-db` from the *JFI repo itself* (not the target repo) with
the target repo as an argument — it writes a human-readable
`.jfi/<session>_plan_export.md` you can read back in the target repo.

## 4. Handling prompts

JFI pauses the TUI with a prompt and needs a keypress to continue:

- **`LLM request failed. Retry, or stop the run?`** — usually transient.
  Retry: `tmux send-keys -t <tmux-session-name> "" Enter` (blank Enter
  selects the highlighted "Retry now"). If the same request fails 3+ times
  in a row even when nothing else is contending for the model, that's a
  real problem (see §8), not something to keep blindly retrying.
- **`Review failed — start another fix iteration?`** — read *why* before
  approving: read `.jfi/review.md` if it still exists (it can get
  overwritten by the next iteration fast, so check promptly — and it's
  flat like everything else in `.jfi/`, so it only ever reflects whichever
  session most recently wrote one). Approve
  (`tmux send-keys -t <tmux-session-name> "" Enter`) for genuinely minor
  issues (missing README docs, a stray file). Treat correctness/regression
  findings as NOT minor — read the actual issue, don't rubber-stamp. JFI
  caps itself at 3 review-fail iterations per session; if it still hasn't
  converged after 3, that's a real signal, not something to force past.
- **A plain idle prompt after `PIPELINE COMPLETE`** doesn't accept forced
  (`!`) directives reliably — send a normal (non-`!`-prefixed) message and
  press Enter instead; it starts a fresh continuation turn.

## 5. Correcting a session mid-run (forced directives)

Prefix a message with `!` to inject it as the very next turn instead of
queuing it for after review:

```bash
python3 - <<'PYEOF'
import subprocess
msg = "!<correction text>"
subprocess.run(["tmux", "send-keys", "-t", "<tmux-session-name>", msg])
PYEOF
sleep 1
tmux send-keys -t <tmux-session-name> Enter
```

It shows as queued (`⚡1` in the status bar) and takes effect once the
in-flight tool call finishes — it won't interrupt an already-running tool
call, so expect a short delay before it lands. Verify it actually landed
(check the pane for the directive text, or re-check the thing you flagged)
rather than assuming one message fixes everything — for a stuck pattern you
may need a second, more explicit directive with literal code to paste (see
§9 for a case that needed three).

Use forced directives for: reinventing tools instead of using an available
one (e.g. writing a raw puppeteer/CDP script when a `browse_webpage` tool
already exists — this recurs, watch for it), a wrong external-API
assumption you've since verified is wrong, a plan that's missing something
critical, or a regression you caught that it hasn't noticed yet.

## 6. Common failure patterns to watch for

- **Tool reinvention.** JFI will sometimes write a custom browser-automation
  script (puppeteer/raw CDP) instead of using a `browse_webpage`-style tool
  it already has. Correct it immediately and tell it to delete the
  throwaway script/dependency it added.
- **Self-inflicted `pkill -f` matching its own wrapper shell**, killing its
  own command. It usually self-diagnoses this one.
- **Race-condition false negatives in its own tests** — e.g. checking a
  DOM value immediately after a page load without waiting for an async
  fetch to resolve, then concluding a feature is broken when it isn't. If
  it reports something broken that you'd expect to work, ask it to re-check
  with an explicit wait before accepting the finding.
- **Losing track after context compression** — it may re-read files it
  already read, or briefly get confused about what it already did. Usually
  self-corrects; if it starts going in circles, intervene.

## 7. Session/process lifecycle

- **User says "stop"**: `tmux send-keys -t <tmux-session-name> C-c` (mid-
  stream) or send `"s"` + Enter at an `awaiting` prompt. This closes the
  tmux session/process; progress is saved to `.jfi/JFI.db` (history, plan,
  everything). It is not resumable *from that same tmux pane* — you must
  relaunch.
- **Resuming** (after a stop, or after an unexpected crash — see next
  bullet): relaunch exactly as in §1, then enter the **same session name**
  again at the "enter a session name" prompt. JFI detects the existing
  session_id already has rows in `.jfi/JFI.db` and auto-resumes at the
  exact phase/task it left off, skipping already-completed phases. This is
  safe and expected — use it liberally, it does not lose work. Only one
  session can run against a given repo at a time regardless of name (the
  lock is project-wide, not per-session) — if a name is refused as
  "already running," that means SOME session is live, not necessarily the
  one you tried.
- **Unexpected crashes**: if `tmux capture-pane` suddenly returns
  `can't find pane`, check `tmux ls` and `journalctl --user -n 30 | grep
  tmux-spawn` for a systemd-killed scope (can happen for long-running
  background tmux sessions on some systems). Relaunch/resume the same way —
  no data is lost since JFI persists state to disk continuously.
- **`../../../.env_bk`'s `JFI_WEB_PORT`** (default 7777) only takes effect on process
  start. If two repos share the same default port, only the first to bind
  gets the web dashboard; change one repo's `../../../.env_bk` and relaunch that
  session to get both dashboards live on different ports simultaneously.

## 8. Context/model contention

If you're running JFI against **two repos concurrently** and both hit
`Context size has been exceeded` or reasoning-cap errors repeatedly at the
same time, they're likely sharing one local LLM backend (e.g. LM Studio)
whose context budget gets divided across concurrent requests. Fix: stop one
session (§7), let the other finish, then resume the first — don't just keep
retrying both in parallel hoping it clears up on its own.

If a single session (running alone) hits a context/reasoning error 3+ times
in a row on the same request, suspect the goal/spec-file size itself (a
very large goal or `todo_fix.md`-style doc can push a single planning turn
close to the model's limit) rather than transient infra — consider trimming
the doc or splitting the task.

## 9. Never trust "PIPELINE COMPLETE" alone

This is the single most important rule. A session reporting
`✅ PIPELINE COMPLETE` or passing its own review is **not sufficient**
evidence the work is correct — its own live-server verification can pass
against a *stale* already-running process instead of a fresh one, and
review passes can miss real regressions the reviewer didn't think to check.

Concretely observed failure in production use of this skill: a restructure
task deleted the server's `main()`/`if __name__ == "__main__":` entrypoint
while moving code between files. The session's own tests and even its
reviewer's live checks passed — because they curled a server process that
was already running from *before* the entrypoint was deleted, not a fresh
one. This happened **three separate times** in one session (it kept
"fixing" it, moving on, and losing the fix again on the next edit) before a
maximally explicit forced directive (exact code to paste + a 4-step
mandatory proof sequence: grep for the function, kill any existing process
by PID, start a genuinely new one, grep its own log for a real startup
line) got it to stick.

**Always, after any "done" claim, independently verify yourself — don't
delegate verification back to the same session that just claimed success:**

```bash
# Kill anything already running FIRST (never pkill -f a pattern that could
# match your own wrapper shell — kill by PID, or use a pattern specific
# enough not to self-match).
ps aux | grep <process-name> | grep -v grep   # find PIDs
kill -9 <pid> <pid> …

# Start genuinely fresh, capture its own log, and confirm the log itself
# proves startup — not just that a curl succeeded (which could hit a
# leftover stale process on the same port).
nohup <start-command> > /tmp/verify.log 2>&1 &
disown
sleep 6
grep "<expected startup log line>" /tmp/verify.log
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:<port>/health

# Then re-derive a couple of the "expected" ground-truth numbers yourself
# (independently — a fresh query/script, not reading the session's own
# output) and compare.
```

If verification fails, don't just report it — figure out root cause
yourself if you can (grep for what's missing), then send a forced directive
with the exact concrete fix rather than a vague "please check this."

## 10. Multi-repo coordination

When a task spans a backend and frontend repo:

1. Do backend work first (its own JFI session, its own tmux pane),
   independently verify the API/data really is real (not seed/placeholder
   data — cross-check against the actual source of truth, e.g. query the
   real DB directly and compare).
2. Only then dispatch the frontend session to wire up to the now-verified
   backend, with the backend's real port/contract given explicitly in the
   goal.
3. Keep `../../../.env_bk`/config values (ports, API URLs) consistent between the two
   goals — mismatches here are a common self-inflicted bug.
