"""`browser` -- an interactive headless browser that stays open across calls:
open a page, look at it (a screenshot the model sees), find elements, click,
type, press keys, scroll and read the text, the way a person tests a web UI.

browse_webpage and check_page load a page once and report. A UI flow (fill a
form, click a tab, check what changed) needs the same page across several
calls, so this keeps one browser per JFI process, closed with
close_browser() when the session ends.

Playwright's sync API must be driven from the thread that started it, and
the episode's tool calls come from the pipeline thread (which may already run
an asyncio loop -- the reason browse_webpage uses a worker thread). Every
call here goes to one dedicated worker thread that owns the browser.

Elements are addressed by a ref number from the last element list (each
call that changes the page returns a fresh one), by their visible text, or
by a CSS selector."""

import base64
import concurrent.futures
import json
from typing import Optional
from urllib.parse import urlparse

VIEWPORT = {"width": 1280, "height": 800}
ACTION_TIMEOUT_MS = 10_000
NAV_TIMEOUT_MS = 20_000
MAX_ELEMENTS = 40
TEXT_CHARS = 4000
SCREENSHOT_QUALITY = 70
ACTIONS = ("open", "screenshot", "find", "click", "type", "press", "scroll", "text", "back", "close")

# Marks every visible interactive element with data-jfi-ref and returns a
# short description of each, so the model can say "click 3". Single-page apps
# often make a plain div clickable from JavaScript (the fleet's session cards
# were missed), so an element whose cursor is a pointer counts too -- only the
# outermost one, since the pointer cursor is inherited by its children.
_LIST_ELEMENTS = """(query) => {
  const sel = 'a[href], button, input, select, textarea, summary, [role=button], [role=link], [role=tab],'
            + ' [role=checkbox], [role=menuitem], [onclick], [contenteditable=true]';
  document.querySelectorAll('[data-jfi-ref]').forEach(e => e.removeAttribute('data-jfi-ref'));
  const out = [];
  let n = 0;
  const pointer = (e) => e && e.nodeType === 1 && getComputedStyle(e).cursor === 'pointer';
  for (const el of document.querySelectorAll('body *')) {
    if (!el.matches(sel) && !(pointer(el) && !pointer(el.parentElement))) continue;
    if (el.closest('[data-jfi-ref]') && !el.matches(sel)) continue;
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (r.width === 0 || r.height === 0 || style.visibility === 'hidden' || style.display === 'none') continue;
    const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder
                   || el.getAttribute('title') || el.name || el.id || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    if (query && !label.toLowerCase().includes(query.toLowerCase())) continue;
    n += 1;
    el.setAttribute('data-jfi-ref', String(n));
    const tag = el.tagName.toLowerCase();
    const kind = tag === 'input' ? `input[${el.type}]` : (el.getAttribute('role') || tag);
    out.push({ref: n, kind, label, value: tag === 'input' || tag === 'textarea' ? String(el.value).slice(0, 40) : null});
  }
  return out;
}"""


