#!/usr/bin/env python3
"""Post-hoc structural checker for the 'recipe_card' html task."""
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

    titles = find_all(root, attr="data-recipe-title")
    checks.append(("has one element with data-recipe-title, non-empty text", len(titles) == 1 and text_of(titles[0]).strip() != ""))

    ingredients = find_all(root, attr="data-ingredient")
    checks.append((f"at least 5 elements with data-ingredient (found {len(ingredients)})", len(ingredients) >= 5))
    checks.append(("every data-ingredient element has non-empty text", all(text_of(n).strip() for n in ingredients)))

    steps = find_all(root, attr="data-step")
    checks.append((f"at least 3 elements with data-step (found {len(steps)})", len(steps) >= 3))
    checks.append(("every data-step element has non-empty text", all(text_of(n).strip() for n in steps)))

    imgs = find_all(root, tag="img")
    checks.append(("at least one <img> present", len(imgs) >= 1))
    checks.append(("every <img> has a src attribute", all(n.attrs.get("src") for n in imgs) if imgs else False))

    passed = 0
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
        passed += ok
    print(f"\n{passed}/{len(checks)} structural checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
