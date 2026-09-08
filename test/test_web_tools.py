"""Tests for fetch_webpage_images (JFI.tool.web_tools).

Like capture_screenshot, this tool never returns image bytes directly — it
downloads files to disk and returns plain text; view_image is the separate
tool that actually attaches an image to the conversation. No real network
calls are made: httpx.Client is monkeypatched to a fake that serves
responses from a dict keyed by URL, so these tests are fast and offline.
"""

import httpx
import pytest

import JFI.tool.web_tools as web_tools


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield tmp_path


class FakeResponse:
    def __init__(self, status_code=200, headers=None, text="", content=None, url=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.content = content if content is not None else text.encode()
        self.url = url or ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", str(self.url) or "http://example.com"),
                response=self,
            )


class FakeClient:
    """Stand-in for httpx.Client: serves canned responses by URL, raising
    whatever exception a mapping points at instead of a response."""

    def __init__(self, responses):
        self._responses = responses

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url):
        resp = self._responses.get(url)
        if resp is None:
            raise httpx.ConnectError(f"no fake response configured for {url}")
        if isinstance(resp, BaseException):
            raise resp
        return resp


def _install_fake_client(monkeypatch, responses):
    monkeypatch.setattr(web_tools.httpx, "Client", lambda *a, **k: FakeClient(responses))


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 50
JPEG_BYTES = b"\xff\xd8\xff" + b"x" * 50


class TestUrlValidation:
    def test_rejects_non_http_scheme(self):
        result = web_tools.fetch_webpage_images("ftp://example.com", ".JFI/demo")
        assert result.startswith("Error:")
        assert "scheme" in result

    def test_rejects_file_scheme(self):
        """A file:// URL must not be usable to read local files off disk."""
        result = web_tools.fetch_webpage_images("file:///etc/passwd", ".JFI/demo")
        assert result.startswith("Error:")
        assert "scheme" in result

    def test_rejects_directory_outside_cwd(self):
        result = web_tools.fetch_webpage_images("http://example.com", "../outside")
        assert result.startswith("Error:")
        assert "Security Error" in result


class TestFetchErrors:
    def test_page_fetch_http_error(self, monkeypatch):
        _install_fake_client(monkeypatch, {
            "http://example.com/": FakeResponse(status_code=404, url="http://example.com/"),
        })
        result = web_tools.fetch_webpage_images("http://example.com/", ".JFI/demo")
        assert result.startswith("Error:")
        assert "404" in result

    def test_page_fetch_connection_error(self, monkeypatch):
        _install_fake_client(monkeypatch, {})  # no entry -> ConnectError
        result = web_tools.fetch_webpage_images("http://example.com/", ".JFI/demo")
        assert result.startswith("Error fetching")

    def test_non_html_content_type_rejected(self, monkeypatch):
        _install_fake_client(monkeypatch, {
            "http://example.com/data.json": FakeResponse(
                headers={"content-type": "application/json"}, text="{}",
                url="http://example.com/data.json",
            ),
        })
        result = web_tools.fetch_webpage_images("http://example.com/data.json", ".JFI/demo")
        assert result.startswith("Error:")
        assert "not HTML" in result

    def test_no_images_found(self, monkeypatch):
        _install_fake_client(monkeypatch, {
            "http://example.com/": FakeResponse(
                headers={"content-type": "text/html"}, text="<html><body>hi</body></html>",
                url="http://example.com/",
            ),
        })
        result = web_tools.fetch_webpage_images("http://example.com/", ".JFI/demo")
        assert result == "No images found on http://example.com/."


