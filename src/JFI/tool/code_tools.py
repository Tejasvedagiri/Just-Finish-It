"""v2 code tools (laya_plan.md §4.5, §4.8, G2, G3, G11, G17, G18).

Lead and Task write to disk only through scaffold_file / unscaffold_file /
mark_change; Dev reads and edits one function at a time with read_symbol /
replace_symbol. Keeping reads to one symbol is what keeps a Dev episode
inside its token budget.

- scaffold_file: creates (or appends to) a file with STUBS -- the body is
  generated here from the language, never written by the model, so a Lead
  can't "helpfully" implement the function while it's in the file. Each stub
  carries a greppable `JFI: <does>` marker that Dev removes by implementing
  it. Non-code files (TOML, YAML, SQL, Markdown, HTML, .env, Dockerfile, ...)
  get a skeleton of `JFI:` lines in their own comment syntax (G2). A test
  file with no stubs gets only its purpose line (G11: no failing test stubs).
- unscaffold_file: removes a file, or one stub, that is still a pure stub --
  refuses anything containing real code.
- mark_change: puts `JFI-CHANGE:` / `JFI-DELETE:` above an EXISTING symbol
  without touching its body (brownfield work, G3).
- read_symbol / replace_symbol / list_symbols: Python via `ast` (exact);
  JS/TS, Go and Rust via a brace matcher that skips strings and comments.
  No tree-sitter dependency (G17 -- revisit if the brace matcher proves
  fragile on real code).
- search_code, list_dir: size-capped (TOOL_RESULT_MAX_TOKENS).
- scan_markers: the end-of-imp check for leftover JFI markers.

Every path is resolved inside the project root and refused outside it, or
inside .git/ or .jfi/ (G18).
"""

import ast
from collections import Counter
import json
import os
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from JFI.tool.result_cap import cap_result

