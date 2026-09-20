"""renumber_plan (src/JFI/tool/plan_renumber.py) -- deterministic,
number-depth-driven renumbering of a plan.md subtree, written to replace
the fragile hand-edited-line-by-line renumbering a real session was
observed doing repeatedly after each split (see ENHANCEMENT_IDEAS.md's own
citation of that session's metadata.json digest before this tool existed).
"""

import re

import pytest

from JFI.tool.plan_renumber import renumber_plan


class TestRenumberPlan:
    def test_simple_two_children_unchanged_when_already_correct(self):
        text = (
            "- 1. Parent\n"
            "  - [ ] 1.1 First\n"
            "  - [ ] 1.2 Second\n"
        )
        assert renumber_plan(text, "1") == text

    def test_inserted_sibling_renumbers_everything_after_it(self):
        # A new item was appended as "1.3" out of order (or duplicated) --
        # renumbering by POSITION (encounter order) must fix it regardless
        # of what number it was originally given.
        text = (
            "- 1. Parent\n"
            "  - [ ] 1.1 First\n"
            "  - [ ] 1.9 Newly inserted second item\n"
            "  - [ ] 1.2 Was second, now third\n"
        )
        expected = (
            "- 1. Parent\n"
            "  - [ ] 1.1 First\n"
            "  - [ ] 1.2 Newly inserted second item\n"
            "  - [ ] 1.3 Was second, now third\n"
        )
        assert renumber_plan(text, "1") == expected

    def test_duplicate_numbers_are_repaired(self):
        """The exact failure mode that motivated this tool: two leaves
        both ended up numbered 3.3 after a manual split went wrong."""
        text = (
            "- 3. Parent\n"
            "  - [ ] 3.1 First\n"
            "  - [ ] 3.3 Collided A\n"
            "  - [ ] 3.3 Collided B\n"
        )
        expected = (
            "- 3. Parent\n"
            "  - [ ] 3.1 First\n"
            "  - [ ] 3.2 Collided A\n"
            "  - [ ] 3.3 Collided B\n"
        )
        assert renumber_plan(text, "3") == expected

    def test_preserves_checkbox_state_and_description_text_exactly(self):
        text = (
            "- 1. Parent\n"
            "  - [x] 1.1 Already done, ticked\n"
            "  - [○] 1.2 Skipped by the user\n"
            "  - [ ] 1.3 Still pending\n"
        )
        result = renumber_plan(text, "1")
        assert "[x] 1.1 Already done, ticked" in result
        assert "[○] 1.2 Skipped by the user" in result
        assert "[ ] 1.3 Still pending" in result

    def test_deep_nesting_renumbers_each_level_independently(self):
        text = (
            "- 1. Parent\n"
            "  - 1.1 Mid\n"
            "    - [ ] 1.1.9 First leaf\n"
            "    - [ ] 1.1.2 Second leaf\n"
            "  - [ ] 1.2 Sibling leaf\n"
        )
        expected = (
            "- 1. Parent\n"
            "  - 1.1 Mid\n"
            "    - [ ] 1.1.1 First leaf\n"
            "    - [ ] 1.1.2 Second leaf\n"
            "  - [ ] 1.2 Sibling leaf\n"
        )
        assert renumber_plan(text, "1") == expected

    def test_renumbers_a_nested_parent_not_just_the_top(self):
        """Renumbering scope is whatever parent number is passed -- a
        sub-split under 1.1 shouldn't have to touch 1.2/1.3 at all."""
        text = (
            "- 1. Parent\n"
            "  - 1.1 Mid\n"
            "    - [ ] 1.1.9 Stray number\n"
            "    - [ ] 1.1.2 Other leaf\n"
            "  - [ ] 1.2 Untouched sibling\n"
        )
        result = renumber_plan(text, "1.1")
        assert "[ ] 1.1.1 Stray number" in result
        assert "[ ] 1.1.2 Other leaf" in result
        assert "[ ] 1.2 Untouched sibling" in result  # outside the "1.1" subtree, left alone

    def test_does_not_touch_siblings_outside_the_subtree(self):
        text = (
            "- 1. First parent\n"
            "  - [ ] 1.1 Leaf\n"
            "- 2. Second parent\n"
            "  - [ ] 2.1 Leaf\n"
        )
        result = renumber_plan(text, "1")
        assert "- 2. Second parent" in result
        assert "[ ] 2.1 Leaf" in result

    def test_missing_parent_raises_value_error(self):
        text = "- 1. Parent\n  - [ ] 1.1 Leaf\n"
        with pytest.raises(ValueError, match="9"):
            renumber_plan(text, "9")

    def test_parent_own_number_never_changes(self):
        text = (
            "## Implementation\n"
            "- 3. Parent stays 3\n"
            "  - [ ] 3.5 Out-of-order leaf\n"
            "  - [ ] 3.1 Another leaf\n"
        )
        result = renumber_plan(text, "3")
        assert "- 3. Parent stays 3" in result

    def test_asterisk_bullets_supported(self):
        text = "* 1. Parent\n  * [ ] 1.9 Leaf\n"
        result = renumber_plan(text, "1")
        assert "* [ ] 1.1 Leaf" in result

    def test_trusts_the_number_over_broken_indentation(self):
        """Observed live in a real session's plan.md: an auto-split wrote a
        correctly-numbered deep leaf (depth 7, "4.3.1.1.1.1.1") at only
        2 spaces of indent (depth 2) after a replace_in_file that inserted
        new lines without re-indenting anything after them. The NUMBER's
        own depth must win, and the output indentation must be repaired to
        match it -- not the other way around."""
        text = (
            "- 1. Parent\n"
            "  - 1.1 Mid\n"
            "    - 1.1.1 Deeper\n"
            "- [ ] 1.1.1.1 Under-indented but correctly numbered leaf\n"  # should be depth 4, written at depth 1
            "    - [ ] 1.1.2 Back to a normal sibling of 1.1.1\n"
        )
        result = renumber_plan(text, "1")
        lines = result.split("\n")
        leaf_line = next(line for line in lines if "Under-indented" in line)
        assert leaf_line.startswith("      - [ ]")  # depth 4 -> 6 spaces
        assert re.search(r"\b1\.1\.1\.1\b", leaf_line)

    def test_repairs_indentation_to_match_the_number_depth_throughout(self):
        text = (
            "- 1. Parent\n"
            "- 1.1 Mid, written with no indent at all\n"
            "- [ ] 1.1.1 Leaf, also with no indent\n"
        )
        result = renumber_plan(text, "1")
        lines = result.split("\n")
        assert any(line.startswith("  - 1.1 ") for line in lines)
        assert any(line.startswith("    - [ ] 1.1.1 ") for line in lines)
