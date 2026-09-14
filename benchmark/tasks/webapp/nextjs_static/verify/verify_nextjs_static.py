#!/usr/bin/env python3
"""
Post-hoc structural checker for the 'nextjs_static' webapp task -- copied
into the project directory only AFTER the JFI session ends (never visible to
the model), then run as `verify.command` from task.json.

`npm run build` (with `output: 'export'` in next.config) is the real bar
here -- a genuine compile of the actual Next.js project, not a stub -- and
its exit code is checked by the shell `&&` in verify.command before this
script even runs. This script then checks the exported static HTML under
out/ for the required structural hooks, the same data-* attribute approach
the 'html' tier uses (see benchmark/README.md's Evaluation methodology
section) applied to a real framework's build output instead of hand-written
HTML.
"""
import re
import sys
from pathlib import Path

OUT_DIR = Path("out")


def read(name: str) -> str:
    path = OUT_DIR / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    checks = []

    checks.append(("out/ directory exists (npm run build produced a static export)", OUT_DIR.is_dir()))

    index_html = read("index.html")
    checks.append(("out/index.html exists", bool(index_html)))
    checks.append(("index.html has an element with data-page=\"home\"",
                   bool(re.search(r'data-page="home"', index_html))))
    nav_match = re.search(r'data-testid="nav-about"[^>]*href="([^"]*)"', index_html) or \
        re.search(r'href="([^"]*)"[^>]*data-testid="nav-about"', index_html)
    nav_ok = bool(nav_match) and nav_match.group(1).rstrip("/").endswith("about")
    checks.append(("index.html has a data-testid=\"nav-about\" link pointing to /about",
                   nav_ok))

    about_html = read("about.html")
    if not about_html:
        about_html = read("about/index.html")
    checks.append(("out/about.html (or about/index.html) exists", bool(about_html)))
    checks.append(("about page has an element with data-page=\"about\"",
                   bool(re.search(r'data-page="about"', about_html))))

    passed = 0
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
        passed += ok

    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
