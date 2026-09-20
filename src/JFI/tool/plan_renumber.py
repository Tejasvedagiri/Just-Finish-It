"""Deterministic plan.md renumbering -- Python-side, not an LLM turn.

Splitting a leaf into sub-leaves, or inserting a new sibling, shifts every
number after it (see the Journeyman-stage rules in
session/simple_session_manager.py). Doing that shift by hand, one
replace_in_file call per line, is exactly what a real session was observed
getting wrong repeatedly: a stale read produced duplicate/gapped numbers,
which then needed a second and third fix-up pass just to get the numbering
itself consistent, burning planner budget on pure bookkeeping instead of
actual decomposition. This module re-parses the checkbox tree under one
parent and rewrites every descendant's number in a single deterministic
pass.

Callable from a session via execute_command:
    python -m JFI.tool.plan_renumber <plan_path> <parent_number>
prints a summary and, by default, writes the result back to <plan_path> in
place (pass --dry-run to only print the new text, for previewing before
committing to it).

Tree structure is read from each line's OWN NUMBER (its dot-segment count
is its depth; a number starting with "<parent>." is in the subtree),
NEVER from indentation. This was a deliberate choice after testing against
a real, live plan.md: a session's own auto-split can write a semantically
correct, deeply-nested number ("4.3.3.1.3.1.1", depth 7) at shallow,
wrong indentation (2 spaces, implying depth 2) after a `replace_in_file`
that inserted new lines without also fixing the indent of everything
after them. The number stayed right; the indentation drifted. Since a
DUPLICATE number still encodes the correct depth (two sibling "3.3"s are
both still unambiguously depth 2), and a WRONG-INDENT number still encodes
the correct depth too, the number is the more reliable of the two signals
in every failure mode actually observed -- so indentation is regenerated
from the number-derived depth on output (2 spaces per level) rather than
trusted as input. This doubles as an indentation repair, not just a
renumbering.

Line format matched here mirrors the parsing already used elsewhere in
this codebase (see simple_session_manager.py's own checkbox regexes):
"- " or "* ", an optional "[ ]"/"[x]"/"[X]"/"[○]" checkbox, the numeric ID
(a bare "N." at the top level, "N.M[.K...]" with no trailing dot beneath
it -- see PLAN_FORMAT_RULES), then the description.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<bullet>[-*])(?P<sp1>[ \t]+)"
    r"(?P<checkbox>\[[ xX○]\][ \t]+)?"
    r"(?P<num>\d+(?:\.\d+)*)(?P<dot>\.)?(?P<sp2>[ \t]+)(?P<rest>.*)$"
)


def _num_depth(num: str) -> int:
    return num.count(".") + 1


def renumber_plan(text: str, parent: str) -> str:
    """
    Returns `text` with every DESCENDANT of `parent` renumbered in a single
    deterministic pass, in the ORIGINAL DOCUMENT ORDER those lines already
    appear in (a document-order invariant that holds even when indentation
    or the numbers themselves have drifted -- see module docstring).
    `parent`'s own line and number are never touched, and no line outside
    its subtree is touched either. Raises ValueError if `parent` isn't
    found (a parent number that no longer exists is a caller mistake worth
    surfacing loudly, not silently no-op'ing).
    """
    lines = text.split("\n")
    parsed: List[Tuple[int, Optional[re.Match]]] = [(i, _LINE_RE.match(line)) for i, line in enumerate(lines)]

    parent_index = None
    for i, m in parsed:
        if m and m.group("num") == parent:
            parent_index = i
            break
    if parent_index is None:
        raise ValueError(f"parent {parent!r} not found in plan")

    prefix = parent + "."
    parent_depth = _num_depth(parent)

    end = parent_index + 1
    while end < len(lines):
        m = parsed[end][1]
        if m and m.group("num").startswith(prefix):
            end += 1
        else:
            break

    # Stack of (depth, new_number_parts, new_indent) mirroring the nesting
    # implied by each line's OWN (possibly stale/duplicated/mis-indented)
    # number -- never by that line's actual on-disk indentation.
    stack: List[Tuple[int, List[str]]] = [(parent_depth, parent.split("."))]
    child_counter = {parent: 0}

    new_lines = list(lines)
    for i in range(parent_index + 1, end):
        m = parsed[i][1]
        if not m:
            continue
        depth = _num_depth(m.group("num"))
        while len(stack) > 1 and stack[-1][0] >= depth:
            stack.pop()
        parent_parts_here = stack[-1][1]
        parent_str_here = ".".join(parent_parts_here)

        child_counter[parent_str_here] = child_counter.get(parent_str_here, 0) + 1
        new_num_parts = parent_parts_here + [str(child_counter[parent_str_here])]
        new_num = ".".join(new_num_parts)
        new_depth = len(new_num_parts)

        checkbox = m.group("checkbox") or ""
        dot = m.group("dot") or ""  # preserves this line's own "N." vs "N.M" style (see module docstring)
        new_indent = "  " * (new_depth - 1)
        new_lines[i] = f"{new_indent}{m.group('bullet')}{m.group('sp1')}{checkbox}{new_num}{dot}{m.group('sp2')}{m.group('rest')}"
        stack.append((depth, new_num_parts))

    return "\n".join(new_lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plan_path", help="Path to plan.md")
    parser.add_argument("parent", help="Number of the parent whose descendants should be renumbered, e.g. '3' or '3.1'")
    parser.add_argument("--dry-run", action="store_true", help="Print the result instead of writing it back to plan_path")
    args = parser.parse_args(argv)

    path = Path(args.plan_path)
    text = path.read_text(encoding="utf-8")
    try:
        result = renumber_plan(text, args.parent)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(result)
    else:
        path.write_text(result, encoding="utf-8")
        print(f"Renumbered descendants of {args.parent} in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
