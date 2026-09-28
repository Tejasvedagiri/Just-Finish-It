"""v2 code tools (laya_plan.md §4.5, §4.8, G2, G3, G11, G17, G18).

Lead and Task write to disk only through scaffold_file / unscaffold_file /
mark_change; Dev reads and edits one function at a time with read_symbol /
replace_symbol. Keeping reads to one symbol is what keeps a Dev episode
inside its 20k-token budget.

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
        if "{" in signature or "}" in signature:
            return None, "Error: give only the declaration line (no braces); the body is generated."
        return f"{signature} {{\n    {_comment(path, 'JFI: ' + does)}\n    {STUB_BODY[lang]}\n}}\n", ""
    return None, (f"Error: {path.suffix or path.name} files have no functions to stub; scaffold it as an "
                  "artifact (stubs=[] and `fill` lines) instead.")


def scaffold_file(root: Path, path: str, purpose: str, stubs: Sequence[dict] = (),
                  fill: Sequence[str] = ()) -> str:
    """Create or append to `path`: a purpose line, one stub per `stubs` item
    ({"signature", "does"}), and for artifacts one `JFI: <line>` per `fill`
    item (what still needs filling in)."""
    target, error = resolve_path(root, path)
    if error:
        return error
    if not purpose.strip():
        return "Error: give the file's purpose (one line)."
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


def unscaffold_file(root: Path, path: str, name: Optional[str] = None) -> str:
    """Remove a whole file that is still only stubs, or one stub by name."""
    target, error = resolve_path(root, path)
    if error:
        return error
    if not target.exists():
        return f"Error: {path} doesn't exist."
    text = target.read_text(encoding="utf-8")
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
        "mark_change": lambda path, symbol, kind, what: mark_change(root, path, symbol, kind, what),
        "read_symbol": lambda path, name: read_symbol(root, path, name),
        "replace_symbol": lambda path, name, new_source: replace_symbol(root, path, name, new_source),
        "list_symbols": lambda path: list_symbols(root, path),
        "search_code": lambda pattern, path=".", regex=False: search_code(root, pattern, path, regex),
        "list_dir": lambda path=".": list_dir(root, path),
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
    _fn("list_dir", "List one directory level of the project.", {"path": _PATH}, []),
]
