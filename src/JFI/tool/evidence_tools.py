"""Ground-truth evidence (docs/old_new.md): what the new code has to match,
captured once into the session's .jfi/evidence/<session_id>/ folder, and compared with what
the new code gives.

A **case** is one named thing to match (`add`, `watchlist_tab`,
`active_customers`). Its evidence is one of:

- behavioural: `<case>.txt` -- header lines (`# key: value`), then
  one `>>> <input>` line per input followed by the ground truth's output
  (`!error <text>` when the ground truth failed on it). An input `@file`
  names a file in the evidence folder (a SQL query, a request body) instead.
- visual: `<case>.png` (the original, screenshotted or copied)
  plus `<case>.json` (how it was shot, and where the new app shows
  the same state).

Evidence is written by `capture_evidence`, never typed: a hand-written
expected value carries the same misunderstanding as the code (the reason
JFI's own unit tests, written by the same model, pass a misread
requirement). It tries, in order: the runbook's `evidence_one` command (a
CLI like bc, curl/wget for an API, psql for a query), a screenshot, an image
or file it copies, and only then answers the model supplies -- flagged
`source: llm -- not verified`. The header records how, so `recapture` can
redo it and a person reading the file knows how far to trust it.

`compare_case` runs the case's own `compare` command (header, for a leaf's
one function) or the runbook's `compare_one` (the NEW code on one input)
for every input and compares the outputs here, in code: numbers by value,
words exactly, separators ignored ("tokens", the default), or `exact` /
`contains`. A visual case screenshots the new app in the same state and
measures the share of pixels that differ, in the same headless browser
check_page uses (no image library needed).
"""

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from sqlmodel import select

from JFI.models import PlanEvent, RunbookEntry, get_session
from JFI.models._util import utcnow

# Inside .jfi/ (never committed, never edited by Dev's file tools, which refuse
# .jfi/), one folder per session so a new session never mixes with an old one's.
EVIDENCE_DIR = ".jfi/evidence"
SCRATCH_DIR = Path(".jfi") / "compare"
DEFAULT_VIEWPORT = "1280x800"
DEFAULT_MAX_DIFF = 0.10
COMMAND_TIMEOUT_SECONDS = 120
NAV_TIMEOUT_MS = 15_000
MATCH_MODES = ("tokens", "exact", "contains")
LLM_SOURCE = "llm -- not verified; check this file"
_CASE = re.compile(r"^[a-z][a-z0-9_-]{0,59}$")
# <evidence folder>/<plan number>_<case><suffix>: the number is the task that owns it
# (sync_evidence_names keeps it current); a file without one isn't on a node yet.
_FILE = re.compile(r"^(?:(\d+(?:\.\d+)*)_)?([a-z][a-z0-9_-]{0,59}?)(\.result\.txt|\.new\.png|\.compare\.png|"
                   r"\.txt|\.png|\.json|\.sql)$")
GROUND_SUFFIXES = (".txt", ".png", ".json", ".sql")
RESULT_SUFFIXES = (".result.txt", ".new.png", ".compare.png")
_URL = re.compile(r"(https?|file)://\S+")
_TOKEN = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[^\W\d_]+", re.UNICODE)
_API_PROGRAMS = ("curl", "wget", "http", "https", "xh")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")


def max_diff() -> float:
    """COMPARE_MAX_DIFF: the share of pixels a visual case may differ by."""
    try:
        return max(0.0, min(1.0, float(os.environ.get("COMPARE_MAX_DIFF", DEFAULT_MAX_DIFF))))
    except ValueError:
        return DEFAULT_MAX_DIFF


# ------------------------------------------------------------------ the files

@dataclass
class Item:
    input: str
    output: str
    error: bool = False


@dataclass
class Evidence:
    case: str
    path: Path
    visual: bool
    header: Dict[str, str] = field(default_factory=dict)
    items: List[Item] = field(default_factory=list)
    spec: Dict = field(default_factory=dict)  # a visual case's .json

    @property
    def source(self) -> str:
        return (self.spec.get("source") if self.visual else self.header.get("source")) or ""

    @property
    def unverified(self) -> bool:
        return self.source.startswith("llm")

    @property
    def reference_only(self) -> bool:
        """Evidence marked `check: reference` is kept to show, not compared.
        Only sessions planned before every node compared itself have it (an
        Architect's overview of the ground truth)."""
        return ((self.spec if self.visual else self.header).get("check") or "") == "reference"


def evidence_dir(root: Path, session_id: str) -> Path:
    return Path(root) / EVIDENCE_DIR / session_id


def shown(root: Path, path: Path) -> str:
    """A path as the model and the person see it: relative to the project."""
    try:
        return Path(path).relative_to(Path(root)).as_posix()
    except ValueError:
        return str(path)


def case_problem(case: str) -> Optional[str]:
    if not _CASE.match(case or ""):
        return (f"Error: case name {case!r} must start with a letter and use lowercase letters, digits, _ or - "
                "(e.g. add, divide_by_zero, watchlist_tab); its files are <task number>_<case>.* in the evidence folder.")
    return None


def _files(root: Path, session_id: str) -> List[Tuple[Path, Optional[str], str, str]]:
    """(path, plan number or None, case, suffix) for every evidence file."""
    folder = evidence_dir(root, session_id)
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.iterdir()):
        match = _FILE.match(path.name)
        if match and path.is_file():
            out.append((path, match.group(1), match.group(2), match.group(3)))
    return out


