# SWE-bench-style tasks (extension point, not yet populated)

[SWE-bench](https://www.swebench.com/) (and its `Verified`/`Lite`/`Pro` variants) is the
closest thing the field has to an agreed standard for *autonomous issue resolution*: an agent
is handed a real GitHub issue against a real repository at a specific commit, works
unsupervised, and is graded by running that repo's own `FAIL_TO_PASS` / `PASS_TO_PASS` test
IDs against whatever the agent left behind -- a hidden, ground-truth oracle the agent cannot
see or edit, exactly like the `hidden_tests_dir` mechanism the `polyglot` tier already uses,
just at repository scale instead of single-module scale.

This tier is **not populated with real SWE-bench instances yet** -- doing that properly needs
things this pass didn't have the infrastructure for:

- **Network access** to clone the instance's repo at its exact `base_commit`.
- **A matching Python/Node/etc. environment per instance** -- real SWE-bench instances pin
  exact dependency versions, and getting a repo's test suite to even run cleanly is often
  the majority of the setup work (the official harness solves this with per-instance Docker
  images).
- **A decision about how JFI should receive the issue.** JFI's whole design assumes a single
  free-text "goal" and produces its own plan from scratch (see the top-level README's
  four-phase pipeline) -- a SWE-bench issue is closer to "here's a bug report against an
  *existing* codebase," which JFI can still handle (the goal can simply be the issue text,
  with the repo already checked out in the project directory before JFI launches), but it's
  a meaningfully different shape of task than "build something from nothing," and is worth
  flagging rather than quietly mixing in.

## The task shape this harness already supports

`harness.py` and `score.py` don't actually care whether a task is "build an app from
scratch" or "fix a bug in an existing repo" -- both just need a project directory that
already contains whatever should be there *before* JFI launches, a `prompt` (the goal text),
and a `verify.command` that returns exit 0 iff the held-out tests pass. Concretely, a real
SWE-bench-lite instance plugs into this exact shape:

```jsonc
{
  "id": "django__django-11099",
  "tier": "swebench",
  "title": "<the instance's own short description>",
  "language": "python",
  // "setup" isn't a real field yet -- add one, or a tiny setup.sh the
  // harness runs in project_dir before launch, that does:
  //   git clone <repo_url> . && git checkout <base_commit>
  //   <the instance's install_cmds, e.g. pip install -e .>
  "prompt": "<the issue's title + body, verbatim>",
  "verify": {
    // the instance's own FAIL_TO_PASS test IDs, exactly as SWE-bench defines them
    "command": "pytest <fail_to_pass_test_ids> <pass_to_pass_test_ids>"
  }
}
```

The instances themselves (issue text, base commit, install commands, FAIL_TO_PASS/
PASS_TO_PASS test IDs) are published on Hugging Face as
[`princeton-nlp/SWE-bench_Lite`](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite) and
[`princeton-nlp/SWE-bench_Verified`](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified) --
pulling a handful of Lite instances (it's the smaller, more dependency-friendly split) and
writing the setup-script glue above is the concrete next step for this tier, once there's
time to also work through JFI-specific wrinkles: its planner will want to write a `plan.md`
into a repo it didn't create, and `PLAN_FORMAT_RULES` already assumes an empty-ish project
root rather than someone else's existing source tree.

## Why this belongs in the benchmark at all

The `polyglot` tier tests clean-room function implementation; `terminal` tests building a
small app end-to-end. Neither tests the specific skill SWE-bench is built around: **reading
and correctly modifying code you didn't write**, in a codebase with its own conventions and
existing test suite. That's the skill most real engineering work actually is, and it's a real
gap in this benchmark until this tier has actual instances in it.
