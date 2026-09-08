"""Captures REAL screenshots of the JFI TUI under each THEME preset.

Unlike docs/make_theme_screenshots.py (which draws a synthetic mockup from
the style dict), this spawns the actual `python -m JFI.runner` process on a
real pty, feeds its raw output into a real VT100 emulator (pyte), and
rasterizes the resulting screen buffer -- including real per-cell fg/bg
colors as prompt_toolkit actually painted them -- to a PNG with Pillow. This
is the same technique test/test_theme_background_fill.py uses to validate
theme rendering, just turned into a picture instead of an assertion. Useful
whenever a theme/contrast change needs visual proof rather than a synthetic
mockup.

Run:  uv run utils/capture_theme_screenshots.py
      uv run utils/capture_theme_screenshots.py --docs   # regenerate docs/images/theme-*.png,
                                                           # the files docs/Themes.md embeds
Output: screen_shots/theme-*.png (repo root) by default, or docs/images/ with --docs
(only the 10 named presets --docs cares about; --docs skips the auto/invalid-name
edge cases since docs/Themes.md doesn't reference those filenames).
"""
from __future__ import annotations

import argparse
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import pyte

REPO_ROOT = Path(__file__).resolve().parent.parent

ROWS, COLS = 30, 100
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FONT_SIZE = 16

def _preset_cases() -> "list[tuple[str | None, str, str]]":
    """One (theme, label, filename) case per PT_THEME_PRESETS entry, built
    from the live table rather than hand-maintained so a new preset is
    captured automatically. "dark-default" is listed first (it's the ANSI
    baseline other presets are compared against), the rest alphabetically."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from JFI.manager.pt_console_manager import PT_THEME_PRESETS

    names = sorted(PT_THEME_PRESETS, key=lambda n: (n != "dark-default", n))
    return [(name, f"THEME={name}", f"theme-{name}.png") for name in names]


# The full preset set exercised by pt_console_manager.PT_THEME_PRESETS, plus
# two edge cases (unset -> auto-detect, and an unknown name -> safe fallback).
CASES: "list[tuple[str | None, str, str]]" = [
    (None, "auto (THEME unset, honors .env)", "theme-auto-unset.png"),
    *_preset_cases(),
    ("bogus-theme-name", "THEME=bogus-theme-name (invalid -> should fall back)", "theme-invalid-fallback.png"),
]


def _capture_real_app_frame(
        repo_root: Path, theme: str | None, timeout: float = 3.0, docs_frame: bool = False,
) -> pyte.Screen:
    """Spawns a real process on a real pty and captures what it renders.

    ``docs_frame=False`` (the default) spawns the actual `python -m
    JFI.runner` — the most faithful proof for validating the live app (.env
    loading, auto-detection, fallback behavior), but it blocks on the
    session-name prompt, so the captured frame never shows an assistant
    reply, a tool call, or an error — no LLM is wired up to produce one.
    ``docs_frame=True`` instead spawns ``_docs_frame_app.py``, which renders
    one representative frame through the same real PromptToolkitConsoleManager
    without needing an LLM, so the screenshot actually demonstrates every
    message-role color — the point of the docs/Themes.md gallery.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    if theme is None:
        env.pop("THEME", None)
    else:
        env["THEME"] = theme
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["OPENAI_URL"] = "http://127.0.0.1:1/v1"
    env["OPENAI_API_KEY"] = "x"
    env["MODEL"] = "x"
    # Avoid COLORFGBG accidentally forcing a light-terminal auto-detect.
    env.pop("COLORFGBG", None)

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    argv = [sys.executable, str(Path(__file__).resolve().parent / "_docs_frame_app.py")] if docs_frame \
        else [sys.executable, "-m", "JFI.runner"]
    proc = subprocess.Popen(
        argv,
        stdin=slave, stdout=slave, stderr=slave,
        env=env, cwd=str(repo_root), preexec_fn=os.setsid,
    )
    os.close(slave)

    data = b""
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.2)
            if master in r:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                data += chunk
    finally:
        _kill(proc)
        try:
            os.close(master)
        except OSError:
            pass

    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    stream.feed(data.decode("utf-8", errors="replace"))
    return screen


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


