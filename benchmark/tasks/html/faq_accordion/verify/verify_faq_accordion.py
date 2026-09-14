#!/usr/bin/env python3
"""
Post-hoc structural checker for the 'faq_accordion' html task.

Mandating the native <details>/<summary> elements (instead of a
JS-driven show/hide div) is deliberate: it makes a real, correctly
functioning expand/collapse interaction mechanically checkable from raw
HTML alone, with no headless browser needed to click anything and verify
the result -- the browser's native behavior IS the interaction, so
structure is behavior here.
"""
import sys
from pathlib import Path
from verify_html_common import parse, find_all, text_of


def main() -> int:
    path = Path("index.html")
    if not path.exists():
        print("FAIL: index.html does not exist")
        return 1

    root = parse(path.read_text(encoding="utf-8", errors="replace"))
    checks = []

    items = find_all(root, tag="details", attr="data-faq")
    checks.append((f"at least 5 <details data-faq> elements (found {len(items)})", len(items) >= 5))

    for i, item in enumerate(items):
        summaries = find_all(item, tag="summary")
        checks.append((f"item #{i+1} has exactly one <summary> (question)", len(summaries) == 1))
        if summaries:
            checks.append((f"item #{i+1}'s <summary> has non-empty question text", text_of(summaries[0]).strip() != ""))
        answer_text = text_of(item)
        for s in summaries:
            answer_text = answer_text.replace(text_of(s), "", 1)
        checks.append((f"item #{i+1} has non-empty answer text beyond the question", answer_text.strip() != ""))

    passed = 0
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
        passed += ok
    print(f"\n{passed}/{len(checks)} structural checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
