"""The interactive `browser` tool (JFI.tool.browser_session), against a real
headless Chromium and a real local page. check_page could only report a page
once; a UI flow -- type into a form, click, see what changed -- needs one
page kept open across calls."""
import base64
import http.server
import threading

import pytest

from JFI.tool import browser_session
from JFI.tool.browser_session import browser, close_browser

pytest.importorskip("playwright.sync_api")

PAGE = b"""<!doctype html><title>Greeter</title>
<input id="name" placeholder="Your name">
<button onclick="greet()">Greet</button>
<button onclick="console.error('boom')">Break</button>
<a href="/second">Second page</a>
<p id="out"></p>
<script>function greet() { document.getElementById('out').textContent = 'Hello, ' + document.getElementById('name').value; }</script>"""


@pytest.fixture
def site():
    class Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGE if self.path == "/" else b"<title>Second</title><p>second page</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    close_browser()
    server.shutdown()


def _launchable(result: str) -> None:
    if result.startswith("Error: Chromium's browser binary is not installed"):
        pytest.skip("Chromium isn't installed for Playwright here")


def test_a_ui_flow_across_calls(site):
    opened = browser("open", url=site + "/")
    _launchable(opened)
    assert opened.startswith(f"{site}/ -- Greeter")
    assert '[1] input[text] "Your name"' in opened and '[2] button "Greet"' in opened

    browser("type", target="1", text="Ada")
    clicked = browser("click", target="Greet")
    assert 'value="Ada"' in clicked
    assert "Hello, Ada" in browser("text")

    broken = browser("click", target="3")
    assert "console error: boom" in broken, "problems since the last call are reported with the action"
    assert "console error" not in browser("find", text="greet"), "and only once"
    assert browser("find", text="greet").count("[") == 1

    text, image = browser("screenshot")
    assert "Screenshot attached" in text and image.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(image.split(",", 1)[1])[:2] == b"\xff\xd8"

    assert browser("click", target="Second page").startswith(f"{site}/second -- Second")
    assert browser("back").startswith(f"{site}/ -- Greeter")


def test_mistakes_get_an_error_that_says_what_to_do(site):
    close_browser()
    assert browser("click", target="1").startswith('Error: no page is open. Call browser(action="open"')
    assert browser("open", url="ftp://example.com/").startswith("Error: only http, https and file URLs")
    _launchable(browser("open", url=site + "/"))
    assert browser("click", target="").startswith("Error: target is required")
    assert browser("press").startswith("Error: key is required")
    assert browser("fly").startswith("Error: unknown action 'fly'")
    assert browser("close") == "Closed the browser." and browser_session._SESSION is not None
    assert browser("text").startswith("Error: no page is open")


def test_a_static_page_opens_as_a_file_url(tmp_path):
    """Observed on the react_counter run: the reviewer's check_page on the
    project's file:///.../index.html was refused, so it started a throwaway
    http.server just to look at the page."""
    from JFI.tool.browser_tools import check_page

    page = tmp_path / "index.html"
    page.write_text(PAGE.decode(), encoding="utf-8")
    try:
        opened = browser("open", url=page.as_uri())
        _launchable(opened)
        assert opened.startswith(page.as_uri()) and '[2] button "Greet"' in opened
        checked = check_page(page.as_uri(), screenshot_dir=str(tmp_path / "screens"))
        assert checked.startswith(f"{page.as_uri()} -- Greeter") and "OK: no console errors" in checked
    finally:
        close_browser()
