#!/usr/bin/env python3
"""
Post-hoc objective checker for the 'calc' terminal task -- copied into the
project directory only AFTER the JFI session ends (never visible to the
model), then run as `verify.command` from task.json. Drives the actual
program end-to-end over stdin/stdout, the same way a human would use it,
rather than trusting that files merely exist -- the Terminal-Bench-style
distinction this benchmark tier is meant to preserve. Exits 0 iff every
check passes; prints one PASS/FAIL line per check either way.
"""
import subprocess
import sys

CASES = [
    ("2 + 2\nexit\n", "4"),
    ("10 - 3\nexit\n", "7"),
    ("6 * 7\nexit\n", "42"),
    ("2 ^ 10\nexit\n", "1024"),
]

DIVIDE_BY_ZERO_INPUT = "1 / 0\nexit\n"


def run(stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", "main.py"],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=30,
    )


def main() -> int:
    checks = []

    for expr, expected in CASES:
        try:
            proc = run(expr)
            ok = expected in proc.stdout and proc.returncode == 0
            checks.append((f"'{expr.splitlines()[0]}' -> contains '{expected}', exit 0", ok))
        except Exception as e:
            checks.append((f"'{expr.splitlines()[0]}' -> ran without error", False))
            print(f"  (exception: {e})")

    try:
        proc = run(DIVIDE_BY_ZERO_INPUT)
        no_traceback = "Traceback" not in proc.stderr and "Traceback" not in proc.stdout
        checks.append(("division by zero -> no traceback, exit 0", no_traceback and proc.returncode == 0))
    except Exception as e:
        checks.append(("division by zero -> ran without error", False))
        print(f"  (exception: {e})")

    try:
        proc = run("not a valid expression\nexit\n")
        no_traceback = "Traceback" not in proc.stderr and "Traceback" not in proc.stdout
        checks.append(("garbage input -> no traceback, exit 0", no_traceback and proc.returncode == 0))
    except Exception as e:
        checks.append(("garbage input -> ran without error", False))
        print(f"  (exception: {e})")

    passed = 0
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
        passed += ok

    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
