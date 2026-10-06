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
- `start_background_process` / `stop_background_process`, `check_page`;
- `read_file`, `search_code`, `list_dir`, `get_plan`, `get_reviewer_notes`;
- `leaf_diff`: what a leaf changed (its git checkpoint, see
  [`phase-imp.md`](phase-imp.md)), or which leaves changed a file -- so a later
  leaf that broke earlier work shows up;
- `reopen_leaf`, `write_review_report`, `finish`.

`search_code` and `list_dir` are there because, without them, the calc run's
reviewer searched with `findstr` and got 99,395 characters back. For a web app
(the runbook has a `view` URL) the prompt has it start the app and
`check_page` the URL: console errors, uncaught exceptions and failed requests
are bugs, since passing unit tests don't mean the page works. To use the UI
like a person (fill a form, click a tab, check what changed) it can
`load_tool("browser")`: one headless page kept open across calls
(`tool/browser_session.py`), driven by the `[ref]` numbers each call lists,
with screenshots attached for the model to see. An HTTP API is exercised with
`http_request` (optional pool).

An episode that ends on its budget or turn cap before a verdict gets one
continuation episode (`REVIEW_CONTINUE`) that runs the e2e and looks only where
it fails.

It reads the Dev and planner notes, runs the runbook's `e2e`, and gives exactly
one verdict:

| Verdict | How | What happens |
|---|---|---|
| **pass** | `finish(0, "PASS: ...")` | `finish` **re-runs the e2e itself** and refuses unless it exits 0. On a pass, every done leaf's `review_status` becomes `passed` and the `e2e` runbook entry is marked verified. |
| **fix** | `reopen_leaf(leaf_id, fix_note)`, then `finish` | A bug in code that was built (G8). The leaf goes back to `todo` with its `fix_note` (`review_status = failed`, `reopened_count += 1`, logged as a `reopen` PlanEvent). Dev's brief carries the note. With `revert=true` the leaf's files first go back to before it (`checkpoint_tools.revert_leaf`; a file it created is deleted), so Dev rebuilds it instead of patching a wrong approach -- refused if a later leaf or an edit since the last checkpoint touched the same files. |
| **missing** | `write_review_report(text)`, then `finish` | Work that was never planned. The run's next iteration hands the report to the Architect in extend mode. |

A check that can't run in this environment is reported with
`write_review_report` as not checked, never passed.

When the session has ground-truth evidence (`.jfi/evidence/<session>/`) ([`old_new.md`](old_new.md)), the
reviewer has `compare_evidence` / `list_evidence`, and `finish` compares every
case again over the finished build before it confirms a pass
(`Reviewer._evidence_check`): a case that differs refuses the pass and names
its compare leaf to reopen. The pass message lists any case checked only
against LLM-generated (not verified) evidence.

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
