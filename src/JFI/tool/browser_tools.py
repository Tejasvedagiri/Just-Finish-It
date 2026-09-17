"""Real-browser page reading tool.

``fetch_webpage_images`` (web_tools.py) is a plain HTTP GET — no JavaScript
runs, so a single-page app or anything rendered client-side comes back
empty. ``browse_webpage`` fills that gap with an actual headless Chromium
browser (Playwright): it executes the page's JS, optionally clicks one
element first, then returns the rendered title and visible text.

Deliberately stateless, like every other tool here: one call = one
throwaway browser session (launch, navigate, optionally click, read,
close). There is no persistent session across calls — see the function's
own docstring for why, and what to do instead if a real multi-step flow is
needed.

Requires the ``playwright`` package (a real dependency, see pyproject.toml)
AND its Chromium binary (``playwright install chromium``, a separate
download not managed by pip/uv) — both are checked at call time with a
specific, actionable error message rather than a raw traceback, the same
pattern ``capture_screenshot`` uses for its own optional ``mss`` dependency.

Running from the frozen ``dist/jfi`` binary (see build_binary/__init__.py,
which needs ``--collect-all playwright`` for the same reason
prompt_toolkit/openai do) needs one more fix: PyInstaller's onefile
extraction unpacks playwright's driver under a fresh temp directory every
run, and the driver's own Node-side browser lookup treats the mere
existence of a ``driver/package/.local-browsers/`` path relative to
itself as "this is a self-contained local install" — a false positive
under PyInstaller, since that directory is just part of the bundled
package skeleton, not a real local browser cache — and only looks there,
never falling back to the real cache under ``~/.cache/ms-playwright``
where ``playwright install`` actually put the browser. Setting
``PLAYWRIGHT_BROWSERS_PATH`` explicitly (below) short-circuits that
lookup: the env var always wins, in every install mode (frozen or not).
"""

import os
import sys
from typing import Optional
from urllib.parse import urlparse

NAV_TIMEOUT_SECONDS_DEFAULT = 15
# Generous but bounded: an unbounded page's full visible text could be
# enormous (a long article, a data-dense dashboard) and would otherwise
# blow straight through a small model's context on a single tool result.
MAX_TEXT_CHARS = 8000

# Playwright's own default when this is unset (POSIX only — this project
# has never targeted Windows, see simple_session_manager.py's flock
# comment). setdefault so an operator's own explicit override always wins;
# this only fills in the default playwright would already pick unprompted
# in a normal (non-frozen) install — see the module docstring for why a
# frozen build needs it set explicitly rather than left to Playwright's own
# lookup.
_DEFAULT_BROWSERS_PATH = (
    "~/Library/Caches/ms-playwright" if sys.platform == "darwin" else "~/.cache/ms-playwright"
)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser(_DEFAULT_BROWSERS_PATH))


def browse_webpage(
    url: str,
    click_selector: Optional[str] = None,
    wait_selector: Optional[str] = None,
    timeout: int = NAV_TIMEOUT_SECONDS_DEFAULT,
    eval_js: Optional[str] = None,
) -> str:
    """
    Loads `url` in a real headless browser with JavaScript execution, then
    returns the rendered page's title and visible text.

    Use this instead of fetch_webpage_images/execute_command+curl whenever
    JS execution actually matters: a single-page app, content that renders
    or updates client-side, or verifying your own app after a real user
    interaction. For a plain static page, curl or fetch_webpage_images is
    cheaper — reach for this one specifically because a real browser is
    needed.

    `click_selector` (a CSS selector, e.g. "button.load-more") is clicked
    once, before reading, if given — use it to open a menu/tab or trigger a
    client-side action, then read what appeared. `wait_selector` (a CSS
    selector) blocks until that element appears before reading — use it for
    content that renders asynchronously after the initial page load
    (skipped if `click_selector` is also given, since the click already
    waits for its own target element).

    `eval_js` runs one JS EXPRESSION (not a statement — no `;`, no
    multi-line blocks; wrap several steps in an arrow function and call it
    immediately if you need more than one, e.g. `(() => {...; return x})()`)
    in the page after the click/wait, and its (JSON-stringified) result is
    included in the output as "Eval result: ...". Visible text alone can't
    show DOM state a user wouldn't see as text — an attribute
    (`document.body.getAttribute('data-theme')`), a class
    (`el.classList.contains('open')`), a computed style, an element count.
    This is that escape hatch — reach for it instead of a custom
    jsdom/Node-driven workaround, which this tool exists to make
    unnecessary.

    One call = one throwaway session: navigate, optionally click, read,
    close. There is no persistent session across calls — a second call
    starts a brand-new browser from `url` again. For a workflow needing
    several sequential interactions, either combine them into one
    `click_selector` (if a single click is enough to reach the state you
    need) or verify each step with its own separate call.

    `timeout` is seconds allowed for navigation and for the optional
    click/wait — raise it for a slow-loading page.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return (
            f"Error: unsupported URL scheme '{parsed.scheme or '(none)'}'. Only http/https "
            "URLs are supported."
        )

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return (
            "Error: browsing requires the 'playwright' package, which is not installed in "
            "this environment. Skip this step, note the blocker in the plan, and continue."
        )

    timeout_ms = max(1, int(timeout)) * 1000

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except PlaywrightError as e:
                if "executable doesn't exist" in str(e).lower():
                    return (
                        "Error: Chromium's browser binary is not installed. Run "
                        "`playwright install chromium` (via execute_command) once, then retry. "
                        "If that fails in this environment (no network access), skip this step, "
                        "note the blocker in the plan, and continue."
                    )
                return f"Error launching the browser: {e}"

            try:
                page = browser.new_page()
                try:
                    page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                except PlaywrightTimeoutError:
                    return f"Error: {url} did not finish loading within {timeout}s."
                except PlaywrightError as e:
                    return f"Error loading {url}: {e}"

                if click_selector:
                    try:
                        page.click(click_selector, timeout=timeout_ms)
                    except PlaywrightTimeoutError:
                        return (
                            f"Error: click_selector '{click_selector}' was not found/clickable "
                            f"on {url} within {timeout}s."
                        )
                    except PlaywrightError as e:
                        return f"Error clicking '{click_selector}' on {url}: {e}"
                elif wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=timeout_ms)
                    except PlaywrightTimeoutError:
                        return (
                            f"Error: wait_selector '{wait_selector}' never appeared on {url} "
                            f"within {timeout}s."
                        )
                    except PlaywrightError as e:
                        return f"Error waiting for '{wait_selector}' on {url}: {e}"

                title = page.title()
                text = page.inner_text("body").strip()

                eval_result = None
                eval_error = None
                if eval_js:
                    try:
                        eval_result = page.evaluate(eval_js)
                    except PlaywrightError as e:
                        eval_error = str(e)
            finally:
                browser.close()
    except PlaywrightError as e:
        return f"Error browsing {url}: {e}"

    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS]

    header = f"Success: loaded {url}\nTitle: {title}\n\n"
    body = text if text else "(page body has no visible text)"
    footer = f"\n\n(truncated to {MAX_TEXT_CHARS} characters)" if truncated else ""

    eval_section = ""
    if eval_js:
        if eval_error is not None:
            eval_section = f"\n\nEval error ({eval_js!r}): {eval_error}"
        else:
            eval_section = f"\n\nEval result ({eval_js!r}): {eval_result!r}"

    return header + body + footer + eval_section