SKIP_DIRS = {".git", ".jfi", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next",
             "target", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
MARKERS = ("JFI:", "JFI-CHANGE:", "JFI-DELETE:")

PYTHON = {".py"}
BRACE_LANGS = {".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js",
               ".go": "go", ".rs": "rust"}
STUB_BODY = {"js": 'throw new Error("not implemented");', "go": 'panic("not implemented")', "rust": "todo!()"}
LINE_COMMENT = {".py": "#", ".sh": "#", ".toml": "#", ".yaml": "#", ".yml": "#", ".env": "#", ".cfg": "#",
                ".ini": ";", ".txt": "#", ".sql": "--", ".js": "//", ".jsx": "//", ".mjs": "//", ".cjs": "//",
                ".ts": "//", ".tsx": "//", ".go": "//", ".rs": "//"}
BLOCK_COMMENT = {".md": ("<!--", "-->"), ".html": ("<!--", "-->"), ".vue": ("<!--", "-->"),
                 ".svelte": ("<!--", "-->"), ".css": ("/*", "*/"), ".scss": ("/*", "*/")}


# ------------------------------------------------------------------ paths

def resolve_path(root: Path, path: str) -> Tuple[Optional[Path], str]:
    """(absolute path, "") inside the project, or (None, error)."""
    if not path or not str(path).strip():
        return None, "Error: no path given."
    root = Path(root).resolve()
    target = (root / path).resolve()
    try:
        rel = target.relative_to(root)
    except ValueError:
        return None, f"Error: {path!r} is outside the project; only files under the project root can be used."
    if rel.parts and rel.parts[0] in (".git", ".jfi"):
        return None, f"Error: {path!r} is inside {rel.parts[0]}/, which JFI never edits."
    return target, ""


def _suffix(path: Path) -> str:
    name = path.name.lower()
    if name == "dockerfile" or name.startswith("dockerfile."):
        return ".sh"  # '#' comments
    if name.startswith(".env"):
        return ".env"
    return path.suffix.lower()


def _comment(path: Path, text: str) -> str:
    suffix = _suffix(path)
    if suffix in BLOCK_COMMENT:
        start, end = BLOCK_COMMENT[suffix]
        return f"{start} {text} {end}"
    return f"{LINE_COMMENT.get(suffix, '#')} {text}"


# ------------------------------------------------------------------ scaffold

_PLAIN_JS = {".js", ".jsx", ".mjs", ".cjs"}
_BRACE_FUNCTION = re.compile(r"\bfunction\b|=>|^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\b|^\s*func\b")


def _strip_ts_types(signature: str) -> str:
    """Drop TypeScript type annotations from a JS declaration line: the return
    type after the parameter list, and `name: Type` on each parameter
    (defaults like `root = document` are kept)."""
    open_at, close_at = signature.find("("), signature.rfind(")")
    if open_at < 0 or close_at < open_at:
        return signature
    params, depth, current = [], 0, ""
    for ch in signature[open_at + 1:close_at]:
        depth += ch in "([{<"
        depth -= ch in ")]}>"
        if ch == "," and depth == 0:
            params.append(current)
            current = ""
        else:
            current += ch
    params.append(current)
    cleaned = []
    for param in params:
        name, _, default = param.partition("=")
        name = re.sub(r"\s*\??:\s*.+$", "", name.strip()) if not name.strip().startswith(("{", "[")) \
            else name.strip()
        cleaned.append(f"{name} = {default.strip()}" if default.strip() else name)
    arrow = " =>" if "=>" in signature[close_at + 1:] else ""  # the return type goes, an arrow function's => stays
    return f"{signature[:open_at]}({', '.join(p for p in cleaned if p)}){arrow}"


def _render_stub(path: Path, signature: str, does: str) -> Tuple[Optional[str], str]:
    signature = signature.rstrip()
    if "\n" in signature or not signature:
        return None, f"Error: a stub signature must be one declaration line, got {signature!r}."
    suffix = _suffix(path)
    does = does.strip().replace("\n", " ")
    if suffix in PYTHON:
        if not re.match(r"^(async\s+)?def\s+\w+\s*\(.*\)\s*(->\s*.+)?:$", signature):
            return None, f"Error: a Python stub signature must be a single 'def name(...):' line, got {signature!r}."
        return f'{signature}\n    """JFI: {does}"""\n    raise NotImplementedError\n', ""
    lang = BRACE_LANGS.get(suffix)
    if lang:
        # Observed on the stui run: models write the declaration with a
        # trailing "{" (refused, then retried) or Python's ":" (accepted, which
        # produced `f(): {`, invalid JS). Normalise the ending instead.
        signature = re.sub(r"\s*[{:;]+\s*$", "", signature)
        if suffix in _PLAIN_JS:
            # Observed on the stui run: `getSettings(): object`, `saveSetting(key: string, value): void`
            # in a .js file -- TypeScript habits that make the stub invalid JavaScript.
            signature = _strip_ts_types(signature)
        if not _BRACE_FUNCTION.search(signature):
            # Observed on the stui run (gemma): `export const TITLES` stubbed as a
            # function got a `{ throw ... }` body -- invalid JavaScript.
            return None, (f"Error: {signature!r} isn't a function declaration. Stubs are for functions "
                          "(e.g. 'export function f(x)', 'export const f = (x) =>'); put a constant, a data "
                          "table or a class in a `fill` line instead (what goes there, with its source lines).")
        # Braces inside the parameter list are destructuring; only a body after it is refused.
        open_at, close_at = signature.find("("), signature.rfind(")")
        outside = signature[:open_at] + signature[close_at + 1:] if 0 <= open_at < close_at else signature
        if "{" in outside or "}" in outside:
            return None, "Error: give only the declaration line (no braces); the body is generated."
        return f"{signature} {{\n    {_comment(path, 'JFI: ' + does)}\n    {STUB_BODY[lang]}\n}}\n", ""
    return None, (f"Error: {path.suffix or path.name} files have no functions to stub; scaffold it as an "
                  "artifact (stubs=[] and `fill` lines) instead.")


def _scaffold_json(target: Path, path: str, purpose: str, stubs: Sequence[dict], fill: Sequence[str]) -> str:
    """JSON has no comments, so its skeleton is itself valid JSON: the purpose
    and fill lines live under "//" keys (npm's own convention for comments
    in package.json). Observed on the stui run: the "#"-comment fallback made
    package.json invalid, and a Lead spent two whole conversations creating,
    reading and deleting it without ever adding its node."""
    if stubs:
        return "Error: JSON files have no functions to stub; describe what goes in them with `fill` lines."
    data: Dict[str, object] = {}
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as e:
            return f"Error: {path} isn't valid JSON ({e}); fix or remove it before scaffolding."
        if not isinstance(data, dict):
            return f"Error: {path} holds a JSON {type(data).__name__}, not an object; it can't take a skeleton."
    created = not target.exists()
    data.setdefault("//", f"JFI-FILE: {purpose}")
    data["//fill"] = list(data.get("//fill") or []) + [f"JFI: {line.strip()}" for line in fill]
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return f"{'Created' if created else 'Appended to'} {path} with {len(fill)} fill line(s) (valid JSON):\n{text}".rstrip()


def scaffold_file(root: Path, path: str, purpose: str, stubs: Sequence[dict] = (),
                  fill: Sequence[str] = ()) -> str:
    """Create or append to `path`: a purpose line, one stub per `stubs` item
    ({"signature", "does"}), and for artifacts one `JFI: <line>` per `fill`
    item (what still needs filling in)."""
    target, error = resolve_path(root, path)
    if error:
        return error
    # The header is added here; a model that writes it itself got
    # "JFI-FILE: JFI-FILE: ..." and spent three turns deleting and redoing it.
    purpose = re.sub(r"^\s*(JFI-FILE:\s*)+", "", purpose or "")
    if not purpose.strip():
        return "Error: give the file's purpose (one line)."
    if _suffix(target) == ".json":
        return _scaffold_json(target, path, purpose.strip(), stubs, fill)
    rendered: List[str] = []
    for stub in stubs:
        text, error = _render_stub(target, str(stub.get("signature", "")), str(stub.get("does", "")))
        if error:
            return error
        rendered.append(text)
    for line in fill:
        rendered.append(_comment(target, f"JFI: {line.strip()}") + "\n")

    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    for stub in stubs:
        name = _def_name(str(stub.get("signature", "")))
        if name and name in {s[0] for s in _symbols(target, existing)}:
            return f"Error: {path} already defines {name!r}; use mark_change for an existing symbol."
    header = "" if existing else _comment(target, f"JFI-FILE: {purpose.strip()}") + "\n"
    block = "\n".join(rendered)
    new_text = existing + ("\n" if existing and not existing.endswith("\n") else "") + \
        ("\n" if existing else "") + header + ("\n" if header and block else "") + block
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_text, encoding="utf-8")
    added = f"{len(stubs)} stub(s)" + (f", {len(fill)} fill line(s)" if fill else "")
    return f"{'Appended to' if existing else 'Created'} {path} with {added}:\n{header}{block}".rstrip()


def _def_name(signature: str) -> Optional[str]:
    m = re.search(r"(?:def|function|func|fn)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", signature)
    return m.group(1) if m else None


def copy_lines(root: Path, src: str, start: int, end: int, dst: str, at_marker: Optional[str] = None) -> str:
    """Copy lines start..end (1-based, inclusive) of `src` into `dst`, in code:
    into the place of the `JFI:` fill line containing `at_marker`, or
    appended. Observed on the stui run: a conversion plan is mostly verbatim
    moves ("copy L341-957 into index.html"), and the only way Dev had to move
    text was to re-type it through write_file, which costs the text twice in
    tokens and overflowed a single episode on far smaller files (G29)."""
    source, error = resolve_path(root, src)
    if error:
        return error
    target, error = resolve_path(root, dst)
    if error:
        return error
    if not source.is_file():
        return f"Error: {src} doesn't exist."
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = int(start), int(end)
    if not 1 <= start <= end <= len(lines):
        return f"Error: {src} has {len(lines)} lines; {start}-{end} isn't a range inside it."
    chunk = "".join(lines[start - 1:end])
    chunk += "" if chunk.endswith("\n") else "\n"
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if at_marker:
        rows = existing.splitlines(keepends=True)
        hits = [i for i, row in enumerate(rows) if "JFI:" in row and at_marker in row]
        if len(hits) != 1:
            fills = [r.strip() for r in rows if "JFI:" in r][:10]
            return (f"Error: {len(hits)} JFI: lines in {dst} contain {at_marker!r}; it must match exactly one. "
                    f"Its JFI: lines: {fills or 'none'}.")
        rows[hits[0]] = chunk
        new_text = "".join(rows)
    else:
        new_text = existing + ("\n" if existing and not existing.endswith("\n") else "") + chunk
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_text, encoding="utf-8")
    where = f"in place of the JFI line matching {at_marker!r}" if at_marker else "appended"
    return f"Copied {src} lines {start}-{end} ({end - start + 1} lines) into {dst}, {where}."


