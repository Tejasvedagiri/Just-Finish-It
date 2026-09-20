"""
Tests for score.py's plan-structure metrics -- pure functions over plan.md
text, no filesystem/session dependency, so these run standalone:

    python3 -m pytest benchmark/test_score.py

Not wired into the main `uv run pytest` (pyproject.toml's testpaths is
"test" only) -- benchmark/ is a separate offline tool with its own runner
scripts (harness.py, run_all.sh), not part of the agent package the main
suite covers.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import score  # noqa: E402


def test_leaf_and_parent_counts():
    plan = """
## Implementation
- 1. Core arithmetic module
  - [ ] 1.1 Create calculator.py
  - [x] 1.2 Add parse_expression
- 2. REPL entry point
  - [x] 2.1 Create main.py

## Testing
- [ ] 3.1 Run tests
"""
    m = score._plan_structure_metrics(plan)
    assert m["leaf_count"] == 4
    assert m["parent_task_count"] == 2
    assert m["top_level_task_count"] == 3


def test_depth_from_numbering_not_indentation():
    plan = """
## Implementation
- 1. Backend
  - 1.1 Endpoint
    - [ ] 1.1.1 Add route handler
    - [ ] 1.1.2 Validate input
  - [ ] 1.2 Config (already small)
"""
    m = score._plan_structure_metrics(plan)
    assert m["leaf_count"] == 3
    assert m["max_leaf_depth"] == 3
    assert m["min_leaf_depth"] == 2
    assert m["leaves_per_top_level_task"] == {"1": 3}


def test_top_level_period_variant_counts_as_parent():
    # Real plans mix "- 1. Description" (trailing period) at the top level
    # with "- 1.1 Description" (no period) one level down.
    plan = "## Implementation\n- 1. Scaffold\n  - [ ] 1.1 Run create-vite\n"
    m = score._plan_structure_metrics(plan)
    assert m["parent_task_count"] == 1
    assert m["leaf_count"] == 1


def test_empty_plan_has_no_leaves():
    m = score._plan_structure_metrics("")
    assert m == {
        "leaf_count": 0,
        "parent_task_count": 0,
        "max_leaf_depth": 0,
        "min_leaf_depth": 0,
        "avg_leaf_depth": 0,
        "top_level_task_count": 0,
        "leaves_per_top_level_task": {},
    }


def test_flat_plan_has_shallow_depth_despite_leaf_count():
    # The threshold score_session's flag uses (leaf_count >= 5 and
    # max_leaf_depth <= 1) -- checked here at the metrics level since
    # score_session itself needs a real session directory on disk.
    lines = ["## Implementation"] + [f"- [ ] {i}. leaf {i}" for i in range(1, 7)]
    structure = score._plan_structure_metrics("\n".join(lines))
    assert structure["leaf_count"] == 6
    assert structure["max_leaf_depth"] == 1
