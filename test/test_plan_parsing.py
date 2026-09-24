"""Edge cases for the plan-file parsing helpers:

- _pending_items(): missing/unknown sections, all-completed sections, malformed
  task-list lines ("- []", "- [X]", indented sub-bullets), duplicate section
  headers — none of these may crash; only valid unchecked items are returned.
- ensure_plan_file() / plan_progress(): correct True/False outcomes and accurate
  tick counts for existing vs. missing (or empty) plan files.
"""




def _plan_with(manager, text):
    manager.plan_file.parent.mkdir(parents=True, exist_ok=True)
    manager.plan_file.write_text(text, encoding="utf-8")
    return manager


# ---------------------------------------------------------------------------
# _pending_items — section selection
# ---------------------------------------------------------------------------

PLAN_TWO_SECTIONS = """\
# Title

## Context and Prerequisites
Some prose.

## Implementation
- [ ] 1.1 first step
- [x] 1.2 second step (done)
- [ ] 1.3 third step

## Testing
- [ ] 2.1 a test
- [x] 2.2 done test
"""


def test_pending_items_only_requested_section(manager):
    _plan_with(manager, PLAN_TWO_SECTIONS)
    assert manager._pending_items("Implementation") == [
        "- [ ] 1.1 first step",
        "- [ ] 1.3 third step",
    ]
    # Testing items must not leak into the Implementation queue.
    assert "- [ ] 2.1 a test" not in manager._pending_items("Implementation")


def test_pending_items_section_case_insensitive(manager):
    _plan_with(manager, PLAN_TWO_SECTIONS)
    assert len(manager._pending_items("implementation")) == 2


def test_pending_items_matching_section_absent_returns_empty(manager):
    _plan_with(
        manager,
        "# Title\n\n## Testing\n- [ ] 2.1 only testing here\n",
    )
    assert manager._pending_items("Implementation") == []


def test_pending_items_all_completed_section_returns_empty(manager):
    _plan_with(
        manager,
        "## Implementation\n- [x] 1.1 done\n- [x] 1.2 done\n\n"
        "## Testing\n- [ ] 2.1 pending\n",
    )
    assert manager._pending_items("Implementation") == []


def test_pending_items_missing_plan_file_returns_empty(manager):
    assert not manager.plan_file.exists()
    assert manager._pending_items("Testing") == []


def test_pending_items_empty_plan_file_returns_empty(manager):
    _plan_with(manager, "")
    assert manager._pending_items("Implementation") == []


# ---------------------------------------------------------------------------
# _pending_items — malformed lines never crash and are not miscounted
# ---------------------------------------------------------------------------

PLAN_MALFORMED = """\
## Implementation
- [ ] 1.1 valid pending
- [] 1.2 no space in brackets
- [X] 1.3 uppercase done marker
  - [ ] 1.4 indented sub-bullet (still a task-list line)
* [ ] 1.5 star bullet
plain prose line

## Testing
- [] 2.1 malformed here too
"""


def test_pending_items_malformed_lines_ignored(manager):
    _plan_with(manager, PLAN_MALFORMED)
    pending = manager._pending_items("Implementation")
    assert "- [ ] 1.1 valid pending" in pending
    for marker in ("no space", "uppercase"):
        assert not any(marker in line for line in pending), pending


def test_pending_items_indented_and_star_bullets_still_listed(manager):
    """Indented / starred task-list lines inside the section are real items."""
    _plan_with(manager, PLAN_MALFORMED)
    pending = manager._pending_items("Implementation")
    assert any("1.4 indented sub-bullet" in line for line in pending)
    assert any("1.5 star bullet" in line for line in pending)


def test_pending_items_file_order_preserved(manager):
    _plan_with(manager, PLAN_TWO_SECTIONS)
    items = manager._pending_items("Implementation")
    numbers = [line.split()[3] for line in items]  # "- [ ] 1.1 ..." -> "1.1"
    assert numbers == sorted(numbers, key=lambda n: float(n))


# ---------------------------------------------------------------------------
# _pending_items — duplicate section headers
# ---------------------------------------------------------------------------

def test_pending_items_duplicate_section_headers(manager):
    """Two '## Implementation' sections: items from both are queued; after the
    first later header of a different name closes it again."""
    text = (
        "## Implementation\n- [ ] 1.1 first block\n"
        "## Testing\n- [x] 2.1 done\n"
        "## Implementation (iteration 2)\n- [ ] 3.1 second block\n"
    )
    _plan_with(manager, text)
    pending = manager._pending_items("Implementation")
    assert any("1.1 first block" in line for line in pending)
    assert any("3.1 second block" in line for line in pending)


def test_pending_items_section_suffixed_header_matches(manager):
    _plan_with(
        manager,
        "## Implementation (iteration 2: scoped plan items + pytest)\n- [ ] 3.1 x\n",
    )
    assert len(manager._pending_items("Implementation")) == 1


