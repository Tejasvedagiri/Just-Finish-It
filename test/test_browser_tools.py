"""Tests for browse_webpage (JFI.tool.browser_tools).

No real browser is launched — playwright.sync_api.sync_playwright is
monkeypatched to a fake object graph (mirrors test_web_tools.py's FakeClient
approach for httpx), so these tests are fast and offline.
"""
import importlib
import os
import sys
import types

import pytest

import JFI.tool.browser_tools as browser_tools


def test_sets_playwright_browsers_path_default_when_unset(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    importlib.reload(browser_tools)
    try:
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == os.path.expanduser(
            "~/.cache/ms-playwright" if sys.platform != "darwin" else "~/Library/Caches/ms-playwright"
        )
    finally:
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        importlib.reload(browser_tools)


def test_does_not_override_an_explicit_playwright_browsers_path(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/custom/browsers/path")
    importlib.reload(browser_tools)
    try:
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/custom/browsers/path"
    finally:
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        importlib.reload(browser_tools)


class FakePage:
    def __init__(self, title="Example", text="hello world", raise_on_goto=None,
                 raise_on_click=None, raise_on_wait=None, eval_result=None,
                 raise_on_evaluate=None):
        self._title = title
        self._text = text
        self._raise_on_goto = raise_on_goto
        self._raise_on_click = raise_on_click
        self._raise_on_wait = raise_on_wait
        self._eval_result = eval_result
        self._raise_on_evaluate = raise_on_evaluate
        self.goto_calls = []
        self.click_calls = []
        self.wait_calls = []
        self.evaluate_calls = []

    def goto(self, url, timeout=None, wait_until=None):
        self.goto_calls.append((url, timeout, wait_until))
        if self._raise_on_goto:
            raise self._raise_on_goto

    def click(self, selector, timeout=None):
        self.click_calls.append((selector, timeout))
        if self._raise_on_click:
            raise self._raise_on_click

    def wait_for_selector(self, selector, timeout=None):
        self.wait_calls.append((selector, timeout))
        if self._raise_on_wait:
            raise self._raise_on_wait

    def title(self):
        return self._title

    def inner_text(self, selector):
        assert selector == "body"
        return self._text

    def evaluate(self, expression):
        self.evaluate_calls.append(expression)
        if self._raise_on_evaluate:
            raise self._raise_on_evaluate
        return self._eval_result


class FakeBrowser:
    def __init__(self, page, raise_on_launch=None):
        self._page = page
        self._raise_on_launch = raise_on_launch
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser=None, raise_on_launch=None):
        self._browser = browser
        self._raise_on_launch = raise_on_launch

    def launch(self):
        if self._raise_on_launch:
            raise self._raise_on_launch
        return self._browser


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


# Module-level and stable (not rebuilt per call) so a test can construct an
# exception instance from these BEFORE calling _install_fake_playwright and
# have browse_webpage's own lazy `from playwright.sync_api import ...`
# (evaluated at call time, after install) resolve to these SAME class
# objects — an `except SomeError:` only matches instances of the exact
# class (or its subclasses) it was bound to, so two different rebuilt
# classes across two install calls would silently fail to catch.
class FakePlaywrightError(Exception):
    pass


class FakeTimeoutError(FakePlaywrightError):
    pass


def _install_fake_playwright(monkeypatch, page=None, raise_on_launch=None):
    """Builds a fake playwright.sync_api module with just enough surface
    for browse_webpage's lazy `from playwright.sync_api import ...` to work,
    and installs it into sys.modules so the import picks it up."""
    page = page or FakePage()
    browser = FakeBrowser(page)
    chromium = FakeChromium(browser=browser, raise_on_launch=raise_on_launch)

    def fake_sync_playwright():
        return FakePlaywright(chromium)

    sync_api_module = types.ModuleType("playwright.sync_api")
    sync_api_module.sync_playwright = fake_sync_playwright
    sync_api_module.Error = FakePlaywrightError
    sync_api_module.TimeoutError = FakeTimeoutError
    playwright_module = types.ModuleType("playwright")
    playwright_module.sync_api = sync_api_module

    monkeypatch.setitem(sys.modules, "playwright", playwright_module)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api_module)
    return sync_api_module, browser, page


def test_rejects_non_http_scheme():
    result = browser_tools.browse_webpage("file:///etc/passwd")
    assert result.startswith("Error")
    assert "scheme" in result


def test_missing_playwright_package_reports_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    result = browser_tools.browse_webpage("https://example.com")
    assert result.startswith("Error")
    assert "playwright" in result.lower()


def test_successful_load_returns_title_and_text(monkeypatch):
    fake_module, browser, page = _install_fake_playwright(
        monkeypatch, page=FakePage(title="My Page", text="Some visible text")
    )
    result = browser_tools.browse_webpage("https://example.com")
    assert result.startswith("Success:")
    assert "My Page" in result
    assert "Some visible text" in result
    assert browser.closed is True
    assert page.goto_calls == [("https://example.com", 15000, "domcontentloaded")]


