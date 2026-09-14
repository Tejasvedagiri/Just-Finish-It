#!/usr/bin/env python3
"""Post-hoc structural checker for the 'pricing_table' html task."""
import re
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

    plans = find_all(root, attr="data-plan")
    plan_names = [n.attrs.get("data-plan", "").strip() for n in plans]
    checks.append((f"exactly 3 elements with a distinct data-plan value (found {len(set(plan_names))})",
                   len(plans) == 3 and len(set(plan_names)) == 3 and all(plan_names)))

    for i, plan in enumerate(plans):
        prices = find_all(plan, attr="data-price")
        has_price_text = len(prices) >= 1 and any(re.search(r"\d", text_of(p)) for p in prices)
        checks.append((f"plan #{i+1} ('{plan.attrs.get('data-plan','?')}') has a data-price element containing a number", has_price_text))

        features = find_all(plan, tag="li")
        checks.append((f"plan #{i+1} has at least 3 <li> feature items (found {len(features)})", len(features) >= 3))

        ctas = find_all(plan, attr="data-cta")
        checks.append((f"plan #{i+1} has a data-cta element", len(ctas) >= 1))

    passed = 0
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
        passed += ok
    print(f"\n{passed}/{len(checks)} structural checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
