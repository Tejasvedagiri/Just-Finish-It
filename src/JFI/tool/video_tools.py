"""Video -> unique-screenshot extraction tool.

``extract_video_frames`` shells out to the system ``ffmpeg`` binary (no new
Python dependency -- consistent with this project's minimal-dependency
approach elsewhere, e.g. fetch_webpage_images's own comment on that). Like
capture_screenshot and fetch_webpage_images, it only writes PNGs to disk and
returns plain text -- it does NOT put image bytes in front of the model.
Call ``view_image`` on the returned paths afterward to actually see them.

Two ffmpeg passes, deliberately not one:

1. ``_find_distinct_frame_indices`` decodes the video once, downscaled to a
   tiny rgb24 thumbnail stream, and computes an actual "fraction of pixels
   that visibly changed" between each frame and the last KEPT frame, in pure
   Python. ffmpeg's own built-in scene-change filter was tried first and
   rejected: its "scene" score is not a real percentage -- a hard cut
   between two solid colors (about as different as two frames can be) only
   ever scored ~0.4 in testing, so a threshold meant to mean "50% different"
   would silently behave like "any change at all".
2. A second, cheap ffmpeg call re-decodes just those exact frame indices at
   full resolution into PNG files (ffmpeg's `select` filter with an
   eq(n,i)-per-index expression), since the analysis pass only ever produced
   tiny thumbnails.

Comparing each candidate against the last KEPT frame (not simply the
previous frame) is what catches a slow pan/fade eventually crossing the
threshold even though no single frame-to-frame step did.
"""

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from JFI.tool.file_tools import _get_safe_path

DEFAULT_MAX_FRAMES = 12
# Hard ceiling regardless of what the model asks for -- a runaway argument
# (typo'd or otherwise) must not turn into an enormous frame dump.
MAX_FRAMES_CAP = 40

# Fraction of the (downscaled) frame's pixels that must have visibly changed
# relative to the last kept frame before a new frame is kept.
DEFAULT_THRESHOLD = 0.5
MIN_THRESHOLD = 0.05
MAX_THRESHOLD = 0.95

DEFAULT_TIMEOUT_SECONDS = 300

# Downscaled thumbnail used only for frame-difference analysis -- small
# enough that comparing two thumbnails is cheap in pure Python (no PIL/numpy
# dependency), coarse enough that decoder noise/compression artifacts don't
# masquerade as a "changed" pixel.
_THUMB_SIZE = 24
_THUMB_PIXELS = _THUMB_SIZE * _THUMB_SIZE
_THUMB_FRAME_BYTES = _THUMB_PIXELS * 3  # rgb24

# A pixel counts as "changed" once any of its R/G/B channels moves by more
# than this much (out of 255) -- filters out sub-visible dithering/
# compression noise from counting as a real difference.
_PIXEL_DELTA = 30


def _percent_different(a: bytes, b: bytes) -> float:
    """Fraction of thumbnail pixels where any RGB channel moved by more than
    _PIXEL_DELTA, between two same-sized rgb24 thumbnails."""
    changed = 0
    for i in range(0, len(a), 3):
        if (abs(a[i] - b[i]) > _PIXEL_DELTA
                or abs(a[i + 1] - b[i + 1]) > _PIXEL_DELTA
                or abs(a[i + 2] - b[i + 2]) > _PIXEL_DELTA):
            changed += 1
    return changed / _THUMB_PIXELS


def _find_distinct_frame_indices(
    in_path: Path, max_frames: int, threshold: float, deadline: float
) -> Tuple[List[int], Optional[str]]:
    """
    Decodes `in_path` once, downscaled to a tiny rgb24 thumbnail stream, and
    returns the 0-based decode-order indices worth keeping: frame 0, plus
    every later frame that differs from the last KEPT frame's thumbnail by
    at least `threshold` (fraction of pixels changed) -- capped at
    `max_frames`. Stops reading (and kills ffmpeg) once either the cap or
    `deadline` (a time.monotonic() value) is hit.

    Returns (indices, error) -- error is set only when ffmpeg produced
    nothing at all (a real failure, not just a short/static video).
    """
    command = [
        "ffmpeg", "-i", str(in_path),
        "-vf", f"scale={_THUMB_SIZE}:{_THUMB_SIZE}:flags=fast_bilinear,format=rgb24",
        "-f", "rawvideo",
        "-",
    ]
    try:
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL
        )
    except Exception as e:
        return [], f"Error running ffmpeg on {in_path}: {e}"

    # ffmpeg writes progress/diagnostics to stderr continuously; if that pipe
    # fills up while we're only reading stdout, ffmpeg blocks on the stderr
    # write and we deadlock waiting on stdout. Drain it on a side thread.
    stderr_chunks: List[bytes] = []
    stderr_thread = threading.Thread(target=lambda: stderr_chunks.extend(iter(proc.stderr.readline, b"")))
    stderr_thread.daemon = True
    stderr_thread.start()

    indices: List[int] = []
    last_kept: Optional[bytes] = None
    frames_seen = 0
    timed_out = False
    hit_cap = False
    try:
        while True:
            if time.monotonic() > deadline:
                timed_out = True
                break
            chunk = proc.stdout.read(_THUMB_FRAME_BYTES)
            if len(chunk) < _THUMB_FRAME_BYTES:
                break
            if last_kept is None or _percent_different(chunk, last_kept) >= threshold:
                indices.append(frames_seen)
                last_kept = chunk
                if len(indices) >= max_frames:
                    hit_cap = True
                    break
            frames_seen += 1
    finally:
        proc.kill()
        proc.wait()
        stderr_thread.join(timeout=2)

    if timed_out:
        return [], f"ffmpeg analysis timed out after {deadline:.0f}s on {in_path}."

    # A non-zero exit only means something when it happened before we
    # deliberately killed ffmpeg (hit_cap) -- killing a still-running decode
    # always produces a non-zero/"killed" code, which is expected, not a
    # failure.
    if not indices and not hit_cap and proc.returncode not in (0, None):
        stderr_text = b"".join(stderr_chunks).decode(errors="replace").strip()
        tail = "\n".join(stderr_text.splitlines()[-10:])
        return [], f"Error: ffmpeg failed on {in_path} (exit {proc.returncode}).\n\n{tail}"

    return indices, None


