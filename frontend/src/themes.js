// The SAME 20 THEME presets JFI's own terminal console supports (see
// src/JFI/manager/pt_console_manager.py's PT_THEME_PRESETS) -- ported here
// so picking a theme in the fleet UI matches what you'd see running `./JFI`
// with the same THEME env var. Each preset there only defines 5 raw roles
// (background, foreground, "user" accent, "assistant" accent, "assistant
// tag" accent, "system"/muted text) -- deriveTokens() below expands that
// into this app's full CSS custom-property set formulaically, the same way
// a terminal's 5-8 ANSI-ish roles stand in for a whole UI's palette.
//
// Keep in sync with pt_console_manager.py's PT_THEME_PRESETS by hand if
// that list changes -- there is no shared source file between the Python
// terminal and this JS frontend to generate both from.

export const THEME_PRESETS = {
  "dark-default": { bg: "#0a0e0c", fg: "#d7f5df", user: "#5fafff", assistant: "#35e37a", tag: "#f5b942", system: "#7fa88c" },
  "dark-ocean": { bg: "#141b26", fg: "#c8d6e5", user: "#5fafff", assistant: "#00afaf", tag: "#e0af68", system: "#5f5fbe" },
  "dark-mono": { bg: "#1a1a1a", fg: "#d0d0d0", user: "#ffffff", assistant: "#a0a0a0", tag: "#f5f5f5", system: "#808080" },
  "light-default": { bg: "#ffffff", fg: "#1a1a1a", user: "#0000ff", assistant: "#006400", tag: "#4b0082", system: "#444444" },
  "light-sunrise": { bg: "#fdf3e0", fg: "#4a3728", user: "#af00af", assistant: "#870000", tag: "#006064", system: "#87875f" },
  "light-paper": { bg: "#f7f3ec", fg: "#3a3a34", user: "#00005f", assistant: "#5f8767", tag: "#a0522d", system: "#808080" },
  "catppuccin-mocha": { bg: "#1e1e2e", fg: "#cdd6f4", user: "#89b4fa", assistant: "#a6e3a1", tag: "#cba6f7", system: "#9399b2" },
  "catppuccin-macchiato": { bg: "#24273a", fg: "#cad3f5", user: "#8aadf4", assistant: "#a6da95", tag: "#c6a0f6", system: "#8087a2" },
  "catppuccin-frappe": { bg: "#303446", fg: "#c6d0f5", user: "#8caaee", assistant: "#a6d189", tag: "#ca9ee6", system: "#838ba7" },
  "catppuccin-latte": { bg: "#eff1f5", fg: "#4c4f69", user: "#1e66f5", assistant: "#40a02b", tag: "#8839ef", system: "#8c8fa1" },
  "tokyo-night": { bg: "#1a1b26", fg: "#c0caf5", user: "#7aa2f7", assistant: "#9ece6a", tag: "#bb9af7", system: "#565f89" },
  "dracula": { bg: "#282a36", fg: "#f8f8f2", user: "#8be9fd", assistant: "#50fa7b", tag: "#ff79c6", system: "#6272a4" },
  "nord": { bg: "#2e3440", fg: "#d8dee9", user: "#81a1c1", assistant: "#a3be8c", tag: "#b48ead", system: "#4c566a" },
  "gruvbox-dark": { bg: "#282828", fg: "#ebdbb2", user: "#83a598", assistant: "#b8bb26", tag: "#d3869b", system: "#928374" },
  "solarized-dark": { bg: "#002b36", fg: "#839496", user: "#268bd2", assistant: "#859900", tag: "#d33682", system: "#586e75" },
  "solarized-light": { bg: "#fdf6e3", fg: "#657b83", user: "#268bd2", assistant: "#859900", tag: "#6c71c4", system: "#93a1a1" },
  "rose-pine": { bg: "#191724", fg: "#e0def4", user: "#9ccfd8", assistant: "#31748f", tag: "#c4a7e7", system: "#6e6a86" },
  "rose-pine-dawn": { bg: "#faf4ed", fg: "#575279", user: "#286983", assistant: "#56949f", tag: "#907aa9", system: "#9893a5" },
  "one-dark": { bg: "#282c34", fg: "#abb2bf", user: "#61afef", assistant: "#98c379", tag: "#c678dd", system: "#5c6370" },
  "everforest-dark": { bg: "#2d353b", fg: "#d3c6aa", user: "#7fbbb3", assistant: "#a7c080", tag: "#d699b6", system: "#859289" },
};

export const THEME_ORDER = Object.keys(THEME_PRESETS);

export const THEME_LABELS = Object.fromEntries(
  THEME_ORDER.map((id) => [
    id,
    id.split("-").map((w) => w[0].toUpperCase() + w.slice(1)).join(" "),
  ])
);

