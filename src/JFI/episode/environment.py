"""The one line of machine facts every v2 brief carries.

Observed on the first real v2 run (the calc benchmark on Windows): with no
word about the platform, the Architect wrote every runbook command as
`python3 ...` and the e2e as `printf ... | python3 main.py; test $? -eq 0`.
On that machine `python3` is the Microsoft Store alias (exit 9009) and
commands run in cmd.exe, so setup, test_one, build and e2e all failed for
reasons no role could see -- the setup fix alone burned a whole episode.
"""

import os
import platform
import shutil
from functools import lru_cache


def _usable(name: str) -> bool:
    path = shutil.which(name)
    # Windows' "App execution alias" stubs live here and only open the Store.
    return bool(path) and "WindowsApps" not in path


@lru_cache(maxsize=1)
def environment_line() -> str:
    if os.name == "nt":
        system = f"Windows {platform.release()}"
        shell = ("cmd.exe (shell=True): no $(...), printf, test, or && chains that rely on POSIX tools; "
                 "use `cmd /c` syntax or a small Python script")
    else:
        system = platform.system() or "POSIX"
        shell = "/bin/sh"
    pythons = [name for name in ("python3", "python", "py") if _usable(name)]
    python = f"`{pythons[0]}`" if pythons else "none found on PATH"
    unusable = [f"`{n}`" for n in ("python3", "python") if n not in pythons]
    if unusable and pythons:
        python += f" ({', '.join(unusable)} not usable here)"
    return f"Environment: {system}; commands run in {shell}; Python command: {python}."