def test_custom_timeout_is_passed_through_in_ms(monkeypatch):
    fake_module, browser, page = _install_fake_playwright(monkeypatch)
    browser_tools.browse_webpage("https://example.com", timeout=5)
    assert page.goto_calls[0][1] == 5000


def test_click_selector_is_clicked_before_reading(monkeypatch):
    fake_module, browser, page = _install_fake_playwright(monkeypatch)
    browser_tools.browse_webpage("https://example.com", click_selector="button.more")
    assert page.click_calls == [("button.more", 15000)]
    assert page.wait_calls == []


def test_wait_selector_used_when_no_click_selector(monkeypatch):
    fake_module, browser, page = _install_fake_playwright(monkeypatch)
    browser_tools.browse_webpage("https://example.com", wait_selector="#ready")
    assert page.wait_calls == [("#ready", 15000)]


def test_wait_selector_ignored_when_click_selector_given(monkeypatch):
    fake_module, browser, page = _install_fake_playwright(monkeypatch)
    browser_tools.browse_webpage(
        "https://example.com", click_selector="button.more", wait_selector="#ready"
    )
    assert page.click_calls == [("button.more", 15000)]
    assert page.wait_calls == []


def test_empty_body_reports_no_visible_text(monkeypatch):
    _install_fake_playwright(monkeypatch, page=FakePage(title="Blank", text=""))
    result = browser_tools.browse_webpage("https://example.com")
    assert result.startswith("Success:")
    assert "no visible text" in result


def test_long_text_is_truncated(monkeypatch):
    long_text = "x" * (browser_tools.MAX_TEXT_CHARS + 500)
    _install_fake_playwright(monkeypatch, page=FakePage(text=long_text))
    result = browser_tools.browse_webpage("https://example.com")
    assert "truncated" in result
    assert len(result) < len(long_text) + 200


def test_goto_timeout_reports_clear_error(monkeypatch):
    page = FakePage(raise_on_goto=FakeTimeoutError("timed out"))
    _install_fake_playwright(monkeypatch, page=page)
    result = browser_tools.browse_webpage("https://example.com", timeout=3)
    assert result.startswith("Error")
    assert "did not finish loading" in result
    assert "3s" in result


def test_click_selector_not_found_reports_clear_error(monkeypatch):
    page = FakePage(raise_on_click=FakeTimeoutError("timed out"))
    _install_fake_playwright(monkeypatch, page=page)
    result = browser_tools.browse_webpage("https://example.com", click_selector="button.missing")
    assert result.startswith("Error")
    assert "button.missing" in result


def test_browser_launch_failure_missing_binary_reports_install_hint(monkeypatch):
    _install_fake_playwright(
        monkeypatch,
        raise_on_launch=FakePlaywrightError("Executable doesn't exist at /path/to/chromium"),
    )
    result = browser_tools.browse_webpage("https://example.com")
    assert result.startswith("Error")
    assert "playwright install chromium" in result


def test_browser_is_closed_even_on_click_error(monkeypatch):
    page = FakePage(raise_on_click=FakeTimeoutError("timed out"))
    _, browser, page = _install_fake_playwright(monkeypatch, page=page)
    browser_tools.browse_webpage("https://example.com", click_selector="button.x")
    assert browser.closed is True


@pytest.mark.parametrize("scheme", ["ftp://x", "javascript:alert(1)", "not-a-url"])
def test_rejects_other_bad_schemes(scheme):
    result = browser_tools.browse_webpage(scheme)
    assert result.startswith("Error")


def test_eval_js_result_included_in_output(monkeypatch):
    page = FakePage(eval_result="dark")
    _install_fake_playwright(monkeypatch, page=page)
    result = browser_tools.browse_webpage(
        "https://example.com", eval_js="document.body.getAttribute('data-theme')"
    )
    assert result.startswith("Success:")
    assert "Eval result" in result
    assert "dark" in result
    assert page.evaluate_calls == ["document.body.getAttribute('data-theme')"]


def test_eval_js_not_called_when_omitted(monkeypatch):
    _, browser, page = _install_fake_playwright(monkeypatch)
    result = browser_tools.browse_webpage("https://example.com")
    assert page.evaluate_calls == []
    assert "Eval result" not in result


def test_eval_js_runs_after_click(monkeypatch):
    page = FakePage(eval_result=3)
    _install_fake_playwright(monkeypatch, page=page)
    browser_tools.browse_webpage(
        "https://example.com", click_selector="button.open", eval_js="document.querySelectorAll('.item').length"
    )
    assert page.click_calls == [("button.open", 15000)]
    assert page.evaluate_calls == ["document.querySelectorAll('.item').length"]


def test_eval_js_error_reported_without_failing_whole_call(monkeypatch):
    page = FakePage(raise_on_evaluate=FakePlaywrightError("SyntaxError: bad expression"))
    _install_fake_playwright(monkeypatch, page=page)
    result = browser_tools.browse_webpage("https://example.com", eval_js="???not valid js")
    assert result.startswith("Success:")
    assert "Eval error" in result
    assert "bad expression" in result