class TestDownloading:
    def test_downloads_images_in_priority_order(self, tmp_path, monkeypatch):
        """og:image wins priority over plain <img> tags, and results are
        de-duplicated and resolved to absolute URLs against the page URL."""
        html = """
        <html><head>
        <meta property="og:image" content="/hero.png">
        </head><body>
        <img src="/hero.png">
        <img src="https://cdn.example.com/photo.jpg">
        </body></html>
        """
        _install_fake_client(monkeypatch, {
            "http://example.com/page": FakeResponse(
                headers={"content-type": "text/html; charset=utf-8"}, text=html,
                url="http://example.com/page",
            ),
            "http://example.com/hero.png": FakeResponse(
                headers={"content-type": "image/png"}, content=PNG_BYTES,
            ),
            "https://cdn.example.com/photo.jpg": FakeResponse(
                headers={"content-type": "image/jpeg"}, content=JPEG_BYTES,
            ),
        })

        result = web_tools.fetch_webpage_images("http://example.com/page", ".JFI/demo")
        assert result.startswith("Success: downloaded 2 image(s)")

        out_dir = tmp_path / ".JFI" / "demo"
        first, second = out_dir / "web-1.png", out_dir / "web-2.jpg"
        assert first.is_file() and second.is_file()
        assert first.read_bytes() == PNG_BYTES
        assert second.read_bytes() == JPEG_BYTES
        # og:image de-duplicated against the identical <img src>: only one
        # download for /hero.png, not two.
        assert "http://example.com/hero.png" in result
        assert result.count("hero.png") == 1

    def test_relative_urls_resolved_against_final_response_url(self, monkeypatch):
        """Uses response.url (post-redirect), not the requested url, as the
        base for resolving relative <img src> values."""
        html = '<img src="pic.png">'
        _install_fake_client(monkeypatch, {
            "http://example.com/page": FakeResponse(
                headers={"content-type": "text/html"}, text=html,
                url="http://example.com/blog/page",  # redirected into /blog/
            ),
            "http://example.com/blog/pic.png": FakeResponse(
                headers={"content-type": "image/png"}, content=PNG_BYTES,
            ),
        })
        result = web_tools.fetch_webpage_images("http://example.com/page", ".JFI/demo")
        assert result.startswith("Success:")
        assert "http://example.com/blog/pic.png" in result

    def test_respects_max_images_cap(self, monkeypatch):
        html = "".join(f'<img src="/img{i}.png">' for i in range(10))
        responses = {
            "http://example.com/": FakeResponse(
                headers={"content-type": "text/html"}, text=html, url="http://example.com/",
            ),
        }
        for i in range(10):
            responses[f"http://example.com/img{i}.png"] = FakeResponse(
                headers={"content-type": "image/png"}, content=PNG_BYTES,
            )
        _install_fake_client(monkeypatch, responses)

        result = web_tools.fetch_webpage_images("http://example.com/", ".JFI/demo", max_images=3)
        assert result.startswith("Success: downloaded 3 image(s)")

    def test_max_images_hard_capped_regardless_of_argument(self, monkeypatch):
        html = "".join(f'<img src="/img{i}.png">' for i in range(30))
        responses = {
            "http://example.com/": FakeResponse(
                headers={"content-type": "text/html"}, text=html, url="http://example.com/",
            ),
        }
        for i in range(30):
            responses[f"http://example.com/img{i}.png"] = FakeResponse(
                headers={"content-type": "image/png"}, content=PNG_BYTES,
            )
        _install_fake_client(monkeypatch, responses)

        result = web_tools.fetch_webpage_images("http://example.com/", ".JFI/demo", max_images=999)
        assert result.startswith(f"Success: downloaded {web_tools.MAX_IMAGES_CAP} image(s)")

    def test_oversized_image_skipped(self, monkeypatch):
        html = '<img src="/big.png"><img src="/small.png">'
        _install_fake_client(monkeypatch, {
            "http://example.com/": FakeResponse(
                headers={"content-type": "text/html"}, text=html, url="http://example.com/",
            ),
            "http://example.com/big.png": FakeResponse(
                headers={"content-type": "image/png"}, content=b"x" * (web_tools.MAX_IMAGE_BYTES + 1),
            ),
            "http://example.com/small.png": FakeResponse(
                headers={"content-type": "image/png"}, content=PNG_BYTES,
            ),
        })
        result = web_tools.fetch_webpage_images("http://example.com/", ".JFI/demo")
        assert result.startswith("Success: downloaded 1 image(s)")
        assert "small.png" in result
        assert "skipped" in result

    def test_unrecognized_image_type_skipped(self, monkeypatch):
        html = '<img src="/vector.svg"><img src="/photo.jpg">'
        _install_fake_client(monkeypatch, {
            "http://example.com/": FakeResponse(
                headers={"content-type": "text/html"}, text=html, url="http://example.com/",
            ),
            "http://example.com/vector.svg": FakeResponse(
                headers={"content-type": "image/svg+xml"}, content=b"<svg></svg>",
            ),
            "http://example.com/photo.jpg": FakeResponse(
                headers={"content-type": "image/jpeg"}, content=JPEG_BYTES,
            ),
        })
        result = web_tools.fetch_webpage_images("http://example.com/", ".JFI/demo")
        assert result.startswith("Success: downloaded 1 image(s)")
        assert "photo.jpg" in result

    def test_all_downloads_fail_returns_error(self, monkeypatch):
        html = '<img src="/broken.png">'
        _install_fake_client(monkeypatch, {
            "http://example.com/": FakeResponse(
                headers={"content-type": "text/html"}, text=html, url="http://example.com/",
            ),
            "http://example.com/broken.png": FakeResponse(status_code=500, url="http://example.com/broken.png"),
        })
        result = web_tools.fetch_webpage_images("http://example.com/", ".JFI/demo")
        assert result.startswith("Error:")
        assert "none could be downloaded" in result

    def test_auto_increments_across_calls(self, tmp_path, monkeypatch):
        html = '<img src="/pic.png">'
        _install_fake_client(monkeypatch, {
            "http://example.com/": FakeResponse(
                headers={"content-type": "text/html"}, text=html, url="http://example.com/",
            ),
            "http://example.com/pic.png": FakeResponse(
                headers={"content-type": "image/png"}, content=PNG_BYTES,
            ),
        })
        web_tools.fetch_webpage_images("http://example.com/", ".JFI/demo")
        web_tools.fetch_webpage_images("http://example.com/", ".JFI/demo")

        out_dir = tmp_path / ".JFI" / "demo"
        assert (out_dir / "web-1.png").is_file()
        assert (out_dir / "web-2.png").is_file()