def unscaffold_file(root: Path, path: str, name: Optional[str] = None) -> str:
    """Remove a whole file that is still only stubs, or one stub by name."""
    target, error = resolve_path(root, path)
    if error:
        return error
    if not target.exists():
        return f"Error: {path} doesn't exist."
    text = target.read_text(encoding="utf-8")
    if _suffix(target) == ".json":
        try:
            data = json.loads(text or "{}")
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict) or set(data) - {"//", "//fill"}:
            return f"Error: {path} contains more than a JFI skeleton; refusing to delete it."
        target.unlink()
        return f"Deleted {path} (it held only a JFI skeleton)."
    spans = {sym: (start, end) for sym, start, end in _symbols(target, text)}
    lines = text.splitlines(keepends=True)
    if name:
        if name not in spans:
            return f"Error: {path} has no symbol {name!r}."
        start, end = spans[name]
        if not _is_stub("".join(lines[start:end])):
            return f"Error: {name!r} in {path} has real code; unscaffold_file only removes JFI stubs."
        del lines[start:end]
        target.write_text("".join(lines), encoding="utf-8")
        return f"Removed stub {name!r} from {path}."
    for sym, (start, end) in spans.items():
        if not _is_stub("".join(lines[start:end])):
            return f"Error: {path} contains real code ({sym!r}); unscaffold_file only deletes pure JFI stubs."
    covered = set()
    for start, end in spans.values():
        covered.update(range(start, end))
    for i, line in enumerate(lines):
        if i in covered or not line.strip():
            continue
        if any(m in line for m in (*MARKERS, "JFI-FILE:")):
            continue
        return f"Error: {path} contains more than JFI stubs (line {i + 1}); refusing to delete it."
    target.unlink()
    return f"Deleted {path} (it held only JFI stubs)."


def _is_stub(source: str) -> bool:
    return "JFI:" in source and any(body in source for body in ("raise NotImplementedError", *STUB_BODY.values()))


# ------------------------------------------------------------------ symbols

def _symbols(path: Path, text: str) -> List[Tuple[str, int, int]]:
    """Top-level (name, start_line_index, end_line_index_exclusive)."""
    if _suffix(path) in PYTHON:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        out = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
                out.append((node.name, start, node.end_lineno))
        return out
    lang = BRACE_LANGS.get(_suffix(path))
    if not lang:
        return []
    return _brace_symbols(text, lang)


_DEF_PATTERNS = {
    "js": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*[<(]"
                     r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?"
                     r"(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"
                     r"|^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)"),
    "go": re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*[\[(]|^type\s+([A-Za-z_]\w*)\s+(?:struct|interface)"),
    "rust": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)"
                       r"|^\s*(?:pub\s+)?(?:struct|enum|trait|impl)\s+([A-Za-z_]\w*)"),
}


def _brace_symbols(text: str, lang: str) -> List[Tuple[str, int, int]]:
    lines = text.splitlines(keepends=True)
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    out, i = [], 0
    while i < len(lines):
        m = _DEF_PATTERNS[lang].match(lines[i])
        if not m or (lang == "js" and lines[i].startswith((" ", "\t"))):
            i += 1
            continue
        name = next(g for g in m.groups() if g)
        open_at = text.find("{", offsets[i])
        if open_at == -1:
            i += 1
            continue
        close_at = _match_brace(text, open_at, lang)
        if close_at is None:
            i += 1
            continue
        end = text.count("\n", 0, close_at) + 1
        start = i
        while start > 0 and _is_attached_comment(lines[start - 1]):
            start -= 1
        out.append((name, start, end))
        i = end
    return out


def _is_attached_comment(line: str) -> bool:
    s = line.strip()
    return s.startswith(("//", "#[", "@")) or any(m in s for m in ("JFI-CHANGE:", "JFI-DELETE:"))


def _match_brace(text: str, open_at: int, lang: str) -> Optional[int]:
    depth, i, n = 0, open_at, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            nl = text.find("\n", i)
            i = n if nl == -1 else nl
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        quotes = '"`' if lang != "rust" else '"'
        if c in quotes or (c == "'" and (lang != "rust" or text[i + 2:i + 3] == "'" or text[i + 1:i + 2] == "\\")):
            i = _skip_string(text, i, c)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _skip_string(text: str, i: int, quote: str) -> int:
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        if quote != "`" and text[j] == "\n":
            return j
        j += 1
    return j


def list_symbols(root: Path, path: str) -> str:
    target, error = resolve_path(root, path)
    if error:
        return error
    if not target.exists():
        return f"Error: {path} doesn't exist."
    text = target.read_text(encoding="utf-8")
    lines = text.splitlines()
    out = []
    for name, start, end in _symbols(target, text):
        span = "\n".join(lines[start:end])
        tag = " [JFI stub]" if _is_stub(span) else ""
        tag += " [JFI-CHANGE]" if "JFI-CHANGE:" in span else ""
        tag += " [JFI-DELETE]" if "JFI-DELETE:" in span else ""
        out.append(f"{name} (lines {start + 1}-{end}){tag}")
    loose = [f"line {i + 1}: {line.strip()}" for i, line in enumerate(lines)
             if "JFI:" in line and not any(s <= i < e for _, s, e in _symbols(target, text))]
    if not out and not loose:
        return f"{path}: no functions or JFI markers found."
    return cap_result(f"{path}:\n" + "\n".join(out + loose), "Use read_symbol(path, name) for one.")