def _find(root: Path, session_id: str, case: str, suffix: str) -> Optional[Path]:
    return next((path for path, _, c, s in _files(root, session_id) if c == case and s == suffix), None)


def evidence_path(root: Path, session_id: str, case: str, suffix: str) -> Path:
    """The case's file with this suffix: the existing one (whatever its
    number), or a new one without a number until sync_evidence_names gives it
    its task's."""
    return _find(root, session_id, case, suffix) or evidence_dir(root, session_id) / f"{case}{suffix}"


def list_cases(root: Path, session_id: str) -> List[str]:
    return sorted({c for _, _, c, s in _files(root, session_id) if s in (".txt", ".png")})


def sync_evidence_names(engine, session_id: str, root: Path) -> List[str]:
    """Names every evidence file after the node whose `cases` hold the case,
    as the plan numbers it now: an Architect's component (2_dividends_view.png),
    a Lead's file (1.2_add.txt) or a Task's leaf (1.2.3_add_decimals.txt), and
    Dev's results for it beside them (1.2.3_add_decimals.result.txt). A legacy
    compare leaf names the results of the case it checks. Plan numbers shift
    when nodes are added, so this runs after planning steps and before
    comparisons. Returns the renames done."""
    from JFI.models import build_indexes, display_number
    from JFI.planner.nodes import load_nodes

    nodes = load_nodes(engine, session_id)
    if not nodes or not evidence_dir(root, session_id).is_dir():
        return []
    by_id, siblings = build_indexes(nodes)
    owner, checker = {}, {}
    for node in nodes:
        for case in node.cases or []:
            target = checker if node.kind == "compare" else owner
            target.setdefault(case, display_number(node, by_id, siblings))
    renames = []
    for path, number, case, suffix in _files(root, session_id):
        wanted = (checker.get(case) if suffix in RESULT_SUFFIXES else None) or owner.get(case)
        name = f"{wanted}_{case}{suffix}" if wanted else f"{case}{suffix}"
        if wanted and number != wanted and not (path.parent / name).exists():
            path.rename(path.parent / name)
            renames.append(f"{path.name} -> {name}")
    return renames


def parse_text(text: str) -> Tuple[Dict[str, str], List[Item]]:
    header: Dict[str, str] = {}
    items: List[Item] = []
    current: Optional[Item] = None
    lines: List[str] = []

    def close():
        if current is not None:
            out = "\n".join(lines).strip("\n")
            if out.startswith("!error"):
                current.error, out = True, out[len("!error"):].strip()
            current.output = out
            items.append(current)

    for line in text.splitlines():
        if current is None and line.startswith("#"):
            key, _, value = line[1:].partition(":")
            if value:
                header[key.strip().lower()] = value.strip()
            continue
        if line.startswith(">>> ") or line == ">>>":
            close()
            current, lines = Item(input=line[4:], output=""), []
            continue
        if current is not None:
            lines.append(line)
    close()
    return header, items


def render_text(header: Dict[str, str], items: Sequence[Item]) -> str:
    out = [f"# {key}: {value}" for key, value in header.items() if value]
    for item in items:
        out.append(f">>> {item.input}")
        out.append(("!error " + item.output) if item.error else item.output)
    return "\n".join(out) + "\n"


def read_evidence(root: Path, session_id: str, case: str) -> Optional[Evidence]:
    text_path, png_path = _find(root, session_id, case, ".txt"), _find(root, session_id, case, ".png")
    if text_path is not None:
        header, items = parse_text(text_path.read_text(encoding="utf-8"))
        return Evidence(case, text_path, False, header, items)
    if png_path is not None:
        spec_path = _find(root, session_id, case, ".json")
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8")) if spec_path else {}
        except json.JSONDecodeError:
            spec = {}
        return Evidence(case, png_path, True, spec=spec)
    return None


def evidence_hash(root: Path, session_id: str, cases: Sequence[str]) -> str:
    """A fingerprint of the cases' evidence, ignoring the `reviewed` mark a
    person sets after reading a file -- so accepting evidence doesn't send
    its compare leaf back, but editing or re-capturing it does."""
    digest = hashlib.sha256()
    for case in sorted(cases):
        digest.update(case.encode())
        for suffix in GROUND_SUFFIXES:
            path = _find(root, session_id, case, suffix)
            if path is None:
                continue
            data = path.read_bytes()
            if suffix == ".txt":
                data = b"\n".join(line for line in data.splitlines() if not line.startswith(b"# reviewed:"))
            elif suffix == ".json":
                try:
                    spec = json.loads(data)
                    spec.pop("reviewed", None)
                    data = json.dumps(spec, sort_keys=True).encode()
                except json.JSONDecodeError:
                    pass
            digest.update(suffix.encode() + data)
    return digest.hexdigest()[:16]


def mark_reviewed(root: Path, session_id: str, case: str) -> str:
    """A person read the evidence and agrees with it (the dashboard's Accept)."""
    evidence = read_evidence(root, session_id, case)
    if evidence is None:
        return f"Error: no evidence for case {case!r}."
    stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    if evidence.visual:
        spec_path = evidence_path(root, session_id, case, ".json")
        evidence.spec["reviewed"] = stamp
        spec_path.write_text(json.dumps(evidence.spec, indent=2) + "\n", encoding="utf-8")
    else:
        evidence.header["reviewed"] = stamp
        evidence.path.write_text(render_text(evidence.header, evidence.items), encoding="utf-8")
    return f"Marked {case} as reviewed."


