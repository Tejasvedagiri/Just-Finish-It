# Phase: `reviewer`

Proves the finished project works end to end, the way a user would use it.
Code: `src/JFI/review/` (`Reviewer`, the prompt in `prompts.py`);
`runner._run_reviewer` runs it. Design: [`laya_plan.md`](laya_plan.md) §7, G8,
G12, G19.

## One episode

The reviewer is one scoped episode (`role="reviewer"`, model `REVIEWER_*` →
shared). Its brief carries the runbook index and any leftover `JFI:` markers.
Its tools include:
- `runbook_get` / `runbook_set`, `execute_command`;
- `start_background_process` / `stop_background_process`;
- `read_file`, `get_plan`, `get_reviewer_notes`;
- `reopen_leaf`, `write_review_report`, `finish`.

It reads the Dev and planner notes, runs the runbook's `e2e`, and gives exactly
one verdict:

| Verdict | How | What happens |
|---|---|---|
| **pass** | `finish(0, "PASS: ...")` | `finish` **re-runs the e2e itself** and refuses unless it exits 0. On a pass, every done leaf's `review_status` becomes `passed` and the `e2e` runbook entry is marked verified. |
| **fix** | `reopen_leaf(leaf_id, fix_note)`, then `finish` | A bug in code that was built (G8). The leaf goes back to `todo` with its `fix_note` (`review_status = failed`, `reopened_count += 1`, logged as a `reopen` PlanEvent). Dev's brief carries the note. |
| **missing** | `write_review_report(text)`, then `finish` | Work that was never planned. The run's next iteration hands the report to the Architect in extend mode. |

A check that can't run in this environment is reported with
`write_review_report` as not checked, never passed.

## The fix loop (`_run_reviewer`)

```
for round in 1..MAX_REVIEW_ITERATIONS:
    review
    fix  -> Dev fixes only the re-opened leaves (Imp.run), then review again
    else -> done
```

If the leaves still fail after `MAX_REVIEW_ITERATIONS` rounds, a review report
says so and the outer loop takes over. `REVIEWER_COMPLETE` is appended from this
outcome, never from model text. After the phase, the reviewer notes are cleared
(`_clear_reviewer_notes`).

## The next iteration

`collect_next_iteration` (`runner.py`) turns a `REVIEW_REPORT` note, plus
anything the user queued, into the next iteration's feedback. `MAX_REVIEW_ITERATIONS`
failed reviews end the run; `REVIEW_LOOP_APPROVAL=1` asks before each retry.
