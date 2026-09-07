"""Tests for the screenshot/image-viewing tools.

Two separate tools by design: capture_screenshot writes a PNG and returns
plain text (like every other tool); view_image reads one back and returns
(status_text, data_url) — the one tool whose result isn't just a string,
because a tool result can't carry image content on the wire (see
JFI.tool.image_tools's module docstring and runner.execute_tool_call).

capture_screenshot needs a real display, which isn't guaranteed in every CI
environment. Rather than skip those tests, they assert the function degrades
to a clean "Error: ..." string instead of crashing when no display/backend is
available, and additionally verify real capture behavior when it succeeds —
so the same test is meaningful with or without a display.
"""

import base64

import pytest


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield tmp_path


class TestCaptureScreenshot:
    def test_returns_success_or_clean_error_never_raises(self):
        from JFI.tool.image_tools import capture_screenshot

        result = capture_screenshot(".JFI/demo")
        assert isinstance(result, str)
        assert result.startswith("Success:") or result.startswith(("Error:", "Warning:"))

    def test_successful_capture_writes_a_real_png_and_auto_increments(self, tmp_path):
        from JFI.tool.image_tools import capture_screenshot

        first = capture_screenshot(".JFI/demo")
        if not first.startswith("Success:"):
            pytest.skip(f"no screenshot backend available here: {first!r}")

        second = capture_screenshot(".JFI/demo")
        assert second.startswith("Success:")

        png1 = tmp_path / ".JFI" / "demo" / "screen-1.png"
        png2 = tmp_path / ".JFI" / "demo" / "screen-2.png"
        assert png1.is_file() and png2.is_file()
        # PNG magic bytes.
        assert png1.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_missing_mss_returns_error_not_exception(self, monkeypatch):
        """Simulates the package genuinely not being installed."""
        import builtins
        import JFI.tool.image_tools as image_tools

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "mss":
                raise ImportError("No module named 'mss'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        result = image_tools.capture_screenshot(".JFI/demo")
        assert result.startswith("Error:")
        assert "mss" in result

    def test_rejects_path_outside_cwd(self):
        from JFI.tool.image_tools import capture_screenshot

        result = capture_screenshot("../outside")
        assert result.startswith("Error:")
        assert "Security Error" in result


class TestViewImage:
    def _write_png(self, tmp_path, name="pic.png", data=b"\x89PNG\r\n\x1a\n" + b"x" * 100):
        path = tmp_path / name
        path.write_bytes(data)
        return path

    def test_loads_and_returns_data_url(self, tmp_path):
        from JFI.tool.image_tools import view_image

        self._write_png(tmp_path)
        status, data_url = view_image("pic.png")

        assert status.startswith("Success:")
        assert data_url is not None
        assert data_url.startswith("data:image/png;base64,")
        encoded = data_url.split(",", 1)[1]
        assert base64.b64decode(encoded) == (tmp_path / "pic.png").read_bytes()

    def test_missing_file_returns_error_and_no_data_url(self):
        from JFI.tool.image_tools import view_image

        status, data_url = view_image("nope.png")
        assert status.startswith("Error:")
        assert "does not exist" in status
        assert data_url is None

    def test_unsupported_extension_rejected(self, tmp_path):
        from JFI.tool.image_tools import view_image

        (tmp_path / "notes.txt").write_text("hello")
        status, data_url = view_image("notes.txt")
        assert status.startswith("Error:")
        assert "unsupported image type" in status
        assert data_url is None

    def test_jpeg_and_webp_mime_types_recognized(self, tmp_path):
        from JFI.tool.image_tools import view_image

        (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 20)
        status, data_url = view_image("photo.jpg")
        assert data_url.startswith("data:image/jpeg;base64,")

        (tmp_path / "photo.webp").write_bytes(b"RIFF" + b"x" * 20)
        status, data_url = view_image("photo.webp")
        assert data_url.startswith("data:image/webp;base64,")

    def test_oversized_file_rejected(self, tmp_path, monkeypatch):
        import JFI.tool.image_tools as image_tools

        monkeypatch.setattr(image_tools, "MAX_IMAGE_BYTES", 10)
        self._write_png(tmp_path, data=b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        status, data_url = image_tools.view_image("pic.png")
        assert status.startswith("Error:")
        assert "over the" in status
        assert data_url is None

    def test_rejects_path_outside_cwd(self):
        from JFI.tool.image_tools import view_image

        status, data_url = view_image("../../../etc/passwd")
        assert status.startswith("Error:")
        assert "Security Error" in status
        assert data_url is None