def save_edited_text(root: Path, session_id: str, case: str, text: str) -> str:
    """A person corrected a behavioural case's evidence (the dashboard's Edit):
    the file says so, and what it was captured from before."""
    path = _find(root, session_id, case, ".txt")
    if path is None:
        return f"Error: no text evidence for case {case!r}."
    header, items = parse_text(text)
    if not items:
        return "Error: the evidence needs at least one '>>> input' line followed by its output."
    old_source = parse_text(path.read_text(encoding="utf-8"))[0].get("source", "")
    if header.get("source") != "user":
        header["was"] = header.get("source") or old_source
        header["source"] = "user"
    path.write_text(render_text(header, items), encoding="utf-8")
    return f"Saved {path.name}."


# ------------------------------------------------------------------ running commands

def _runbook(engine, session_id: str, name: str) -> Optional[RunbookEntry]:
    with get_session(engine) as db:
        return db.exec(select(RunbookEntry).where(RunbookEntry.session_id == session_id,
                                                  RunbookEntry.name == name)).first()


def _input_file(root: Path, session_id: str, value: str, n: int) -> Path:
    if value.startswith("@"):
        name = value[1:].strip()
        match = _FILE.match(name)
        found = _find(root, session_id, match.group(2), match.group(3)) if match else None
        return (found or evidence_dir(root, session_id) / name).resolve()
    scratch = Path(root) / SCRATCH_DIR
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / f"input-{n}.txt"
    path.write_text(value + "\n", encoding="utf-8")
    return path.resolve()


def fill(template: str, root: Path, session_id: str, value: str = "", n: int = 0, case: str = "", view: str = "") -> str:
    """{input} is the input as written; {input_file} a file holding it (an
    `@file` input's own file) -- the safe choice for anything with quotes."""
    text = value
    if value.startswith("@"):
        path = _input_file(root, session_id, value, n)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
    command = template.replace("{project}", str(Path(root).resolve())).replace("{case}", case)
    command = command.replace("{view}", view)
    if "{input_file}" in command:
        command = command.replace("{input_file}", str(_input_file(root, session_id, value, n)))
    return command.replace("{input}", text.strip())