def extract_video_frames(
    video_path: str,
    directory: str,
    max_frames: int = DEFAULT_MAX_FRAMES,
    threshold: float = DEFAULT_THRESHOLD,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """
    Extracts up to `max_frames` visually distinct frames from `video_path`
    and saves them as auto-numbered `frame-N.png` files inside `directory` --
    a small set of unique screenshots instead of every frame.

    A frame is kept when the fraction of its pixels that visibly changed
    relative to the last KEPT frame reaches `threshold` (0.05-0.95, default
    0.5 -- i.e. at least 50% of the frame looks different). The very first
    frame is always kept. Lower the threshold to catch subtler changes (more
    frames); raise it to keep only the biggest cuts (fewer frames). If you
    get just 1 frame back for a video that clearly changes, retry with a
    lower threshold; if you get too many near-duplicates, retry higher.

    Does NOT show you any image -- like capture_screenshot, it only writes
    files and reports where. Call view_image on one of the returned paths
    afterward to actually see it.
    """
    if shutil.which("ffmpeg") is None:
        return (
            "Error: video frame extraction requires the 'ffmpeg' binary, which is not "
            "installed (or not on PATH) in this environment. Skip this step, note the "
            "blocker in the plan, and continue."
        )

    try:
        in_path = _get_safe_path(video_path)
    except (PermissionError, ValueError) as e:
        return f"Error: {e}"
    if not in_path.exists():
        return f"Error: File {video_path} does not exist."
    if not in_path.is_file():
        return f"Error: {video_path} is not a file."

    try:
        out_dir = _get_safe_path(directory)
    except (PermissionError, ValueError) as e:
        return f"Error: {e}"
    out_dir.mkdir(parents=True, exist_ok=True)

    max_frames = max(1, min(int(max_frames), MAX_FRAMES_CAP))
    threshold = max(MIN_THRESHOLD, min(float(threshold), MAX_THRESHOLD))

    deadline = time.monotonic() + timeout
    indices, error = _find_distinct_frame_indices(in_path, max_frames, threshold, deadline)
    if error:
        return error
    if not indices:
        return f"Error: no frames could be decoded from {video_path}. Is this a valid video file?"

    existing = [
        int(m.group(1))
        for p in out_dir.glob("frame-*.png")
        if (m := re.match(r"^frame-(\d+)\.png$", p.name))
    ]
    start_number = max(existing, default=0) + 1

    # Commas inside the select expression must be backslash-escaped: an
    # unescaped comma is the filtergraph's own filter separator.
    select_expr = "+".join(f"eq(n\\,{i})" for i in indices)
    out_pattern = out_dir / "frame-%03d.png"

    command = [
        "ffmpeg", "-y",
        "-i", str(in_path),
        "-vf", f"select='{select_expr}'",
        "-vsync", "vfr",
        "-start_number", str(start_number),
        str(out_pattern),
    ]

    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=max(1, deadline - time.monotonic()) if deadline > time.monotonic() else timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return f"Error: ffmpeg timed out extracting frames from {video_path}."
    except Exception as e:
        return f"Error running ffmpeg on {video_path}: {e}"

    produced: List[Path] = sorted(
        p for p in out_dir.glob("frame-*.png")
        if (m := re.match(r"^frame-(\d+)\.png$", p.name)) and int(m.group(1)) >= start_number
    )

    if result.returncode != 0 and not produced:
        stderr_tail = "\n".join(result.stderr.strip().splitlines()[-10:])
        return f"Error: ffmpeg failed extracting frames from {video_path} (exit {result.returncode}).\n\n{stderr_tail}"
    if not produced:
        return f"Error: expected {len(indices)} frame(s) but ffmpeg wrote none for {video_path}."

    lines = [
        f"Success: extracted {len(produced)} unique frame(s) from {video_path} "
        f"(threshold={threshold:.2f} = {threshold * 100:.0f}% of pixels changed):"
    ]
    for p in produced:
        lines.append(f"  {p}")
    if len(produced) >= max_frames:
        lines.append(
            f"(hit the max_frames={max_frames} cap -- there may be more distinct frames; "
            "call again with a higher max_frames, or a higher threshold to see fewer, "
            "coarser ones.)"
        )
    elif len(produced) == 1:
        lines.append(
            "(only the first frame was kept -- nothing else crossed the threshold. "
            "AUTO-RECTIFY: retry with a lower threshold to catch subtler changes.)"
        )
    lines.append("Call view_image on one of the paths above to actually see it.")
    return "\n".join(lines)
