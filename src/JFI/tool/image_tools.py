"""Screenshot capture and image viewing tools.

Two separate tools, deliberately not one:

- ``capture_screenshot`` writes a PNG to disk and returns plain text (like
  every other tool) — it does NOT put image bytes in front of the model.
- ``view_image`` reads a PNG/JPEG/etc. already on disk, base64-encodes it,
  and hands back ``(status_text, data_url)``. The status text is the normal
  tool result; the data URL is not — a tool result is always plain text on
  the wire (``role: "tool"`` content must be a string), so there is no way
  for a tool call by itself to make the model *see* an image. The runner
  special-cases this one tool: when it gets a non-None second value back, it
  appends a follow-up ``role: "user"`` message with an image content part
  right after the tool result, which is what actually puts the image in
  front of the model on its next turn.
"""

import base64
import re
from typing import Optional

from JFI.tool.file_tools import _get_safe_path

# Generous but bounded: some OpenAI-compatible servers reject oversized
# request bodies outright, and an unbounded screenshot/photo could otherwise
# blow past that silently with a confusing downstream error.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


def capture_screenshot(directory: str) -> str:
    """
    Captures the primary monitor and saves it as an auto-numbered
    ``screen-N.png`` inside `directory` (pass your session's ``JFI/<session>``
    folder — the same one your plan file lives in — so screenshots land next
    to everything else from this run). Returns where it landed; use
    ``view_image`` on that path afterward to actually look at it.
    """
    try:
        import mss
    except ImportError:
        return (
            "Error: screenshot capture requires the 'mss' package, which is not installed "
            "in this environment. Skip this step, note the blocker in the plan, and continue."
        )

    try:
        path = _get_safe_path(directory)
        path.mkdir(parents=True, exist_ok=True)

        existing = [
            int(m.group(1))
            for p in path.glob("screen-*.png")
            if (m := re.match(r"^screen-(\d+)\.png$", p.name))
        ]
        out_path = path / f"screen-{max(existing, default=0) + 1}.png"

        with mss.MSS() as sct:
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(monitor)
            mss.tools.to_png(shot.rgb, shot.size, output=str(out_path))

        size = out_path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            return (
                f"Warning: captured {out_path} ({shot.size[0]}x{shot.size[1]}, {size} bytes) but it "
                f"exceeds the {MAX_IMAGE_BYTES}-byte limit view_image enforces — view_image on it will "
                "fail. The file is still on disk if you need it another way."
            )
        return f"Success: captured screenshot ({shot.size[0]}x{shot.size[1]}) to {out_path}"
    except PermissionError as e:
        return f"Error: {e}"
    except Exception as e:
        return (
            f"Error capturing screenshot: {e}. This usually means no display is available "
            "(a headless/CI environment) — skip this step and continue without a screenshot."
        )


def view_image(file_path: str) -> tuple[str, Optional[str]]:
    """
    Reads an image file (png/jpg/jpeg/gif/webp) and attaches it to the
    conversation so you can actually see it on your next turn.

    Returns (status_text, data_url_or_None); the runner appends the image as
    a follow-up message only when a data_url comes back.
    """
    try:
        path = _get_safe_path(file_path)
    except PermissionError as e:
        return f"Error: {e}", None

    if not path.exists():
        return f"Error: File {file_path} does not exist.", None

    ext = path.suffix.lower().lstrip(".")
    mime = _MIME_BY_EXT.get(ext)
    if mime is None:
        return (
            f"Error: unsupported image type '.{ext}'. Supported: "
            f"{', '.join(sorted(_MIME_BY_EXT))}.",
            None,
        )

    try:
        data = path.read_bytes()
    except Exception as e:
        return f"Error reading {file_path}: {e}", None

    if len(data) > MAX_IMAGE_BYTES:
        return (
            f"Error: {file_path} is {len(data)} bytes, over the {MAX_IMAGE_BYTES}-byte limit. "
            "Too large to attach.",
            None,
        )

    b64 = base64.b64encode(data).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    return f"Success: loaded {file_path} ({len(data)} bytes) — attached as an image below.", data_url
