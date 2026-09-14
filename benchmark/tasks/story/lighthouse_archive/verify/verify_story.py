#!/usr/bin/env python3
"""
Post-hoc checker for a 'story' tier task -- copied into the project
directory only AFTER the JFI session ends, then run as `verify.command`.

Honest about what this can and can't check (see benchmark/README.md's
"Evaluation methodology" section for the full reasoning): there is no
ground truth for creative writing, so every check here is a MECHANICAL
GATE on things that are objectively verifiable (a file exists, it has a
title, its length is in range, it contains the premise's concrete nouns,
it isn't contaminated with leaked model-internal tokens) -- NOT a judgment
of whether the story is any good. Passing every gate here means "the model
did not skip or badly botch the assignment," not "this is a good story."
Exits 0 iff every gate passes.
"""
import json
import re
import sys
from pathlib import Path

# A leaked chat-template/special token (</s>, <|channel|>, <tool_call|>, ...)
# always has a '|' immediately touching a '<' or '>' -- real prose, markdown,
# or HTML never does. Mirrors src/JFI/text_sanitize.py's own signature
# exactly, deliberately conservative so it never flags legitimate punctuation.
LEAKED_TOKEN_RE = re.compile(r"<\s*\||\|\s*>")


def main() -> int:
    spec = json.loads((Path(__file__).parent / "story_spec.json").read_text(encoding="utf-8"))
    story_path = Path(spec["filename"])
    checks = []

    if not story_path.exists():
        print(f"FAIL: {story_path} does not exist")
        return 1

    text = story_path.read_text(encoding="utf-8", errors="replace")
    lines = [l for l in text.splitlines() if l.strip()]

    has_title = bool(lines) and (lines[0].lstrip().startswith("#") or (len(lines[0]) < 100 and len(lines) > 1))
    checks.append(("has a title as the first non-blank line", has_title))

    word_count = len(text.split())
    in_range = spec["min_words"] <= word_count <= spec["max_words"]
    checks.append((f"word count {word_count} in [{spec['min_words']}, {spec['max_words']}]", in_range))

    for keyword in spec["required_keywords"]:
        checks.append((f"mentions '{keyword}'", keyword.lower() in text.lower()))

    no_leak = not LEAKED_TOKEN_RE.search(text)
    checks.append(("no leaked chat-template tokens", no_leak))

    passed = 0
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
        passed += ok
    print(f"\n{passed}/{len(checks)} mechanical gates passed (word count: {word_count})")
    print("NOTE: these gates check the assignment was not skipped/botched, not narrative quality -- see README.")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
