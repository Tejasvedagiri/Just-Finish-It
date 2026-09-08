"""Renders JFI console screenshots for every THEME preset into docs/images/.

The on-screen UI is prompt_toolkit, but the exact per-role colors are plain
style dictionaries (UI_STYLE_BASE + PT_THEME_PRESETS in
src/JFI/manager/pt_console_manager.py), so we import those directly and draw a
terminal-style frame with Pillow. ANSI names resolve against the classic
xterm-256color 16-color palette; preset overrides use literal hex and render
identically on every terminal, which is what makes each preset distinct in
the screenshots (dark presets get dark window backgrounds, light presets get
light ones).

Run:  .venv/bin/python docs/make_theme_screenshots.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from JFI.manager.pt_console_manager import (  # noqa: E402
    PT_THEME_PRESETS,
    UI_STYLE_BASE,
)

# xterm-256color base palette — what "ansi*" names resolve to on a typical terminal.
ANSI = {
    "black": "#000000", "red": "#cd0000", "green": "#00cd00", "yellow": "#cdcd00",
    "blue": "#0000ee", "magenta": "#cd00cd", "cyan": "#00cdcd", "white": "#e5e5e5",
    "ansibrightblack": "#7f7f7f", "ansibrightred": "#ff0000", "ansibrightgreen": "#00ff00",
    "ansibrightyellow": "#ffff55", "ansibrightblue": "#5c5cff", "ansibrightmagenta": "#ff00ff",
    "ansibrightcyan": "#00ffff", "ansibrightwhite": "#ffffff",
}

# Window background per preset family. dark-default is the ansi*-based baseline,
# so it shows on a classic near-black terminal; each other preset gets its own
# slightly different background so the screenshots read as distinct terminals.
BACKGROUNDS = {
    "dark-default": "#1d2025",
    "dark-ocean": "#141b26",
    "dark-mono": "#0f0f10",
    "light-default": "#ffffff",
    "light-sunrise": "#fff3e0",
    "light-paper": "#faf7ef",
}

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FONT_SIZE = 15


def _fonts() -> dict:
    return {
        "reg": ImageFont.truetype(FONT_PATH, FONT_SIZE),
        "bold": ImageFont.truetype(FONT_BOLD_PATH, FONT_SIZE),
    }


def parse_style(style_spec: str) -> tuple[str, bool]:
    """'bold ansicyan' -> ('#00cdcd', True)."""
    bold = False
    color_name = ""
    for tok in style_spec.split():
        if tok == "bold":
            bold = True
        else:
            color_name = tok
    hex_color = ANSI.get(color_name) or (color_name if color_name.startswith("#") else "#ffffff")
    return hex_color, bold


def measure(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


class Row:
    """A logical console line: list of (text, style_class_or_None)."""

    def __init__(self):
        self.parts: list[tuple[str, str | None]] = []


def build_content() -> list[Row]:
    """Sample content exercising every message-role class the UI uses."""
    def r(*parts):
        return _RowWith(list(parts))

    rows = [
        Row(),  # header, filled per theme below (colors are shared chrome)
        Row(),  # task line
        Row(),  # rule under the header area? No — content starts here.
        r(("🧑 you ▸ ", "out.user"),
         ("add a light/dark theme switch and document it", None)),
        r(("", None)),
        r(("🤖 Assistant", "out.assistant.tag")),
        r(("Done — the THEME env var now picks one of six presets, and I've", "out.assistant")),
        r(("captured screenshots for each. Docs are in `docs/Themes.md`.", "out.assistant")),
        r(("", None)),
        r(("⚙  Session log opened at JFI/readme/session-2025.log", "out.system")),
        r(("", None)),
        r(("🛠  preparing call: write_file", "out.tool")),
        r((" ⚙️ write_file  (file_path=docs/Themes.md, content=# Themes …)", "out.tool")),
        r(("    │ # Themes", "out.result")),
        r(("    │ JFI ships six presets — dark-default is the ANSI baseline.", "out.result")),
        r(("    └ … 12 more line(s)", "out.result")),
        r(("", None)),
        Row(),  # rule with centered label, filled below (needs width)
        r(("", None)),
        r(("✗  LLM stream ended without a finish reason — retrying in 5s", "out.error")),
        r(("", None)),
    ]
    return rows


class _RowWith(Row):
    def __init__(self, parts):
        self.parts = list(parts)


def render_theme(name: str, out_path: Path) -> None:
    overrides = PT_THEME_PRESETS[name]
    style_map = {k.replace("class:", ""): v for k, v in UI_STYLE_BASE.items()}
    style_map.update(overrides)

    # Resolve every class used by the content to (hex, bold).
    def resolve(cls: str | None):
        if cls is None:
            return parse_style(style_map["out.assistant"])
        spec = style_map.get(cls, "ansiwhite")
        return parse_style(spec)

    fonts = _fonts()
    draw_probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    char_w = measure(draw_probe, "M", fonts["reg"])
    line_h = int(FONT_SIZE * 1.45)

    # --- assemble rows ------------------------------------------------------
    content_rows: list[Row] = build_content()

    header_parts = [
        (" Just Finish It", "header"),
        ("  ·  ", "header.dim"),
        ("↓98.2k ↑12.3k", "header.tokens"),
        ("  ·  session: ", "header.dim"),
        ("theming-pass", "header"),
        ("  ·  ", "header.dim"),
        ("✓ planner › imp ✓ testing ▸ reviewer", "header.phase.done"),
    ]
    # Recolor the phase breadcrumb properly (mixed classes).
    header_parts = [
        (" Just Finish It", "header"),
        ("  ·  ", "header.dim"),
        ("↓98.2k ↑12.3k", "header.tokens"),
        ("  ·  session: ", "header.dim"),
        ("theming-pass", "header"),
        ("  ·  ✓ planner › imp ✓ testing ▸ reviewer", "header.phase.done"),
    ]
    content_rows[0].parts = header_parts

    task_row = [
        (" ▸ REVIEWER", "tasktitle.phase"),
        ("  ·  ", "tasktitle.dim"),
        ("3.3 Write docs/Themes.md with preset screenshots", "tasktitle"),
    ]
    content_rows[1].parts = task_row

    # Rule row with a centered label (index 2 in build_content).
    content_rows[2].parts = None  # sentinel: drawn as rule+label after width known

    status_text_left = " Queue_Size: (2)   ⚡1   "
    status_spinner = "⠦ reviewer · streaming"
    hint = ("   Enter queues for after review · !text runs now · PgUp/PgDn scroll · Ctrl+C stop")

    # --- size the canvas ----------------------------------------------------
    def row_width(row: Row | None) -> int:
        if row is None or getattr(row, "parts", None) is None:
            return 0
        w = 0
        for text, cls in row.parts:
            _, bold = resolve(cls)
            font = fonts["bold"] if bold else fonts["reg"]
            w += measure(draw_probe, text, font)
        return w

    status_w = (measure(draw_probe, status_text_left, fonts["reg"])
                + measure(draw_probe, status_spinner, fonts["bold"])
                + measure(draw_probe, hint, fonts["reg"]))
    rule_label = " theme: %s " % name
    rule_w = measure(draw_probe, rule_label, fonts["reg"])

    width_chars_px = max(row_width(r) for r in content_rows if r.parts is not None)
    width_chars_px = max(width_chars_px, status_w, rule_w + 40)

    pad_x, pad_y = 16, 12
    title_h = 30
    n_lines = len(content_rows) + 2  # content + rule separator above status
    img_w = width_chars_px + pad_x * 2 + 8
    img_h = title_h + pad_y + n_lines * line_h + 10 + line_h + pad_y

    bg = BACKGROUNDS[name]
    img = Image.new("RGB", (img_w, int(img_h)), "#ffffff")
    d = ImageDraw.Draw(img)

    # Terminal window: rounded rect background + title bar.
    term_bg = bg
    radius = 10
    d.rounded_rectangle([2, 2, img_w - 3, img_h - 3], radius=radius, fill=term_bg)
    chrome_fg = "#8b949e" if name.startswith("dark") else "#6a737d"
    d.line([(2 + radius // 2, title_h), (img_w - 3 - radius // 2, title_h)], fill=chrome_fg, width=1)
    for i in range(3):
        c = ["#ff5f56", "#ffbd2e", "#27c93f"][i]
        d.ellipse([14 + i * 20, 12, 26 + i * 20, 24], fill=c)

    x0 = pad_x
    y = title_h + pad_y

    def draw_row(row: Row | None, y: int):
        if row is None or getattr(row, "parts", None) is None:
            return
        x = x0
        for text, cls in row.parts:
            hex_color, bold = resolve(cls)
            font = fonts["bold"] if bold else fonts["reg"]
            d.text((x, y), text, font=font, fill=hex_color)
            x += measure(d, text, font)

    for i, row in enumerate(content_rows):
        draw_row(row, y + i * line_h)

    # Rule with centered label where the sentinel sits (row index 2).
    rule_y = y + 2 * line_h + line_h // 3
    rule_color, _ = resolve("out.rule") if False else parse_style(style_map["rule"])
    label_w = measure(d, rule_label, fonts["reg"])
    left_len = (img_w - pad_x * 2 - label_w) // 2
    d.text((x0 + left_len, y + 2 * line_h), rule_label, font=fonts["reg"], fill="#e5e5e5")
    d.line([(x0, rule_y), (x0 + left_len - 4, rule_y)], fill=rule_color, width=1)
    d.line([(x0 + left_len + label_w + 4, rule_y), (img_w - pad_x - 2, rule_y)], fill=rule_color, width=1)

    # Separator above the status line.
    sep_y = y + len(content_rows) * line_h + 6
    d.line([(x0, sep_y), (img_w - pad_x - 2, sep_y)], fill=rule_color, width=1)

    # Status line: neutral chrome on left, spinner in busy green, hint dim.
    sy = y + len(content_rows) * line_h + 14
    x = x0
    for text, cls, bold in [
        (status_text_left, "status", False),
        (status_spinner, "status.busy", True),
        (hint, "status", False),
    ]:
        hex_color, _b = resolve(cls)
        font = fonts["bold"] if bold else fonts["reg"]
        d.text((x, sy), text, font=font, fill=hex_color)
        x += measure(d, text, font)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    print(f"wrote {out_path} ({img.size[0]}x{img.size[1]})")


def main() -> None:
    images_dir = Path(__file__).resolve().parent / "images"
    for name in PT_THEME_PRESETS:
        render_theme(name, images_dir / f"theme-{name}.png")


if __name__ == "__main__":
    main()