# ---------------------------------------------------------------------------
# ensure_plan_file / plan_progress
# ---------------------------------------------------------------------------

def test_ensure_plan_file_true_for_existing_tracked_plan(console, manager):
    _plan_with(
        manager,
        "## Implementation\n- [x] 1.1 done\n- [ ] 1.2 pending\n",
    )
    assert manager.ensure_plan_file() is True
    # Progress counts must be accurate: 1 of 2 ticked.
    assert manager.plan_progress() == (1, 2)
    # The plan path got registered in the project state metadata.
    assert manager.plan_path in manager.metadata["implemented_files"]
    # Status bar announced the tick count.
    assert any("1/2" in msg for msg in console.system_messages)


def test_ensure_plan_file_false_for_missing_plan(console, manager):
    """A DB-backed session (the normal case now) never has a plan.md at
    all -- ensure_plan_file() must say so False silently, not announce a
    "No plan file found" warning on every planner completion for what is
    now the universal case."""
    assert not manager.plan_file.exists()
    assert manager.ensure_plan_file() is False
    assert not console.system_messages
    # Nothing tracked, nothing counted.
    assert manager.metadata["implemented_files"] == []
    assert manager.plan_progress() == (0, 0)


def test_ensure_plan_file_false_for_empty_plan(console, manager):
    _plan_with(manager, "")
    assert manager.ensure_plan_file() is False
    assert any("is empty" in msg for msg in console.system_messages)


def test_ensure_plan_file_warns_on_itemless_nonempty_plan(console, manager):
    _plan_with(manager, "# Title\n\nNo task list here.\n")
    assert manager.ensure_plan_file() is False
    assert any("no '- [ ]' items" in msg for msg in console.system_messages)


def test_ensure_plan_file_counts_x_and_uppercase_ticks(console, manager):
    _plan_with(
        manager,
        "## Implementation\n- [x] 1.1 a\n- [X] 1.2 b\n- [ ] 1.3 c\n",
    )
    assert manager.ensure_plan_file() is True
    assert manager.plan_progress() == (2, 3)
    assert any("2/3" in msg for msg in console.system_messages)


class TestEnsurePlanFileWarnsOnMissingRequiredHeaders:
    """A planner that drifts away from the two literal '## Implementation'/
    '## Testing' headers (a different name, its own extra headers instead of
    nested bullets, ...) breaks phase_progress silently -- see
    PLAN_FORMAT_RULES. ensure_plan_file must surface that immediately rather
    than let it show up only as a mysteriously empty work queue deep into
    the imp/testing phase."""

    def test_no_warning_when_both_required_sections_have_items(self, console, manager):
        _plan_with(
            manager,
            "## Implementation\n- [ ] 1.1 a\n## Testing\n- [ ] 2.1 b\n",
        )
        assert manager.ensure_plan_file() is True
        assert not any("Implementation' section" in m or "Testing' section" in m
                       for m in console.system_messages)

    def test_warns_when_testing_header_is_missing(self, console, manager):
        _plan_with(manager, "## Implementation\n- [ ] 1.1 a\n")
        manager.ensure_plan_file()
        assert any("no '## Testing' section" in m for m in console.system_messages)
        assert not any("no '## Implementation' section" in m for m in console.system_messages)

    def test_warns_when_implementation_header_is_missing(self, console, manager):
        _plan_with(manager, "## Testing\n- [ ] 2.1 b\n")
        manager.ensure_plan_file()
        assert any("no '## Implementation' section" in m for m in console.system_messages)

    def test_warns_on_both_when_a_planner_invents_its_own_headers_instead(self, console, manager):
        """Reproduces the real failure: a custom '## Tasks' umbrella with its
        own '### 1. ...' subsections instead of the two required headers --
        items exist and are unchecked, but neither literal header appears
        anywhere, so both phases would see an empty queue."""
        _plan_with(
            manager,
            "## Tasks\n### 1. Scaffold\n- [ ] 1.1 a\n### 2. Verification\n- [ ] 2.1 b\n",
        )
        assert manager.ensure_plan_file() is True  # still tracked -- 2 items exist overall
        assert any("no '## Implementation' section" in m for m in console.system_messages)
        assert any("no '## Testing' section" in m for m in console.system_messages)

    def test_warns_when_own_subsection_header_ends_the_required_section_early(self, console, manager):
        """The subtler variant: '## Implementation' IS present, but a nested
        markdown header (not a bullet) for the planner's own subgrouping
        ends that section immediately, per the header-boundary mechanics
        PLAN_FORMAT_RULES now calls out explicitly."""
        _plan_with(
            manager,
            "## Implementation\n### Renderers\n- [ ] 1.1 a\n## Testing\n- [ ] 2.1 b\n",
        )
        manager.ensure_plan_file()
        assert any("no '## Implementation' section" in m for m in console.system_messages)