def read_symbol(root: Path, path: str, name: str) -> str:
    target, error = resolve_path(root, path)
    if error:
        return error
    if not target.exists():
        return f"Error: {path} doesn't exist."
    text = target.read_text(encoding="utf-8")
    for sym, start, end in _symbols(target, text):
        if sym == name:
            body = "".join(text.splitlines(keepends=True)[start:end])
            return cap_result(f"{path} lines {start + 1}-{end}:\n{body}",
                              f"Use read_file('{path}', start, end) for a line range.")
    names = ", ".join(s for s, _, _ in _symbols(target, text)) or "none"
    return f"Error: {path} has no top-level symbol {name!r}. Symbols: {names}."


def replace_symbol(root: Path, path: str, name: str, new_source: str) -> str:
    """Replace symbol `name` (its decorators/attached comments included) with
    `new_source`. Python edits must parse, before and after."""
    target, error = resolve_path(root, path)
    if error:
        return error
    if not target.exists():
        return f"Error: {path} doesn't exist."
    text = target.read_text(encoding="utf-8")
    span = next(((s, e) for sym, s, e in _symbols(target, text) if sym == name), None)
    if span is None:
        names = ", ".join(s for s, _, _ in _symbols(target, text)) or "none"
        return f"Error: {path} has no top-level symbol {name!r}. Symbols: {names}."
    if _suffix(target) in PYTHON:
        try:
            ast.parse(new_source)
        except SyntaxError as e:
            return f"Error: new_source for {name!r} doesn't parse: {e}."
    lines = text.splitlines(keepends=True)
    start, end = span
    replacement = new_source if new_source.endswith("\n") else new_source + "\n"
    new_text = "".join(lines[:start]) + replacement + "".join(lines[end:])
    if _suffix(target) in PYTHON:
        try:
            ast.parse(new_text)
        except SyntaxError as e:
            return f"Error: {path} wouldn't parse after replacing {name!r}: {e}. Nothing was written."
    target.write_text(new_text, encoding="utf-8")
    return f"Replaced {name!r} in {path} (was lines {start + 1}-{end}, now {replacement.count(chr(10))} lines)."


def mark_change(root: Path, path: str, symbol: str, kind: str, what: str) -> str:
    """Put `JFI-CHANGE: what` (kind=change) or `JFI-DELETE: what` (kind=delete)
    directly above an existing symbol, never touching its body."""
    target, error = resolve_path(root, path)
    if error:
        return error
    if kind not in ("change", "delete"):
        return "Error: kind must be 'change' or 'delete'."
    if not target.exists():
        return f"Error: {path} doesn't exist."
    text = target.read_text(encoding="utf-8")
    span = next(((s, e) for sym, s, e in _symbols(target, text) if sym == symbol), None)
    if span is None:
        return f"Error: {path} has no top-level symbol {symbol!r} to mark."
    lines = text.splitlines(keepends=True)
    start = span[0]
    indent = re.match(r"\s*", lines[start]).group(0)
    marker = f"{indent}{_comment(target, ('JFI-CHANGE: ' if kind == 'change' else 'JFI-DELETE: ') + what.strip())}\n"
    lines.insert(start, marker)
    target.write_text("".join(lines), encoding="utf-8")
    return f"Marked {symbol!r} in {path} for {kind}: {what.strip()}"


LARGE_FILE_LINES = 300
LARGE_FILE_CHARS = 12_000  # ~3k tokens: the most one tool result may carry
OUTLINE_CHUNK_LINES = 60
OUTLINE_CHUNK_CHARS = 6_000
DIR_OUTLINE_MAX_FILES = 150
DIR_OUTLINE_NAMES_PER_FILE = 8
HTML_LIKE = {".html", ".htm", ".vue", ".svelte", ".xml", ".jsx", ".tsx"}
DOC_SUFFIXES = {".md", ".markdown", ".rst", ".txt", ".adoc"}

_HTML_BLOCK = re.compile(r"<(head|body|header|nav|aside|main|section|article|footer|style|script|template|form|"
                         r"dialog|table)\b([^>]*)>", re.I)
_HTML_ATTR = re.compile(r'\b(id|data-view|class)="([^"]*)"')
_HTML_ID = re.compile(r'\bid="([^"]+)"')
_HTML_DATA_ATTR = re.compile(r'\b(data-[\w-]+)=')
_HTML_CONTENT_BLOCKS = {"header", "nav", "aside", "main", "section", "article", "footer", "form", "dialog"}
OUTLINE_IDS_PER_BLOCK = 10
_COMMENT_HEADER = re.compile(r"^\s*(?:/\*+|//+|#+|<!--|--)\s*[-=*#~]{2,}\s*([A-Za-z0-9][^*]*?)\s*"
                             r"(?:[-=*#~]{2,}.*|\*/|-->)?\s*$")
_JS_FUNCTION = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)|"
                          r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
                          r"(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)|"
                          r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+])\1{2,}\s*$")