class BrowserSession:
    def __init__(self):
        self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="jfi-browser")
        self._playwright = self._browser = self._page = None
        self._events: list[str] = []

    def call(self, action: str, **kwargs):
        return self._worker.submit(self._dispatch, action, kwargs).result()

    def close(self) -> None:
        self._worker.submit(self._close).result()

    # ------------------------------------------------------------ worker thread

    def _dispatch(self, action: str, kw: dict):
        if action not in ACTIONS:
            return f"Error: unknown action {action!r}. Use one of: {', '.join(ACTIONS)}."
        if action == "close":
            self._close()
            return "Closed the browser."
        if action == "open":
            return self._open(kw.get("url") or "")
        if self._page is None:
            return 'Error: no page is open. Call browser(action="open", url=...) first.'
        from playwright.sync_api import Error as PlaywrightError
        try:
            return getattr(self, f"_{action}")(kw)
        except PlaywrightError as e:
            return f"Error: {action} failed: {str(e).splitlines()[0]}"

    def _start(self) -> Optional[str]:
        if self._page is not None:
            return None
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError:
            return "Error: the browser tool needs the 'playwright' package, which is not installed here."
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch()
        except PlaywrightError as e:
            self._playwright.stop()
            self._playwright = None
            if "executable doesn't exist" in str(e).lower():
                return ("Error: Chromium's browser binary is not installed. Run `playwright install chromium` "
                        "(via execute_command) once, then retry.")
            return f"Error launching the browser: {e}"
        self._page = self._browser.new_page(viewport=VIEWPORT)
        self._page.set_default_timeout(ACTION_TIMEOUT_MS)
        self._page.on("console", lambda m: self._events.append(f"console {m.type}: {m.text}")
                      if m.type in ("error", "warning") else None)
        self._page.on("pageerror", lambda e: self._events.append(f"uncaught exception: {e}"))
        self._page.on("response", lambda r: self._events.append(f"{r.request.method} {r.url} -> {r.status}")
                      if r.status >= 400 else None)
        return None

    def _close(self) -> None:
        for closer in (self._browser, self._playwright):
            if closer is not None:
                try:
                    (closer.close if closer is self._browser else closer.stop)()
                except Exception:  # noqa: BLE001 -- already gone; nothing left to release
                    pass
        self._playwright = self._browser = self._page = None
        self._events.clear()

    def _open(self, url: str) -> str:
        if urlparse(url).scheme not in ("http", "https", "file"):
            return f"Error: only http, https and file URLs are supported, not {url!r}."
        error = self._start()
        if error:
            return error
        from playwright.sync_api import Error as PlaywrightError
        try:
            self._page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="networkidle")
        except PlaywrightError as e:
            return (f"Error loading {url}: {str(e).splitlines()[0]}. Is the app running (the runbook's run "
                    f"command, with start_background_process)?")
        return self._state()

    def _settle(self) -> None:
        from playwright.sync_api import Error as PlaywrightError
        try:
            self._page.wait_for_load_state("networkidle", timeout=3000)
        except PlaywrightError:
            pass  # a page that keeps polling never goes idle; what's rendered is what we report

    def _state(self, query: str = "") -> str:
        elements = self._page.evaluate(_LIST_ELEMENTS, query)
        lines = [f"{self._page.url} -- {self._page.title() or '(no title)'}"]
        if self._events:
            lines.append("Problems since the last call:")
            lines += [f"  {e}" for e in self._events[:20]]
            self._events.clear()
        if elements:
            lines.append(f"Elements ({len(elements)}{', first ' + str(MAX_ELEMENTS) if len(elements) > MAX_ELEMENTS else ''}):")
            for el in elements[:MAX_ELEMENTS]:
                value = f" value={json.dumps(el['value'], ensure_ascii=False)}" if el["value"] else ""
                lines.append(f"  [{el['ref']}] {el['kind']} {json.dumps(el['label'], ensure_ascii=False)}{value}")
        else:
            lines.append("No matching interactive elements." if query else "No interactive elements.")
        return "\n".join(lines)

    def _locate(self, target: str):
        target = (target or "").strip()
        if not target:
            raise ValueError("target is required: a ref number from the element list, visible text, or a CSS selector")
        if target.isdigit():
            found = self._page.locator(f'[data-jfi-ref="{target}"]')
            missing = f"there is no element [{target}] now; the page changed -- call find to list them again"
        elif target[0] in "#.[" or ">" in target:
            found = self._page.locator(target)
            missing = f"no element matches the selector {target!r}"
        else:
            found = self._page.get_by_text(target, exact=False)
            missing = f"nothing on the page shows the text {target!r}; use a [ref] from the element list"
        # Without this check Playwright waits the whole action timeout for an
        # element that will never appear (10 s per wrong guess).
        if found.count() == 0:
            raise ValueError(missing)
        return found.first

    def _screenshot(self, kw: dict):
        data = self._page.screenshot(type="jpeg", quality=SCREENSHOT_QUALITY, full_page=bool(kw.get("full_page")))
        url = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        return f"{self._state()}\nScreenshot attached below ({len(data)} bytes).", url

    def _find(self, kw: dict) -> str:
        return self._state(kw.get("text") or "")

    def _click(self, kw: dict) -> str:
        try:
            self._locate(kw.get("target")).click()
        except ValueError as e:
            return f"Error: {e}."
        self._settle()
        return self._state()

    def _type(self, kw: dict) -> str:
        try:
            field = self._locate(kw.get("target"))
        except ValueError as e:
            return f"Error: {e}."
        field.fill(kw.get("text") or "")
        if kw.get("submit"):
            field.press("Enter")
        self._settle()
        return self._state()

    def _press(self, kw: dict) -> str:
        if not kw.get("key"):
            return 'Error: key is required, e.g. "Enter", "Tab", "Escape", "ArrowDown".'
        self._page.keyboard.press(kw["key"])
        self._settle()
        return self._state()

    def _scroll(self, kw: dict) -> str:
        step = VIEWPORT["height"] * (-1 if (kw.get("direction") or "down") == "up" else 1)
        self._page.mouse.wheel(0, step)
        self._page.wait_for_timeout(300)
        return self._state()

    def _text(self, kw: dict) -> str:
        text = self._page.inner_text("body").strip()
        more = f"\n... ({len(text) - TEXT_CHARS} more characters)" if len(text) > TEXT_CHARS else ""
        return f"{self._page.url} -- visible text:\n{text[:TEXT_CHARS]}{more}"

    def _back(self, kw: dict) -> str:
        self._page.go_back()
        self._settle()
        return self._state()


_SESSION: Optional[BrowserSession] = None


def browser(action: str, url: Optional[str] = None, target: Optional[str] = None, text: Optional[str] = None,
            key: Optional[str] = None, direction: Optional[str] = None, submit: bool = False,
            full_page: bool = False):
    global _SESSION
    if _SESSION is None:
        _SESSION = BrowserSession()
    return _SESSION.call(action, url=url, target=target, text=text, key=key, direction=direction,
                         submit=submit, full_page=full_page)


def close_browser() -> None:
    global _SESSION
    if _SESSION is not None:
        _SESSION.close()
        _SESSION = None


BROWSER_TOOL_SCHEMA = {"type": "function", "function": {
    "name": "browser",
    "description": (
        "An interactive headless browser that stays open across calls, to use a web UI like a person: "
        "open a URL, screenshot it (you see the image), find elements, click, type, press keys, scroll, read "
        "the text, go back. Every call that changes the page returns the URL, any console errors or failed "
        "requests since the last call, and the visible interactive elements as [ref] kind \"label\". Address an "
        "element by its ref number, its visible text, or a CSS selector. Start the app first (the runbook's "
        "run command, as a background process); a static page with no server opens as a file:/// URL."
    ),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "url": {"type": "string", "description": "open: the page, e.g. the runbook's view URL"},
        "target": {"type": "string", "description": "click/type: a ref number, visible text, or CSS selector"},
        "text": {"type": "string", "description": "type: what to enter; find: filter elements by label"},
        "key": {"type": "string", "description": "press: e.g. Enter, Tab, Escape, ArrowDown"},
        "direction": {"type": "string", "enum": ["down", "up"], "description": "scroll"},
        "submit": {"type": "boolean", "description": "type: press Enter afterwards"},
        "full_page": {"type": "boolean", "description": "screenshot: the whole page, not just the viewport"},
    }, "required": ["action"]},
}}