# xterm-256color base 16-entry ANSI palette, for cells pyte reports by name
# (e.g. "brown", "white") rather than as a literal truecolor hex.
ANSI_NAMED = {
    "black": "#000000", "red": "#cd0000", "green": "#00cd00", "brown": "#cdcd00",
    "blue": "#0000ee", "magenta": "#cd00cd", "cyan": "#00cdcd", "white": "#e5e5e5",
    "brightblack": "#7f7f7f", "brightred": "#ff0000", "brightgreen": "#00ff00",
    "brightbrown": "#ffff55", "brightblue": "#5c5cff", "brightmagenta": "#ff00ff",
    "brightcyan": "#00ffff", "brightwhite": "#ffffff",
}


def _resolve_color(value: str, default: str) -> str:
    if value in (None, "default"):
        return default
    if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
        return f"#{value}"
    return ANSI_NAMED.get(value, default)


def render_screen_to_png(screen: pyte.Screen, out_path: Path, theme_label: str) -> None:
    fonts = {
        "reg": ImageFont.truetype(FONT_PATH, FONT_SIZE),
        "bold": ImageFont.truetype(FONT_BOLD_PATH, FONT_SIZE),
    }
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    bbox = probe.textbbox((0, 0), "M", font=fonts["reg"])
    cell_w = bbox[2] - bbox[0]
    cell_h = FONT_SIZE + 6
    margin = 12
    caption_h = 22

    default_bg = "#1d2025"
    default_fg = "#d0d0d0"

    width = margin * 2 + COLS * cell_w
    height = margin * 2 + ROWS * cell_h + caption_h

    img = Image.new("RGB", (width, height), default_bg)
    draw = ImageDraw.Draw(img)

    # First pass: paint each cell's background, matching how a real terminal
    # composites cell backgrounds independent of glyph rendering.
    for y in range(ROWS):
        row = screen.buffer[y]
        for x in range(COLS):
            cell = row[x]
            bg = _resolve_color(cell.bg, default_bg)
            if bg != default_bg:
                px = margin + x * cell_w
                py = margin + caption_h + y * cell_h
                draw.rectangle([px, py, px + cell_w, py + cell_h], fill=bg)

    for y in range(ROWS):
        row = screen.buffer[y]
        for x in range(COLS):
            cell = row[x]
            if not cell.data or cell.data == " ":
                continue
            bg = _resolve_color(cell.bg, default_bg)
            fg = _resolve_color(cell.fg, default_fg)
            if cell.reverse:
                fg, bg = bg, fg
            font = fonts["bold"] if cell.bold else fonts["reg"]
            px = margin + x * cell_w
            py = margin + caption_h + y * cell_h
            draw.text((px, py), cell.data, font=font, fill=fg)

    draw.text((margin, 2), theme_label, font=fonts["bold"], fill="#e5e5e5")
    img.save(out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--docs", action="store_true",
        help="Write every named preset into docs/images/, matching the filenames "
             "docs/Themes.md embeds (skips the auto/invalid-name edge cases).",
    )
    parser.add_argument(
        "--out-dir", default=None,
        help="Explicit output directory, overriding --docs / the screen_shots/ default.",
    )
    args = parser.parse_args()

    if args.out_dir:
        out_dir = Path(args.out_dir)
    elif args.docs:
        out_dir = REPO_ROOT / "docs" / "images"
    else:
        out_dir = REPO_ROOT / "screen_shots"
    out_dir.mkdir(parents=True, exist_ok=True)

    cases = CASES
    if args.docs and not args.out_dir:
        # docs/Themes.md only embeds the 10 named presets, not the auto-detect
        # or invalid-name edge cases screen_shots/ also captures for proof.
        cases = [c for c in CASES if c[0] not in (None, "bogus-theme-name")]

    results = []
    for theme, label, filename in cases:
        print(f"Capturing theme={theme!r} ...", flush=True)
        screen = _capture_real_app_frame(REPO_ROOT, theme, docs_frame=args.docs)
        out_path = out_dir / filename
        render_screen_to_png(screen, out_path, label)

        bgs = sorted({screen.buffer[y][x].bg for y in range(ROWS) for x in range(COLS)})
        text_lines = []
        for y in range(min(6, ROWS)):
            line = "".join(screen.buffer[y][x].data or " " for x in range(COLS)).rstrip()
            text_lines.append(line)
        results.append((theme, filename, bgs, text_lines))
        print(f"  -> {out_path} (bg colors seen: {bgs})")

    print("\n=== Summary ===")
    for theme, filename, bgs, text_lines in results:
        print(f"\ntheme={theme!r} file={filename}")
        print(f"  distinct cell backgrounds: {bgs}")
        for line in text_lines:
            if line.strip():
                print(f"  | {line}")


if __name__ == "__main__":
    main()