_CSS_AT_RULE = re.compile(r"^\s*(@(?:media|keyframes|font-face|supports|layer|import)\b[^{;]*)")
_CONFIG_KEY = re.compile(r"^([A-Za-z_][\w.\-]*)\s*[:=]|^\[([^\]]+)\]\s*$")
_SQL_CREATE = re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?"
                         r"(TABLE|VIEW|INDEX|FUNCTION|PROCEDURE|TRIGGER|TYPE|SCHEMA)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                         r"([\w.\"`]+)", re.I)


def _block_end(lines: List[str], start: int, tag: str) -> int:
    """0-based index of the line closing the <tag> opened on `start`."""
    open_re, close_re = re.compile(rf"<{tag}\b", re.I), re.compile(rf"</{tag}\s*>", re.I)
    depth = 0
    for i in range(start, len(lines)):
        depth += len(open_re.findall(lines[i])) - len(close_re.findall(lines[i]))
        if depth <= 0:
            return i
    return len(lines) - 1


def _entry(start: int, end: Optional[int], label: str) -> Tuple[int, str]:
    where = f"L{start + 1}" + (f"-{end + 1}" if end is not None and end > start else "")
    return start, f"{where.ljust(12)}{label}"


def _outline_html(lines: List[str]) -> List[Tuple[int, str]]:
    out = []
    for i, line in enumerate(lines):
        for match in _HTML_BLOCK.finditer(line):
            tag = match.group(1).lower()
            attrs = " ".join(f'{k}="{v}"' for k, v in _HTML_ATTR.findall(match.group(2))
                             if k != "class" or tag in ("section", "main", "aside", "nav"))
            end = _block_end(lines, i, tag)
            inside = ""
            if tag in _HTML_CONTENT_BLOCKS:
                # The ids a block holds are its interface: what scripts hook
                # into. Observed on the stui run: seeing only "<section
                # id=view-watchlist> (11 lines)", the Architect invented a
                # renderWatchlist() and a #watchForm for a block of static cards.
                body = "\n".join(lines[i + 1:end + 1])
                ids = list(dict.fromkeys(_HTML_ID.findall(body)))
                shown = " ".join(f"#{x}" for x in ids[:OUTLINE_IDS_PER_BLOCK])
                more = f" +{len(ids) - OUTLINE_IDS_PER_BLOCK}" if len(ids) > OUTLINE_IDS_PER_BLOCK else ""
                hooks = Counter(_HTML_DATA_ATTR.findall(body))
                data = " ".join(f"[{name}]x{n}" for name, n in hooks.most_common(4))
                parts = [f"ids: {shown}{more}" if ids else "", f"hooks: {data}" if data else ""]
                inside = "  " + ("  ".join(p for p in parts if p) or "static (no ids or data- hooks inside)")
            out.append(_entry(i, end, f"<{tag}{' ' + attrs if attrs else ''}>  ({end - i + 1} lines){inside}"))
    return out


def _outline_python(text: str) -> List[Tuple[int, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(_entry(node.lineno - 1, node.end_lineno - 1, f"def {node.name}()"))
        elif isinstance(node, ast.ClassDef):
            out.append(_entry(node.lineno - 1, node.end_lineno - 1, f"class {node.name}"))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(_entry(item.lineno - 1, item.end_lineno - 1, f"  .{item.name}()"))
    return out


def _outline_docs(lines: List[str]) -> List[Tuple[int, str]]:
    out, fenced = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced  # a '#' inside a code block is a comment, not a heading
            continue
        if fenced:
            continue
        heading = _MD_HEADING.match(line)
        if heading:
            out.append(_entry(i, None, f"{'  ' * (len(heading.group(1)) - 1)}{heading.group(2)}"))
        elif i and _RST_UNDERLINE.match(line) and lines[i - 1].strip() and not _RST_UNDERLINE.match(lines[i - 1]):
            out.append(_entry(i - 1, None, lines[i - 1].strip()))
    return out


def _outline_lines(lines: List[str], suffix: str) -> List[Tuple[int, str]]:
    """Line-pattern structure any text file can have: section comments, and
    per type: JS/TS functions and classes, CSS at-rules, config keys, SQL
    objects."""
    config = suffix in (".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".properties")
    out = []
    for i, line in enumerate(lines):
        header = _COMMENT_HEADER.match(line) if suffix not in DOC_SUFFIXES else None  # '### x' is a heading
        if header:
            out.append(_entry(i, None, f"-- {header.group(1).strip(' */->')}"))
            continue
        if suffix in BRACE_LANGS or suffix in HTML_LIKE:
            fn = _JS_FUNCTION.match(line)
            if fn:
                name = fn.group(1) or fn.group(2)
                out.append(_entry(i, None, f"  {name}()" if name else f"  class {fn.group(3)}"))
        elif suffix in (".css", ".scss", ".sass", ".less"):
            at_rule = _CSS_AT_RULE.match(line)
            if at_rule:
                out.append(_entry(i, None, at_rule.group(1).strip()))
        elif config and line[:1].strip():
            key = _CONFIG_KEY.match(line)
            if key:
                out.append(_entry(i, None, key.group(1) or f"[{key.group(2)}]"))
        elif suffix == ".sql":
            create = _SQL_CREATE.match(line)
            if create:
                out.append(_entry(i, None, f"{create.group(1).upper()} {create.group(2)}"))
    return out


def _outline_json(text: str) -> List[Tuple[int, str]]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return [(0, f"(a JSON {type(data).__name__} of {len(data)} items)" if isinstance(data, list) else "")]
    lines = text.splitlines()
    out = []
    for key in data:
        line = next((i for i, t in enumerate(lines) if f'"{key}"' in t), 0)
        value = data[key]
        size = f" ({len(value)} items)" if isinstance(value, (list, dict)) else ""
        out.append(_entry(line, None, f"{key}{size}"))
    return out


def _outline_chunks(lines: List[str]) -> List[Tuple[int, str]]:
    """The fallback for text with little structure (logs, plain specs, data):
    ~OUTLINE_CHUNK_LINES-line chunks, cut at a blank line where one is near,
    each labelled with its first non-blank line."""
    out, start = [], 0
    while start < len(lines):
        end, size = start, 0
        while end < len(lines) and end - start < OUTLINE_CHUNK_LINES and (size < OUTLINE_CHUNK_CHARS or end == start):
            size += len(lines[end]) + 1
            end += 1
        if end - start == OUTLINE_CHUNK_LINES:
            for j in range(end, start + OUTLINE_CHUNK_LINES // 2, -1):
                if j < len(lines) and not lines[j].strip():
                    end = j
                    break
        first = next((t.strip() for t in lines[start:end] if t.strip()), "")
        out.append(_entry(start, end - 1, first[:80]))
        start = end + 1 if end < len(lines) and not lines[end].strip() else end
    return out


def _file_structure(target: Path, text: str) -> List[Tuple[int, str]]:
    lines = text.splitlines()
    suffix = _suffix(target)
    entries: List[Tuple[int, str]] = []
    if suffix in HTML_LIKE:
        entries += _outline_html(lines)
    if suffix in PYTHON:
        entries += _outline_python(text)
    elif suffix in BRACE_LANGS:
        entries += [_entry(start, end - 1, f"{name}()") for name, start, end in _symbols(target, text)]
    if suffix in DOC_SUFFIXES:
        entries += _outline_docs(lines)
    if suffix == ".json":
        entries += _outline_json(text)
    entries += _outline_lines(lines, suffix)
    unique = {e for e in entries if len(e[1].split(None, 1)) == 2}
    return sorted(unique, key=lambda e: (e[0], e[1]))


def _outline_dir(root: Path, target: Path, path: str) -> str:
    """A repo/folder map: each text file's size and its main contents, so the
    Architect can see a whole repo's layout without opening files."""
    rows, files, skipped = [], 0, 0
    for file in _walk(root, target):
        if files >= DIR_OUTLINE_MAX_FILES:
            skipped += 1
            continue
        rel = file.relative_to(root).as_posix()
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            rows.append(f"  {rel}  (binary, {file.stat().st_size // 1024} KB)")
            files += 1
            continue
        structure = _file_structure(file, text)
        # Only the named parts (functions, classes, headings, keys, blocks):
        # section comments are noise at folder level.
        named = [n for n in (label.split(None, 1)[1].strip() for _, label in structure) if not n.startswith("--")]
        names = named[:DIR_OUTLINE_NAMES_PER_FILE]
        more = f", +{len(named) - len(names)} more" if len(named) > len(names) else ""
        summary = f": {', '.join(names)}{more}" if names else ""
        rows.append(f"  {rel}  ({text.count(chr(10)) + 1} lines){summary}")
        files += 1
    if skipped:
        rows.append(f"  ... {skipped} more files (outline a subfolder to see them)")
    head = f"{path}/: {files + skipped} files (skipping {', '.join(sorted(SKIP_DIRS))})"
    return cap_result("\n".join([head, *(rows or ["  (empty)"])]),
                      "Outline a subfolder or one file to see more.")


def outline_file(root: Path, path: str = ".") -> str:
    """A cheap line map, built in code, so a role reads only the ranges it
    needs instead of whole files. Works on anything:
    - a folder: every file with its size and main contents (a repo map);
    - code: classes/methods (Python), functions and classes (JS/TS/Go/Rust);
    - HTML/Vue/JSX: structural blocks with their end lines and ids;
    - docs (Markdown, RST, text): the heading tree;
    - CSS: section comments and @-rules; JSON/YAML/TOML/INI: top-level keys;
      SQL: CREATE statements;
    - any file: `/* ---- section ---- */`-style comment headers;
    - text with little structure: ~60-line chunks cut at blank lines, each
      labelled with its first line."""
    target, error = resolve_path(root, path)
    if error:
        return error
    if target.is_dir():
        return _outline_dir(Path(root).resolve(), target, path.rstrip("/\\") or ".")
    if not target.is_file():
        return f"Error: {path} doesn't exist."
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: {path} isn't a text file."
    lines = text.splitlines()
    structure = _file_structure(target, text)
    big = len(lines) > LARGE_FILE_LINES or len(text) > LARGE_FILE_CHARS
    if big and len(structure) < max(3, len(lines) // 200, len(text) // 40_000):
        structure = sorted(set(structure) | set(_outline_chunks(lines)), key=lambda e: e[0])
    head = f"{path}: {len(lines)} lines, {len(text.encode('utf-8')) // 1024} KB"
    body = ["  " + label for _, label in structure] or ["  (short and unstructured; read it whole)"]
    return cap_result("\n".join([head, *body]), f"Read a part with read_file('{path}', start, end).")


def read_file_range(root: Path, path: str, start: Optional[int] = None, end: Optional[int] = None) -> str:
    """v2 read_file: project-root-safe and size-capped, with an optional
    1-based inclusive line range (numbered, so a later read can target it)."""
    target, error = resolve_path(root, path)
    if error:
        return error
    if not target.is_file():
        return f"Error: {path} doesn't exist."
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: {path} isn't a text file."
    if start is None and end is None:
        if text.count("\n") + 1 > LARGE_FILE_LINES or len(text) > LARGE_FILE_CHARS:
            # A whole-file read of a big file only ever returned its first
            # ~3k tokens (on the stui run: 300 lines of CSS out of 1,621) and
            # spent the budget doing it. Give the map instead.
            return (f"{path} is too big to read whole; here is its outline. Read the part you need with "
                    f"read_file('{path}', start, end).\n\n" + outline_file(root, path))
        return cap_result(text, f"Use read_file('{path}', start, end) for a line range, or read_symbol.")
    lines = text.splitlines()
    first, last = max(1, int(start or 1)), min(len(lines), int(end or len(lines)))
    if first > last:
        return f"Error: {path} has {len(lines)} lines; {start}-{end} is empty."
    body = "\n".join(f"{no:>5}| {lines[no - 1]}" for no in range(first, last + 1))
    return cap_result(f"{path} lines {first}-{last}:\n{body}", "Ask for a smaller range.")


# ------------------------------------------------------------------ search / list / scan

def _walk(root: Path, base: Path):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in sorted(filenames):
            yield Path(dirpath) / filename


def search_code(root: Path, pattern: str, path: str = ".", regex: bool = False) -> str:
    base, error = resolve_path(root, path) if path not in (".", "") else (Path(root).resolve(), "")
    if error:
        return error
    try:
        matcher = re.compile(pattern if regex else re.escape(pattern))
    except re.error as e:
        return f"Error: bad regex {pattern!r}: {e}."
    hits = []
    files = [base] if base.is_file() else _walk(root, base)
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = file.relative_to(Path(root).resolve()).as_posix()
        for no, line in enumerate(text.splitlines(), 1):
            if matcher.search(line):
                hits.append(f"{rel}:{no}: {line.strip()[:200]}")
    if not hits:
        return f"No matches for {pattern!r}."
    return cap_result(f"{len(hits)} match(es):\n" + "\n".join(hits), "Narrow the pattern or pass a path.")


_STRING_LITERAL = re.compile(r"""(['"`])(?:\\.|(?!\1).)*\1""")
_IMPORT_LINE = re.compile(r"^\s*(?:import\b|from\b|export\s+\{|(?:const|let|var)\s+.*\brequire\()")
_CODE_COMMENT = ("#", "//", "/*", "*", "<!--", "--")


def find_references(root: Path, name: str, path: str = ".") -> str:
    """Where a function or class is defined, imported and called, from the
    same parsers as read_symbol. A change or delete leaf needs every caller;
    search_code also returns comments, strings and look-alike names."""
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", name or ""):
        return f"Error: {name!r} isn't a function or class name."
    base, error = resolve_path(root, path) if path not in (".", "") else (Path(root).resolve(), "")
    if error:
        return error
    word = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
    groups = {"definition": [], "import": [], "call": [], "other use": []}
    files = [base] if base.is_file() else _walk(root, base)
    for file in files:
        if _suffix(file) not in PYTHON and _suffix(file) not in BRACE_LANGS and _suffix(file) not in HTML_LIKE:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not word.search(text):
            continue
        rel = file.relative_to(Path(root).resolve()).as_posix()
        starts = {start for symbol, start, _ in _symbols(file, text) if symbol == name}
        lines = text.splitlines()
        for i, line in enumerate(lines):
            code = _STRING_LITERAL.sub('""', line)
            if not word.search(code) or line.lstrip().startswith(_CODE_COMMENT):
                continue
            if any(start <= i < start + 4 for start in starts) and re.search(
                    rf"\b(def|class|function|func|fn|const|let|var)\s+{re.escape(name)}\b", code):
                kind = "definition"
            elif _IMPORT_LINE.search(code):
                kind = "import"
            elif re.search(rf"(?<![\w$]){re.escape(name)}\s*\(", code):
                kind = "call"
            else:
                kind = "other use"
            groups[kind].append(f"{rel}:{i + 1}: {line.strip()[:160]}")
    found = sum(len(v) for v in groups.values())
    if not found:
        return f"No references to {name} (comments and strings don't count)."
    parts = [f"{found} reference(s) to {name}:"]
    for kind, hits in groups.items():
        if hits:
            parts.append(f"{kind}s ({len(hits)}):" if kind != "other use" else f"other uses ({len(hits)}):")
            parts.extend(f"  {h}" for h in hits)
    return cap_result("\n".join(parts), "Pass a path to look in one folder or file.")


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _patch_files(diff: str):
    """[(old_path, new_path, [hunk lines...])] from a unified diff."""
    files, current = [], None
    for line in diff.replace("\r\n", "\n").rstrip("\n").split("\n"):
        if line.startswith("--- "):
            current = [line[4:].split("\t")[0].strip(), None, []]
            files.append(current)
        elif line.startswith("+++ ") and current is not None and current[1] is None:
            current[1] = line[4:].split("\t")[0].strip()
        elif current is not None and current[1] is not None and (line.startswith(("@@", " ", "+", "-")) or line == ""):
            current[2].append(line)
    return files


def _strip_prefix(path: str) -> str:
    return path[2:] if path.startswith(("a/", "b/")) else path


def apply_patch(root: Path, diff: str) -> str:
    """Several edits across one or more files in one call, from a unified
    diff -- all or nothing: every hunk must match the file's current text
    exactly once, or no file is written. Modify leaves spent many turns on
    one replace_in_file per spot, and the turn cap is what ends episodes."""
    files = _patch_files(diff or "")
    if not files:
        return "Error: no unified diff found (expected '--- a/file', '+++ b/file' and '@@ ... @@' hunks)."
    writes = {}
    for old_name, new_name, body in files:
        deleting = new_name == "/dev/null"
        name = _strip_prefix(old_name if deleting else new_name)
        target, error = resolve_path(root, name)
        if error:
            return error
        creating = old_name == "/dev/null"
        if creating:
            text = ""
        else:
            if not target.is_file():
                return f"Error: {name} doesn't exist; nothing was changed."
            text = writes.get(target, target.read_text(encoding="utf-8"))
        hunks, hunk = [], None
        for line in body:
            if line.startswith("@@"):
                if not _HUNK.match(line):
                    return f"Error: bad hunk header {line!r} in {name}; nothing was changed."
                hunk = ([], [])
                hunks.append(hunk)
            elif hunk is not None:
                tag, rest = (line[:1], line[1:]) if line else (" ", "")
                if tag in (" ", "-"):
                    hunk[0].append(rest)
                if tag in (" ", "+"):
                    hunk[1].append(rest)
        if not hunks:
            return f"Error: no hunks for {name}; nothing was changed."
        if deleting:
            writes[target] = None
            continue
        lines = text.split("\n")
        for old_lines, new_lines in hunks:
            if creating:
                lines = new_lines + [""]
                continue
            # Whole lines, never substrings: "z = 3" must not match inside "z = 30".
            size = len(old_lines)
            at = [i for i in range(len(lines) - size + 1) if size and lines[i:i + size] == old_lines]
            if len(at) != 1:
                where = "isn't in" if not at else f"matches {len(at)} places in"
                return (f"Error: a hunk {where} {name} (its - and context lines must match the file exactly, "
                        f"once); nothing was changed. read_file the spot and resend the diff.")
            lines[at[0]:at[0] + size] = new_lines
        writes[target] = "\n".join(lines)
    for target, text in writes.items():
        if text is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    names = ", ".join(t.relative_to(Path(root).resolve()).as_posix() for t in writes)
    return f"Applied the patch to {len(writes)} file(s): {names}."


def list_dir(root: Path, path: str = ".") -> str:
    base, error = resolve_path(root, path) if path not in (".", "") else (Path(root).resolve(), "")
    if error:
        return error
    if not base.is_dir():
        return f"Error: {path} is not a directory."
    entries = []
    for child in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name in (".git", ".jfi"):
            continue
        if child.is_dir():
            note = " (skipped by search_code)" if child.name in SKIP_DIRS else ""
            entries.append(f"{child.name}/{note}")
        else:
            entries.append(f"{child.name} ({child.stat().st_size:,} bytes)")
    return cap_result(f"{path}:\n" + ("\n".join(entries) or "(empty)"), "List a subdirectory instead.")


def scan_markers(root: Path) -> List[Tuple[str, int, str]]:
    """Every leftover JFI marker: (relative path, line number, marker). The
    end-of-imp check -- each one is planned work nobody finished."""
    root = Path(root).resolve()
    found = []
    for file in _walk(root, root):
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            for marker in MARKERS:
                if marker in line and "JFI-FILE:" not in line:
                    found.append((file.relative_to(root).as_posix(), no, marker))
                    break
    return found


# ------------------------------------------------------------------ binding + schemas

def make_code_tools(root: Path) -> Dict[str, Callable]:
    root = Path(root)
    return {
        "scaffold_file": lambda path, purpose, stubs=(), fill=(): scaffold_file(root, path, purpose, stubs, fill),
        "unscaffold_file": lambda path, name=None: unscaffold_file(root, path, name),
        "copy_lines": lambda src, start, end, dst, at_marker=None: copy_lines(root, src, start, end, dst, at_marker),
        "mark_change": lambda path, symbol, kind, what: mark_change(root, path, symbol, kind, what),
        "read_symbol": lambda path, name: read_symbol(root, path, name),
        "replace_symbol": lambda path, name, new_source: replace_symbol(root, path, name, new_source),
        "list_symbols": lambda path: list_symbols(root, path),
        "search_code": lambda pattern, path=".", regex=False: search_code(root, pattern, path, regex),
        "find_references": lambda name, path=".": find_references(root, name, path),
        "apply_patch": lambda diff: apply_patch(root, diff),
        "list_dir": lambda path=".": list_dir(root, path),
        "read_file": lambda path, start=None, end=None: read_file_range(root, path, start, end),
        "outline_file": lambda path=".": outline_file(root, path),
    }


def _fn(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties,
                                                            "required": required}}}


_PATH = {"type": "string", "description": "path relative to the project root"}
CODE_TOOL_SCHEMAS = [
    _fn("scaffold_file", "Create a file (or append to one) with a purpose line and STUBS: give each function's "
        "one-line declaration and what it does; the not-implemented body is generated. For non-code files "
        "(config, SQL, docs, Dockerfile) give `fill` lines saying what still needs writing. Test files: "
        "purpose only, no stubs.",
        {"path": _PATH, "purpose": {"type": "string"},
         "stubs": {"type": "array", "items": {"type": "object", "properties": {
             "signature": {"type": "string", "description": "one declaration line, e.g. 'def f(x: int) -> int:'"},
             "does": {"type": "string"}}, "required": ["signature", "does"]}},
         "fill": {"type": "array", "items": {"type": "string"}}},
        ["path", "purpose"]),
    _fn("unscaffold_file", "Delete a file you scaffolded, or one stub in it by name -- only while it is still a "
        "pure JFI stub.", {"path": _PATH, "name": {"type": "string"}}, ["path"]),
    _fn("copy_lines", "Copy lines start..end (1-based, inclusive) of src into dst without re-typing them: in place "
        "of the one JFI: fill line in dst that contains at_marker, or appended if at_marker is omitted. Use it for "
        "any verbatim move (markup, CSS, data); then edit only the seams.",
        {"src": _PATH, "start": {"type": "integer"}, "end": {"type": "integer"}, "dst": _PATH,
         "at_marker": {"type": "string", "description": "text that appears in exactly one JFI: line of dst"}},
        ["src", "start", "end", "dst"]),
    _fn("mark_change", "Mark an EXISTING function/class for Dev: kind 'change' (what to change) or 'delete' "
        "(why). Adds a JFI-CHANGE / JFI-DELETE line above it; the body is untouched.",
        {"path": _PATH, "symbol": {"type": "string"}, "kind": {"type": "string", "enum": ["change", "delete"]},
         "what": {"type": "string"}}, ["path", "symbol", "kind", "what"]),
    _fn("read_symbol", "Read one top-level function or class (with its decorators/comments) from a file.",
        {"path": _PATH, "name": {"type": "string"}}, ["path", "name"]),
    _fn("replace_symbol", "Replace one top-level function or class with new source (a stub with its real "
        "implementation, or a changed function). Remove its JFI marker as part of the new source.",
        {"path": _PATH, "name": {"type": "string"}, "new_source": {"type": "string"}},
        ["path", "name", "new_source"]),
    _fn("list_symbols", "List a file's top-level functions and classes with line ranges, and which are still "
        "JFI stubs or marked for change/delete.", {"path": _PATH}, ["path"]),
    _fn("search_code", "Search the project's files for text (or a regex) and get file:line matches.",
        {"pattern": {"type": "string"}, "path": _PATH, "regex": {"type": "boolean"}}, ["pattern"]),
    _fn("find_references", "Where a function or class is defined, imported and called across the project (or "
        "under path) -- comments and strings excluded. Use it before changing or deleting a function.",
        {"name": {"type": "string"}, "path": _PATH}, ["name"]),
    _fn("apply_patch", "Apply a unified diff (--- a/file, +++ b/file, @@ hunks) to one or more files in one call. "
        "All or nothing: every hunk's - and context lines must match the file exactly once, or nothing is "
        "written. Use it for several edits at once instead of many replace_in_file calls.",
        {"diff": {"type": "string"}}, ["diff"]),
    _fn("list_dir", "List one directory level of the project.", {"path": _PATH}, []),
    _fn("read_file", "Read a text file, or a numbered line range of it (start/end, 1-based, inclusive). Prefer "
        "read_symbol for one function.", {"path": _PATH, "start": {"type": "integer"}, "end": {"type": "integer"}},
        ["path"]),
    _fn("outline_file", "A cheap map, built in code. A folder: every file with its size and main contents. A "
        "file: its sections, blocks, functions/classes, headings or keys, with line numbers. Use it FIRST on any "
        "folder or file you haven't seen, then read only the ranges you need.", {"path": _PATH}, []),
]