def run(command: str, root: Path) -> Tuple[int, str, str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(command, shell=True, cwd=root, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return 124, "", f"(timed out after {COMMAND_TIMEOUT_SECONDS}s)"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


_SHELL_ERROR = re.compile(r"^(/bin/)?(ba|da|z)?sh: |: (command )?not found\b|Syntax error", re.M)


def _command_broken(code: int, output: str) -> bool:
    """The command itself didn't run (not found, not executable, a shell
    syntax error), as opposed to the ground truth answering with an error."""
    return code in (126, 127) or bool(_SHELL_ERROR.search(output))


def _outcome(code: int, stdout: str, stderr: str) -> Tuple[str, bool]:
    """(output, failed). A command that prints only to stderr has failed even
    with exit 0: bc reports "Divide by zero" that way."""
    out = stdout.strip()
    if code != 0 or (not out and stderr.strip()):
        return (stderr.strip() or out or f"exit {code}"), True
    return out, False


# ------------------------------------------------------------------ the browser

def _browser_problem() -> Optional[str]:
    try:
        import JFI.tool.browser_tools  # noqa: F401 -- sets PLAYWRIGHT_BROWSERS_PATH for frozen builds
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return "Error: screenshots need the 'playwright' package, which is not installed here."
    return None


def _viewport(text: str) -> Tuple[int, int]:
    match = re.match(r"^\s*(\d{2,5})\s*[x×]\s*(\d{2,5})\s*$", text or DEFAULT_VIEWPORT)
    return (int(match.group(1)), int(match.group(2))) if match else (1280, 800)


def _resolve_url(url: str, root: Path, view: str = "") -> str:
    url = (url or "").strip()
    if re.match(r"^(https?|file)://", url):
        return url
    # A #fragment or ?query stays one: as_uri() would make it part of the
    # file name (page.html%23holdings, "file not found").
    split = re.search(r"[#?]", url)
    path, tail = (url[:split.start()], url[split.start():]) if split else (url, "")
    if view and (url.startswith("/") or not (Path(root) / path).exists()):
        return view.rstrip("/") + "/" + url.lstrip("/")
    return (Path(root) / path).resolve().as_uri() + tail


def _do_step(page, step: str) -> None:
    verb, _, arg = step.strip().partition(" ")
    verb, arg = verb.lower(), arg.strip()
    if verb == "click":
        if re.match(r"^[#.\[]", arg):
            page.locator(arg).filter(visible=True).first.click(timeout=5000)
            return
        # A control before plain text: found while building this, "click
        # Watchlist" hit the nav label "Watchlist" (the first text match) and
        # never the Watchlist button, so the tab it opens was never shot.
        # Visible ones only: a hidden copy (a collapsed mobile menu) can come first.
        for locator in (page.get_by_role("button", name=arg), page.get_by_role("link", name=arg),
                        page.get_by_role("tab", name=arg), page.get_by_text(arg, exact=True), page.get_by_text(arg)):
            locator = locator.filter(visible=True)
            if locator.count():
                locator.first.click(timeout=5000)
                return
        raise ValueError(f"nothing to click called {arg!r}")
    elif verb == "type":
        selector, _, text = arg.partition("=")
        page.fill(selector.strip(), text.strip(), timeout=5000)
    elif verb == "press":
        page.keyboard.press(arg)
    elif verb == "scroll":
        page.mouse.wheel(0, int(arg or 600))
    elif verb == "wait":
        page.wait_for_timeout(int(arg or 500))
    else:
        raise ValueError(f"unknown step {step!r}: use click <text or css>, type <css>=<text>, press <key>, "
                         "scroll <pixels> or wait <ms>")


# What a person could click on the page, for a step that did nothing.
_CLICKABLE_JS = """() => {
  const seen = new Set(), out = [];
  for (const el of document.querySelectorAll('body *')) {
    if (out.length >= 25) break;
    const style = getComputedStyle(el), box = el.getBoundingClientRect();
    if (style.cursor !== 'pointer' || !box.width || !box.height) continue;
    const inside = el.parentElement && seen.has(el.parentElement);
    seen.add(el);
    if (inside) continue;  // the icon or label inside a nav item, not another control
    const text = (el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 40);
    const id = el.id ? '#' + el.id : '';
    const data = [...el.attributes].filter(a => a.name.startsWith('data-')).map(a => `[${a.name}="${a.value}"]`)[0] || '';
    if (text || id || data) out.push(`${el.tagName.toLowerCase()}${id}${data}${text ? ' "' + text + '"' : ''}`);
  }
  return out;
}"""


def screenshot(url: str, out: Path, viewport: str = DEFAULT_VIEWPORT, steps: Sequence[str] = (),
               selector: str = "", must_change: bool = False) -> Tuple[str, str]:
    """(error or "", visible text). The viewport, not the full page: both sides
    are shot the same way, and the pair stays a manageable image. With a
    selector, only that element (one chart, one card). `must_change`: steps
    that leave the page exactly as it loaded are an error, not a capture --
    observed on the first real run, where every side-bar tab's evidence was
    the home page."""
    problem = _browser_problem()
    if problem:
        return problem, ""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    width, height = _viewport(viewport)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except PlaywrightError as e:
                if "executable doesn't exist" in str(e).lower():
                    return ("Error: Chromium's browser binary is not installed. Run `playwright install chromium` "
                            "once, then retry."), ""
                return f"Error launching the browser: {e}", ""
            try:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="networkidle")
                before = page.screenshot() if must_change and steps else None
                for step in steps or ():
                    _do_step(page, step)
                page.wait_for_timeout(200)
                if before is not None and page.screenshot() == before:
                    clickable = page.evaluate(_CLICKABLE_JS)
                    return (f"Error: the steps {list(steps)} changed nothing on {url}: the page is exactly as it "
                            f"loaded, so this would save the first screen again. Click the control itself with a "
                            f"CSS step (e.g. 'click [data-view=\"holdings\"]' or 'click #nav-holdings'). Clickable "
                            f"here: " + "; ".join(clickable or ["nothing found"])), ""
                if selector:
                    element = page.locator(selector).first
                    if not page.locator(selector).count():
                        return f"Error: nothing on {url} matches the selector {selector!r}.", ""
                    if not element.is_visible():
                        return (f"Error: {selector!r} is on {url} but hidden on this screen (another tab or view). "
                                f"Add steps to open it first, e.g. steps=['click Holdings']."), ""
                    element.scroll_into_view_if_needed(timeout=5000)
                    text = element.inner_text().strip()
                    element.screenshot(path=str(out))
                else:
                    text = page.inner_text("body").strip() if page.query_selector("body") else ""
                    page.screenshot(path=str(out))
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 -- a page that won't load, a step that can't be done
        return f"Error screenshotting {url}: {e}", ""
    return "", text


_DIFF_JS = """async ([a, b]) => {
  const load = src => new Promise((ok, bad) => { const i = new Image(); i.onload = () => ok(i); i.onerror = bad; i.src = src; });
  const [ia, ib] = await Promise.all([load(a), load(b)]);
  const w = ia.width, h = ia.height;
  const c = document.createElement('canvas'); c.width = w * 3; c.height = h;
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, w * 3, h);
  ctx.drawImage(ia, 0, 0); ctx.drawImage(ib, w, 0, w, h);
  const da = ctx.getImageData(0, 0, w, h).data, db = ctx.getImageData(w, 0, w, h).data;
  const out = ctx.createImageData(w, h); let off = 0;
  for (let p = 0; p < da.length; p += 4) {
    const d = Math.abs(da[p] - db[p]) + Math.abs(da[p + 1] - db[p + 1]) + Math.abs(da[p + 2] - db[p + 2]);
    const bad = d > 48; if (bad) off++;
    out.data[p] = bad ? 255 : 255 - (255 - da[p]) * 0.25;
    out.data[p + 1] = bad ? 0 : 255 - (255 - da[p + 1]) * 0.25;
    out.data[p + 2] = bad ? 0 : 255 - (255 - da[p + 2]) * 0.25;
    out.data[p + 3] = 255;
  }
  ctx.putImageData(out, w * 2, 0);
  return {ratio: off / (w * h), composite: c.toDataURL('image/png')};
}"""


def _data_url(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def diff_images(old: Path, new: Path, out: Path) -> Tuple[str, float, str]:
    """(error or "", share of pixels that differ, data URL of old | new | diff
    side by side). The new image is scaled to the old one's size."""
    problem = _browser_problem()
    if problem:
        return problem, 1.0, ""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                result = page.evaluate(_DIFF_JS, [_data_url(old), _data_url(new)])
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001
        return f"Error comparing the screenshots: {e}", 1.0, ""
    composite = result["composite"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(composite.split(",", 1)[1]))
    return "", float(result["ratio"]), composite


def _image_to_png(source: Path, out: Path) -> str:
    """A mockup in any format the browser shows (jpg, webp, svg) as a PNG."""
    if source.suffix.lower() == ".png":
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, out)
        return ""
    problem = _browser_problem()
    if problem:
        return problem
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(source.resolve().as_uri())
                page.locator("img, svg").first.screenshot(path=str(out))
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001
        return f"Error converting {source.name} to PNG: {e}"
    return ""


def _view_url(engine, session_id: str) -> str:
    view = _runbook(engine, session_id, "view")
    match = _URL.search(view.command) if view else None
    return match.group(0).rstrip("/") if match else ""


# ------------------------------------------------------------------ capture

def _event(engine, session_id: str, detail: str) -> None:
    with get_session(engine) as db:
        db.add(PlanEvent(session_id=session_id, node_id=None, type="evidence", detail=detail[:500]))
        db.commit()


def _preview(items: Sequence[Item], limit: int = 8) -> str:
    lines = []
    for item in items[:limit]:
        out = item.output.replace("\n", " | ")
        lines.append(f"  {item.input[:60]}  ->  {'ERROR ' if item.error else ''}{out[:80]}")
    if len(items) > limit:
        lines.append(f"  ... and {len(items) - limit} more")
    return "\n".join(lines)


def capture_evidence(engine, session_id: str, root: Path, role: str, case: str, inputs: Sequence[str] = (),
                     sql: str = "", url: str = "", image: str = "", new_url: str = "", steps: Sequence[str] = (),
                     viewport: str = "", match: str = "", answers: Sequence[str] = (), from_file: str = "",
                     selector: str = "", new_selector: str = "", new_command: str = "") -> str:
    """Saves one case's evidence, from the best source there is (see the
    module docstring). Returns what was saved, or an Error saying what's
    missing."""
    root = Path(root)
    case = (case or "").strip().lower()
    problem = case_problem(case)
    if problem:
        return problem
    folder = evidence_dir(root, session_id)
    folder.mkdir(parents=True, exist_ok=True)
    if url or image:
        return _capture_visual(engine, session_id, root, role, case, url, image, new_url, steps, viewport,
                               selector, new_selector)

    match = (match or "tokens").strip().lower()
    if match not in MATCH_MODES:
        return f"Error: match must be one of {', '.join(MATCH_MODES)} (tokens: numbers by value, words exactly)."
    inputs = [str(i) for i in (inputs or [])]
    if sql:
        evidence_path(root, session_id, case, ".sql").write_text(sql.strip() + "\n", encoding="utf-8")
        inputs = [f"@{case}.sql"]
    if not inputs:
        return ("Error: give the case's inputs (e.g. inputs=['1 + 1', '2.5 + 0.25']), sql='<query>' for a query, "
                "or url/image for a visual case.")
    header = {"case": case}
    evidence_one = _runbook(engine, session_id, "evidence_one")
    if evidence_one is not None:
        items, broken = [], None
        for n, value in enumerate(inputs):
            code, stdout, stderr = run(fill(evidence_one.command, root, session_id, value, n, case), root)
            output, failed = _outcome(code, stdout, stderr)
            if failed and _command_broken(code, output):
                broken = broken or output
            items.append(Item(value, output, failed))
        if broken or (len(items) > 1 and all(item.error for item in items)):
            # Observed while building this: `bc -l <<< ...` under /bin/sh (dash)
            # failed with "redirection unexpected" on every input, and that shell
            # error was saved as the ground truth's answer.
            first = broken or items[0].output
            return (f"Error: evidence_one didn't run, so nothing was saved -- the command itself is wrong. "
                    f"Error: {first[:300]}\nFix it with runbook_set('evidence_one', ...) and capture again. "
                    f"Commands run in a POSIX sh: no bash-only syntax such as <<<; use {{input_file}} (a file "
                    f"holding the input), e.g. \"bc -l < {{input_file}}\".")
        program = evidence_one.command.strip().split()[0].rsplit("/", 1)[-1].lower()
        header["source"] = f"{'api' if program in _API_PROGRAMS else 'command'} {evidence_one.command}"
        header["how"] = evidence_one.command
    elif answers:
        if len(answers) != len(inputs):
            return f"Error: {len(inputs)} input(s) but {len(answers)} answer(s); give one answer per input."
        source_file = from_file.split(" L")[0].strip() if from_file else ""
        if source_file and not (root / source_file).is_file():
            return f"Error: from_file {source_file!r} doesn't exist in the project."
        items = [Item(value, str(answer)) for value, answer in zip(inputs, answers)]
        header["source"] = f"file {from_file}" if from_file else LLM_SOURCE
    else:
        return ("Error: there's no way to get the ground truth's answers. runbook_set('evidence_one', "
                "'<command that prints the ground truth's answer for {input}>') -- a CLI, curl/wget for an API, "
                "a database client for a query -- then capture again. Only when no command, API, page or file "
                "can give them: pass answers=[...] (with from_file='<path> L<a>-<b>' if you copied them from a "
                "file); answers you made up are saved as 'not verified' for a person to check.")
    header["match"] = match
    if new_command.strip():
        header["compare"] = new_command.strip()
    header["captured"] = f"{utcnow().strftime('%Y-%m-%d %H:%M UTC')} by {role or 'jfi'}"
    path = evidence_path(root, session_id, case, ".txt")
    path.write_text(render_text(header, items), encoding="utf-8")
    _event(engine, session_id, f"{case}: {header['source']}")
    sync_evidence_names(engine, session_id, root)
    path = evidence_path(root, session_id, case, ".txt")
    warn = "\nNOT VERIFIED: these answers came from you, not a command or file." if header["source"] == LLM_SOURCE \
        else ""
    return f"Saved {shown(root, path)} ({len(items)} input(s), source: {header['source']}):\n" \
           f"{_preview(items)}{warn}"


def _capture_visual(engine, session_id: str, root: Path, role: str, case: str, url: str, image: str,
                    new_url: str, steps: Sequence[str], viewport: str, selector: str = "",
                    new_selector: str = "") -> str:
    if image and Path(image).suffix.lower() not in IMAGE_SUFFIXES:
        # Observed on the first real run: the Lead passed image="<the dashboard>.html"
        # to capture one chart, and the screenshot of the page's first <img>/<svg>
        # -- its logo -- became the evidence for a dozen cases.
        return (f"Error: image= takes an image file ({', '.join(IMAGE_SUFFIXES)}), not {image!r}. For a page, "
                f"use url={image!r} (plus steps=[...] to reach a state); for ONE part of it -- a chart, a card, "
                f"a pill -- add selector='<css>' (e.g. '#allocation-donut').")
    if not new_url:
        return ("Error: a visual case needs new_url: where the NEW app shows this state (a route like / or "
                "/login, a URL, or a file like index.html) -- compare_evidence screenshots it there.")
    png = evidence_path(root, session_id, case, ".png")
    spec = {"case": case, "new_url": new_url, "steps": list(steps or []), "viewport": viewport or DEFAULT_VIEWPORT}
    if selector:
        spec["selector"] = selector
    if new_selector:
        spec["new_selector"] = new_selector
    if image:
        source = (root / image).resolve()
        if not source.is_file():
            return f"Error: image {image!r} doesn't exist in the project."
        problem = _image_to_png(source, png)
        if problem:
            return problem
        spec.update(source=f"file {image}", image=image, text="")
    else:
        target = _resolve_url(url, root)
        problem, text = screenshot(target, png, spec["viewport"], spec["steps"], selector, must_change=True)
        if problem:
            return problem
        twin = _same_capture(root, session_id, case, png, text if not selector else "")
        if twin:
            png.unlink()
            return (f"Error: this is the same screen as case {twin}'s evidence, so it can't show anything {twin} "
                    f"doesn't. A different tab or view needs steps to reach it (e.g. steps=['click Holdings'] for "
                    f"a side-bar tab): a #fragment or ?query in the url doesn't switch a page that changes views "
                    f"on click. A part of the screen needs selector='<css>'.")
        where = f" {selector}" if selector else ""
        spec.update(source=f"screenshot {target}{where} {spec['viewport']}", url=target, text=text[:4000])
    spec["captured"] = f"{utcnow().strftime('%Y-%m-%d %H:%M UTC')} by {role or 'jfi'}"
    evidence_path(root, session_id, case, ".json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    _event(engine, session_id, f"{case}: {spec['source']}")
    sync_evidence_names(engine, session_id, root)
    png, spec_file = evidence_path(root, session_id, case, ".png"), evidence_path(root, session_id, case, ".json")
    return f"Saved {shown(root, png)} and {spec_file.name} (source: {spec['source']})."


def _same_capture(root: Path, session_id: str, case: str, png: Path, text: str = "") -> Optional[str]:
    """Another case whose evidence is this very screen: the same image, or
    (a whole page, where an animated chart can change a few pixels) the same
    visible text. Observed on the first real run: a dozen cases (holdings
    table, dividend chart, watchlist, news, ...) all saved the home page, so
    none of them could ever check its tab."""
    shot = png.read_bytes()
    for path, _, other, suffix in _files(root, session_id):
        if other == case:
            continue
        if suffix == ".png" and path.read_bytes() == shot:
            return other
        if suffix == ".json" and text:
            try:
                spec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not spec.get("selector") and spec.get("text") == text[:4000]:
                return other
    return None


def recapture(engine, session_id: str, root: Path, case: str) -> str:
    """Re-runs how a case was captured (its header / .json), e.g. after the
    ground truth was fixed. Hand-written evidence can't be re-captured."""
    evidence = read_evidence(root, session_id, case)
    if evidence is None:
        return f"Error: no evidence for case {case!r}."
    if evidence.visual:
        if not evidence.spec.get("url") and not evidence.spec.get("image"):
            return f"Error: {case} doesn't say how it was captured."
        return _capture_visual(engine, session_id, Path(root), "recapture", case, evidence.spec.get("url", ""),
                               evidence.spec.get("image", ""), evidence.spec.get("new_url", ""),
                               evidence.spec.get("steps") or (), evidence.spec.get("viewport", ""),
                               evidence.spec.get("selector", ""), evidence.spec.get("new_selector", ""))
    how = evidence.header.get("how")
    if not how:
        return (f"Error: {case} came from {evidence.source or 'an unknown source'}, not a command, so it can't "
                "be re-captured; edit it instead.")
    items = []
    for n, item in enumerate(evidence.items):
        output, failed = _outcome(*run(fill(how, Path(root), session_id, item.input, n, case), Path(root)))
        items.append(Item(item.input, output, failed))
    header = dict(evidence.header)
    header.pop("reviewed", None)
    header["captured"] = f"{utcnow().strftime('%Y-%m-%d %H:%M UTC')} by recapture"
    evidence.path.write_text(render_text(header, items), encoding="utf-8")
    _event(engine, session_id, f"{case}: re-captured")
    return f"Re-captured {case}:\n{_preview(items)}"


# ------------------------------------------------------------------ compare

def _tokens(text: str) -> List[str]:
    return _TOKEN.findall(text)


def _same_token(a: str, b: str) -> bool:
    if a == b:
        return True
    try:
        x, y = float(a), float(b)
    except ValueError:
        return False
    return math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-9)


def outputs_match(mode: str, expected: str, actual: str) -> bool:
    if mode == "exact":
        return " ".join(expected.split()) == " ".join(actual.split())
    if mode == "contains":
        want, have = _tokens(expected), _tokens(actual)
        return all(any(_same_token(w, h) for h in have) for w in want)
    want, have = _tokens(expected), _tokens(actual)
    return len(want) == len(have) and all(_same_token(w, h) for w, h in zip(want, have))


@dataclass
class CaseResult:
    case: str
    ok: bool
    report: str
    output: str = ""  # everything the new code printed, for spotting a stub it ran into
    image_url: Optional[str] = None
    visual: bool = False
    unverified: bool = False


def compare_case(engine, session_id: str, root: Path, case: str) -> CaseResult:
    root = Path(root)
    evidence = read_evidence(root, session_id, case)
    if evidence is None:
        return CaseResult(case, False, f"Error: no evidence for case {case!r} in {shown(root, evidence_dir(root, session_id))}/ "
                                       f"(capture it with capture_evidence).")
    if evidence.reference_only:
        return CaseResult(case, True, f"{case}: kept to look at, not compared ({shown(root, evidence.path)}).")
    if evidence.visual:
        return _compare_visual(engine, session_id, root, evidence)
    # A case can say how to run the new code itself (a leaf's one function);
    # otherwise the runbook's compare_one runs the whole program.
    command = evidence.header.get("compare") or getattr(_runbook(engine, session_id, "compare_one"), "command", "")
    if not command:
        return CaseResult(case, False, "Error: the runbook has no compare_one: how to run the NEW code on one input "
                                       "({input} or {input_file}), printing its answer. runbook_set it, e.g. "
                                       "\"printf '%s\\nexit\\n' {input} | python3 main.py\".")
    mode = evidence.header.get("match", "tokens")
    view = _view_url(engine, session_id)
    lines, outputs, bad = [f"compare {case}  ({shown(root, evidence.path)}, source: {evidence.source})"], [], 0
    for n, item in enumerate(evidence.items):
        filled = fill(command, root, session_id, item.input, n, case, view)
        code, stdout, stderr = run(filled, root)
        output, failed = _outcome(code, stdout, stderr)
        outputs.append(stdout + stderr)
        if item.error:
            ok = failed or "error" in output.lower()
        else:
            ok = not failed and outputs_match(mode, item.output, output)
        bad += not ok
        expected = ("an error" if item.error else item.output).replace("\n", " | ")
        lines.append(f"  {item.input[:40]:<16} new: {output.replace(chr(10), ' | ')[:80]:<20} "
                     f"evidence: {expected[:80]:<12} {'match' if ok else 'MISMATCH'}")
    total = len(evidence.items)
    lines.append(f"{case}: {total - bad} of {total} match" if not bad else f"{case}: {bad} of {total} differ")
    if evidence.unverified:
        lines.append(f"(the evidence for {case} came from the LLM, not a command or file: it isn't verified)")
    # Dev's evidence: what the new code gave, kept next to the ground truth
    # and named after the same task (sync_evidence_names).
    evidence_path(root, session_id, case, ".result.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sync_evidence_names(engine, session_id, root)
    lines.append(f"  saved: {shown(root, evidence_path(root, session_id, case, '.result.txt'))}")
    return CaseResult(case, bad == 0 and total > 0, "\n".join(lines), "\n".join(outputs),
                      unverified=evidence.unverified)


def _compare_visual(engine, session_id: str, root: Path, evidence: Evidence) -> CaseResult:
    spec, case = evidence.spec, evidence.case
    view = _view_url(engine, session_id)
    target = _resolve_url(spec.get("new_url", ""), root, view)
    new_png = evidence_path(root, session_id, case, ".new.png")
    problem, new_text = screenshot(target, new_png, spec.get("viewport", DEFAULT_VIEWPORT), spec.get("steps") or (),
                                   spec.get("new_selector") or spec.get("selector", ""))
    if problem:
        hint = " Is the app running? Start it with the runbook's run (start_background_process)." \
            if target.startswith("http") else ""
        return CaseResult(case, False, problem + hint, visual=True)
    problem, ratio, composite = diff_images(evidence.path, new_png, evidence_path(root, session_id, case, ".compare.png"))
    sync_evidence_names(engine, session_id, root)
    if problem:
        return CaseResult(case, False, problem, visual=True)
    old_words = {w.lower() for w in re.findall(r"[^\W\d_]{3,}", spec.get("text", ""))}
    new_words = {w.lower() for w in re.findall(r"[^\W\d_]{3,}", new_text)}
    missing = sorted(old_words - new_words)
    # Text as well as pixels: found while building this, a nav link missing
    # from the new page changed 0.03% of the pixels and passed on pixels alone.
    ok = ratio <= max_diff() and not missing
    lines = [f"compare {case}  ({shown(root, evidence_path(root, session_id, case, '.png'))} vs {target}, "
             f"{spec.get('viewport', DEFAULT_VIEWPORT)})",
             f"  differing pixels: {ratio:.2%} (allowed {max_diff():.0%}, COMPARE_MAX_DIFF)",
             f"  text missing from the new page: {', '.join(missing[:25])}" if missing
             else "  text: everything in the original is on the new page" if old_words else "",
             f"  saved: {shown(root, evidence_path(root, session_id, case, '.compare.png'))} (original | new | "
             f"differences in red); attached",
             f"{case}: {'looks the same' if ok else 'DIFFERS'}"]
    return CaseResult(case, ok, "\n".join(line for line in lines if line), image_url=composite, visual=True,
                      unverified=evidence.unverified)


def compare_cases(engine, session_id: str, root: Path, cases: Sequence[str]) -> List[CaseResult]:
    return [compare_case(engine, session_id, root, case) for case in cases]


# ------------------------------------------------------------------ tool binding + schemas

def make_evidence_tools(engine, session_id: str, root: Path, role: str = "") -> Dict[str, Callable]:
    def compare_evidence(case: str = ""):
        cases = [case.strip().lower()] if case and case.strip() else list_cases(root, session_id)
        if not cases:
            return f"No evidence yet: {shown(root, evidence_dir(root, session_id))}/ is empty."
        results = compare_cases(engine, session_id, root, cases)
        text = "\n\n".join(r.report for r in results)
        if len(results) > 1:
            failed = [r.case for r in results if not r.ok]
            text += "\n\n" + (f"{len(failed)} of {len(results)} case(s) differ: {', '.join(failed)}" if failed
                              else f"All {len(results)} case(s) match.")
        image = results[0].image_url if len(results) == 1 else None
        return (text, image) if image else text

    def list_evidence():
        cases = list_cases(root, session_id)
        if not cases:
            return f"No evidence yet: {shown(root, evidence_dir(root, session_id))}/ is empty."
        lines = []
        for case in cases:
            evidence = read_evidence(root, session_id, case)
            what = "screenshot" if evidence.visual else f"{len(evidence.items)} input(s)"
            lines.append(f"{case}: {what}, source: {evidence.source}")
        return "\n".join(lines)

    return {
        "capture_evidence": lambda case, inputs=(), sql="", url="", image="", new_url="", steps=(), viewport="",
        match="", answers=(), from_file="", selector="", new_selector="", new_command="": capture_evidence(
            engine, session_id, root, role, case, inputs, sql, url, image, new_url, steps, viewport, match, answers,
            from_file, selector, new_selector, new_command),
        "compare_evidence": compare_evidence,
        "list_evidence": list_evidence,
    }


EVIDENCE_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "capture_evidence",
        "description": ("Save one case's ground truth in the evidence folder: runs the runbook's evidence_one on each input "
                        "(a CLI, curl/wget, a DB client for sql), or screenshots url, or copies image. You pick the "
                        "inputs, never the answers (answers= is the flagged last resort)."),
        "parameters": {"type": "object", "properties": {
            "case": {"type": "string", "description": "e.g. add, divide_by_zero, watchlist_tab"},
            "inputs": {"type": "array", "items": {"type": "string"}, "description": "e.g. ['1 + 1', '-3 + 10']"},
            "sql": {"type": "string", "description": "a query, run as the input"},
            "url": {"type": "string", "description": "visual: the original page (URL or file) to screenshot"},
            "image": {"type": "string", "description": "visual: a mockup IMAGE file (png, jpg, svg); never a page"},
            "new_url": {"type": "string", "description": "visual: where the NEW app shows it (/, /login, index.html)"},
            "steps": {"type": "array", "items": {"type": "string"},
                      "description": "visual: e.g. ['click Watchlist'] (click/type <css>=<text>/press/scroll/wait)"},
            "selector": {"type": "string", "description": "visual: CSS of ONE part to shoot, e.g. #allocation-donut"},
            "new_selector": {"type": "string", "description": "visual: that part's CSS in the new app, if different"},
            "viewport": {"type": "string", "description": "WIDTHxHEIGHT, default 1280x800"},
            "match": {"type": "string", "enum": list(MATCH_MODES),
                      "description": "tokens (default: numbers by value, words exactly), exact, contains"},
            "answers": {"type": "array", "items": {"type": "string"}, "description": "LAST RESORT, one per input"},
            "from_file": {"type": "string", "description": "where answers= came from, e.g. docs/api.md L40-58"},
            "new_command": {"type": "string", "description": "runs the NEW code for THIS case, if not compare_one"},
        }, "required": ["case"]},
    }},
    {"type": "function", "function": {
        "name": "compare_evidence",
        "description": ("Run the new code on a case's inputs (the runbook's compare_one) or screenshot the new app "
                        "in the case's state, and compare it with its evidence: prints new vs evidence per "
                        "input, or attaches original | new | differences. No case: every case."),
        "parameters": {"type": "object", "properties": {"case": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "list_evidence",
        "description": "The cases in this session's evidence folder, with how each was captured.",
        "parameters": {"type": "object", "properties": {}},
    }},
]