function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHex([r, g, b]) {
  const c = (v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0");
  return `#${c(r)}${c(g)}${c(b)}`;
}

function mix(hexA, hexB, t) {
  // t=0 -> hexA, t=1 -> hexB
  const a = hexToRgb(hexA);
  const b = hexToRgb(hexB);
  return rgbToHex(a.map((v, i) => v + (b[i] - v) * t));
}

function relativeLuminance(hex) {
  const [r, g, b] = hexToRgb(hex).map((v) => v / 255);
  const lin = (c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

// WCAG relative-luminance contrast ratio, 1:1 (identical) to 21:1 (black/white).
function contrastOf(hexA, hexB) {
  const l1 = relativeLuminance(hexA);
  const l2 = relativeLuminance(hexB);
  const [hi, lo] = l1 > l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}

// Whichever of black/white reads better on a solid fill of `bgHex`. Used
// for badge/segment text (see .pd-seg in style.css): a single fixed dark
// text color goes invisible on the handful of presets whose accent/tag/red
// happens to be a dark, saturated tone even though the THEME itself is
// light (e.g. light-default's indigo tag, light-paper's navy user color).
function bestTextOn(bgHex) {
  return contrastOf("#000000", bgHex) >= contrastOf("#ffffff", bgHex) ? "#000000" : "#ffffff";
}

// Smallest t in [0,1] mixing `from` toward `toward` that reaches
// `targetRatio` contrast against `bg`. Exists because a fixed blend
// percentage (e.g. "45% of the way from bg to system") has no legibility
// floor: several presets' "system"/muted color is ALREADY low-contrast
// against their own bg (that's fine for a terminal's subtle status line),
// so diluting it further toward bg landed some derived --ink-faint values
// as low as 1.1:1 -- effectively invisible. Assumes `toward` (always `fg`
// here) has higher contrast against `bg` than `from` does, which holds for
// every preset since fg is deliberately each theme's strongest color.
function mixForContrast(bg, from, toward, targetRatio) {
  if (contrastOf(from, bg) >= targetRatio) return from;
  if (contrastOf(toward, bg) < targetRatio) return toward; // best available; never overshoot past fg
  let lo = 0;
  let hi = 1;
  for (let i = 0; i < 24; i++) {
    const mid = (lo + hi) / 2;
    if (contrastOf(mix(from, toward, mid), bg) >= targetRatio) hi = mid;
    else lo = mid;
  }
  return mix(from, toward, hi);
}

/**
 * Expands one THEME_PRESETS entry (5 raw colors) into this app's full CSS
 * custom-property set. Layering (bg-raised/card/panel/log) and borders
 * (line/line-soft) are derived by blending bg toward fg at small, fixed
 * ratios -- the same "same hue family, more/less of it" relationship a
 * hand-built dark or light palette already has, just computed instead of
 * hand-picked per theme. Semantic red/danger stays a fixed, deliberately
 * chosen color per light/dark family (picked by background luminance) --
 * "awaiting input" should read as urgent the same way in every theme,
 * which is a DIFFERENT job from the theme's own accent color.
 */
export function deriveTokens(preset) {
  const isLight = relativeLuminance(preset.bg) > 0.5;
  const red = isLight ? "#c0392b" : "#ff5f5f";
  const redDim = isLight ? "#e3a89f" : "#7a2f2f";
  const accent = preset.assistant;
  const amber = preset.tag;
  const blue = preset.user;
  const purple = preset.tag;

  // Contrast floors, not fixed blend percentages -- see mixForContrast's
  // own docstring for why the old fixed-ratio version went unreadable on
  // several presets. --ink-dim targets normal AA body-text contrast
  // (4.5:1); --ink-faint is deliberately allowed to read as more
  // secondary/muted, but still never below 3.3:1 (still clearly legible,
  // just quieter than --ink-dim).
  const inkDim = mixForContrast(preset.bg, mix(preset.fg, preset.system, 0.55), preset.fg, 4.5);
  const inkFaint = mixForContrast(preset.bg, mix(preset.bg, preset.system, isLight ? 0.55 : 0.45), preset.fg, 3.3);

  return {
    "--bg": preset.bg,
    "--bg-raised": mix(preset.bg, preset.fg, isLight ? 0.04 : 0.05),
    "--bg-card": mix(preset.bg, preset.fg, isLight ? 0.04 : 0.05),
    "--bg-panel": mix(preset.bg, preset.fg, isLight ? 0.04 : 0.05),
    "--bg-log": mix(preset.bg, preset.fg, isLight ? 0.02 : 0.03),
    "--line": mix(preset.bg, preset.fg, isLight ? 0.22 : 0.18),
    "--line-soft": mix(preset.bg, preset.fg, isLight ? 0.12 : 0.1),
    "--ink": preset.fg,
    "--ink-dim": inkDim,
    "--ink-faint": inkFaint,
    "--accent": accent,
    "--accent-dim": mix(preset.bg, accent, isLight ? 0.35 : 0.4),
    "--amber": amber,
    "--red": red,
    "--red-dim": redDim,
    "--blue": blue,
    "--purple": purple,
    "--font-ui": "'JetBrains Mono', ui-monospace, monospace",
    // Readable text for a solid badge/segment filled with each color above
    // -- see .pd-seg in style.css, and bestTextOn's own docstring for why a
    // single fixed text color can't work across every preset.
    "--pd-text-on-amber": bestTextOn(amber),
    "--pd-text-on-accent": bestTextOn(accent),
    "--pd-text-on-blue": bestTextOn(blue),
    "--pd-text-on-red": bestTextOn(red),
    "--pd-text-on-faint": bestTextOn(inkFaint),
  };
}
