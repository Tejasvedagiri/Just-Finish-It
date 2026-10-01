"""Phase 4 of the v2 rewrite: the code tools (laya_plan.md §4.5, §4.8, G2,
G3, G11, G17, G18), on real files in a tmp project. The key property for the
edit tools is that everything outside the one symbol stays byte-identical."""
from __future__ import annotations

from pathlib import Path

import pytest

from JFI.tool.code_tools import (
    CODE_TOOL_SCHEMAS, list_dir, list_symbols, make_code_tools, mark_change, read_symbol, replace_symbol,
    scaffold_file, scan_markers, search_code, unscaffold_file,
)


@pytest.fixture
def root(tmp_path):
    return tmp_path


def read(root, path):
    return (Path(root) / path).read_text(encoding="utf-8")


# ------------------------------------------------------------------ scaffold

def test_python_stub_is_generated_not_written_by_the_model(root):
    result = scaffold_file(root, "app/db/session.py", "session handling",
                           [{"signature": "def get_session() -> Iterator[Session]:",
                             "does": "yield a SQLModel session bound to the engine"}])
    assert result.startswith("Created app/db/session.py")
    text = read(root, "app/db/session.py")
    assert text.startswith("# JFI-FILE: session handling\n")
    assert '    """JFI: yield a SQLModel session bound to the engine"""\n    raise NotImplementedError\n' in text


@pytest.mark.parametrize("path,signature,body", [
    ("src/search.ts", "export function search(q: string): Todo[]", 'throw new Error("not implemented");'),
    ("internal/store/redis.go", "func Get(ctx context.Context, code string) (string, error)",
     'panic("not implemented")'),
    ("src/scan/mod.rs", "pub fn hash_file(path: &Path) -> io::Result<String>", "todo!()"),
])
def test_brace_language_stubs(root, path, signature, body):
    scaffold_file(root, path, "purpose", [{"signature": signature, "does": "does a thing"}])
    text = read(root, path)
    assert f"{signature} {{\n    // JFI: does a thing\n    {body}\n}}\n" in text


def test_artifact_skeleton_uses_the_files_comment_syntax(root):
    scaffold_file(root, "pyproject.toml", "project manifest", fill=["add fastapi and sqlmodel to dependencies"])
    scaffold_file(root, "docs/guide.md", "user guide", fill=["write the install section"])
    scaffold_file(root, "db/schema.sql", "schema", fill=["create the todo table"])
    assert "# JFI: add fastapi and sqlmodel to dependencies" in read(root, "pyproject.toml")
    assert "<!-- JFI: write the install section -->" in read(root, "docs/guide.md")
    assert "-- JFI: create the todo table" in read(root, "db/schema.sql")


def test_test_file_gets_no_failing_stub(root):
    scaffold_file(root, "tests/test_session.py", "tests for app/db/session.py")
    assert read(root, "tests/test_session.py") == "# JFI-FILE: tests for app/db/session.py\n"
    assert scan_markers(root) == [], "a test file must not leave a stub that fails the suite"


def test_scaffold_appends_and_never_touches_existing_code(root):
    original = "import os\n\n\ndef existing(x):\n    return x + 1\n"
    (root / "util.py").write_text(original, encoding="utf-8")
    scaffold_file(root, "util.py", "helpers", [{"signature": "def new_helper(y: int) -> int:", "does": "double y"}])
    text = read(root, "util.py")
    assert text.startswith(original)
    assert "def new_helper(y: int) -> int:" in text


@pytest.mark.parametrize("stub,why", [
    ({"signature": "def f(x):\n    return x", "does": "d"}, "one declaration line"),
    ({"signature": "function f() { return 1 }", "does": "d"}, "no braces"),
    ({"signature": "x = 1", "does": "d"}, "'def name(...):'"),
])
def test_scaffold_refuses_anything_but_a_declaration(root, stub, why):
    path = "a.ts" if "function" in stub["signature"] else "a.py"
    result = scaffold_file(root, path, "p", [stub])
    assert result.startswith("Error") and why in result
    assert not (root / path).exists(), "nothing is written on a refusal"


def test_scaffold_refuses_a_duplicate_symbol(root):
    scaffold_file(root, "a.py", "p", [{"signature": "def f():", "does": "d"}])
    assert scaffold_file(root, "a.py", "p", [{"signature": "def f():", "does": "d"}]).startswith("Error")


# ------------------------------------------------------------------ path safety (G18)

@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd", ".jfi/JFI.db", ".git/config"])
def test_paths_outside_the_project_or_in_jfi_and_git_are_refused(root, path):
    for result in (scaffold_file(root, path, "p", [{"signature": "def f():", "does": "d"}]),
                   read_symbol(root, path, "f"), mark_change(root, path, "f", "change", "x")):
        assert result.startswith("Error"), result


# ------------------------------------------------------------------ read / replace

PY_FILE = '''"""module"""
import os


@decorator
def first(a):
    return a


def second(b):
    """JFI: return b doubled"""
    raise NotImplementedError


class Third:
    def method(self):
        return 3
'''


def test_python_read_symbol_includes_decorators(root):
    (root / "m.py").write_text(PY_FILE, encoding="utf-8")
    result = read_symbol(root, "m.py", "first")
    assert "@decorator\ndef first(a):\n    return a" in result
    assert read_symbol(root, "m.py", "nope").startswith("Error")


def test_python_replace_symbol_keeps_the_rest_byte_identical(root):
    (root / "m.py").write_text(PY_FILE, encoding="utf-8")
    result = replace_symbol(root, "m.py", "second", "def second(b):\n    return b * 2\n")
    assert result.startswith("Replaced 'second'")
    text = read(root, "m.py")
    assert text == PY_FILE.replace('    """JFI: return b doubled"""\n    raise NotImplementedError\n',
                                   "    return b * 2\n")


def test_python_replace_that_would_not_parse_writes_nothing(root):
    (root / "m.py").write_text(PY_FILE, encoding="utf-8")
    assert replace_symbol(root, "m.py", "second", "def second(b)\n    return b").startswith("Error")
    assert read(root, "m.py") == PY_FILE


TS_FILE = '''import { x } from "./x";

// keeps the braces in strings and comments out of the count: "}" '{' /* } */
export function first(a: number): number {
  const s = "}{";
  const t = `${a} }`;
  return a; // }
}

export const second = (b: number): number => {
  // JFI: return b doubled
  throw new Error("not implemented");
};

export class Third {
  method() { return 3; }
}
'''


def test_ts_symbols_and_brace_matching_skip_strings_and_comments(root):
    (root / "m.ts").write_text(TS_FILE, encoding="utf-8")
    names = list_symbols(root, "m.ts")
    assert "first" in names and "second" in names and "Third" in names
    assert "second (lines" in names and "[JFI stub]" in names.split("second")[1].split("\n")[0]
    first = read_symbol(root, "m.ts", "first")
    assert first.rstrip().endswith("return a; // }\n}")


def test_ts_replace_symbol_keeps_the_rest_byte_identical(root):
    (root / "m.ts").write_text(TS_FILE, encoding="utf-8")
    new = "export const second = (b: number): number => {\n  return b * 2;\n};"
    assert replace_symbol(root, "m.ts", "second", new).startswith("Replaced")
    old_block = ('export const second = (b: number): number => {\n  // JFI: return b doubled\n'
                 '  throw new Error("not implemented");\n};\n')
    assert read(root, "m.ts") == TS_FILE.replace(old_block, new + "\n")


GO_FILE = '''package store

// Get returns the URL for a code.
func (s *Store) Get(code string) (string, error) {
\tif code == "}" {
\t\treturn "", nil
\t}
\treturn s.m[code], nil
}

func Put(code string) {
\t// JFI: store the code
\tpanic("not implemented")
}
'''


def test_go_read_and_replace(root):
    (root / "s.go").write_text(GO_FILE, encoding="utf-8")
    got = read_symbol(root, "s.go", "Get")
    assert got.split(":\n", 1)[1].startswith("// Get returns the URL for a code.\nfunc (s *Store) Get")
    replace_symbol(root, "s.go", "Put", "func Put(code string) {\n\ts.m[code] = code\n}")
    assert read(root, "s.go").startswith(GO_FILE.split("func Put")[0])
    assert "panic" not in read(root, "s.go")


RS_FILE = '''use std::path::Path;

#[inline]
pub fn first<'a>(s: &'a str) -> &'a str {
    let c = '}';
    s
}

pub fn second() -> u8 {
    // JFI: return two
    todo!()
}
'''


def test_rust_lifetimes_and_char_literals_do_not_break_matching(root):
    (root / "l.rs").write_text(RS_FILE, encoding="utf-8")
    first = read_symbol(root, "l.rs", "first")
    assert "#[inline]" in first and first.rstrip().endswith("s\n}")
    assert "[JFI stub]" in list_symbols(root, "l.rs")


# ------------------------------------------------------------------ mark_change (G3)

def test_mark_change_puts_a_marker_above_and_leaves_the_body(root):
    (root / "m.py").write_text(PY_FILE, encoding="utf-8")
    assert mark_change(root, "m.py", "first", "change", "sort overdue first").startswith("Marked")
    text = read(root, "m.py")
    assert "# JFI-CHANGE: sort overdue first\n@decorator\ndef first(a):\n    return a\n" in text
    mark_change(root, "m.py", "Third", "delete", "unused since v2")
    assert "# JFI-DELETE: unused since v2\nclass Third:" in read(root, "m.py")
    assert mark_change(root, "m.py", "missing", "change", "x").startswith("Error")
    assert mark_change(root, "m.py", "first", "rename", "x").startswith("Error")


def test_marked_symbol_is_still_readable_with_its_marker(root):
    (root / "m.ts").write_text(TS_FILE, encoding="utf-8")
    mark_change(root, "m.ts", "first", "change", "accept a string too")
    assert "// JFI-CHANGE: accept a string too" in read_symbol(root, "m.ts", "first")
    assert "[JFI-CHANGE]" in list_symbols(root, "m.ts")


# ------------------------------------------------------------------ unscaffold

def test_unscaffold_deletes_only_pure_stubs(root):
    scaffold_file(root, "a.py", "p", [{"signature": "def f():", "does": "d"}, {"signature": "def g():", "does": "e"}])
    assert unscaffold_file(root, "a.py", "f").startswith("Removed stub 'f'")
    assert "def f" not in read(root, "a.py") and "def g" in read(root, "a.py")
    assert unscaffold_file(root, "a.py").startswith("Deleted a.py")
    assert not (root / "a.py").exists()


def test_unscaffold_refuses_real_code(root):
    (root / "m.py").write_text(PY_FILE, encoding="utf-8")
    assert unscaffold_file(root, "m.py").startswith("Error")
    assert unscaffold_file(root, "m.py", "first").startswith("Error")
    assert read(root, "m.py") == PY_FILE


# ------------------------------------------------------------------ search / list / scan

def test_search_code_skips_heavy_dirs_and_reports_file_line(root):
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("x = 1\nneedle = 2\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "b.js").write_text("needle\n", encoding="utf-8")
    result = search_code(root, "needle")
    assert "src/a.py:2: needle = 2" in result and "node_modules" not in result
    assert search_code(root, "nothing-here").startswith("No matches")
    assert search_code(root, "(", regex=True).startswith("Error")


def test_list_dir_hides_git_and_jfi(root):
    for d in (".git", ".jfi", "src"):
        (root / d).mkdir()
    (root / "README.md").write_text("hi", encoding="utf-8")
    listing = list_dir(root)
    assert "src/" in listing and "README.md (2 bytes)" in listing
    assert ".git" not in listing and ".jfi" not in listing


def test_scan_markers_finds_leftover_work(root):
    scaffold_file(root, "a.py", "p", [{"signature": "def f():", "does": "d"}])
    (root / "b.py").write_text("def g():\n    pass\n", encoding="utf-8")
    mark_change(root, "b.py", "g", "delete", "unused")
    found = scan_markers(root)
    assert ("a.py", 4, "JFI:") in found and ("b.py", 1, "JFI-DELETE:") in found
    assert all("JFI-FILE" not in m for _, _, m in found)


def test_factory_and_schemas_cover_the_same_tools(root):
    tools = make_code_tools(root)
    assert set(tools) == {s["function"]["name"] for s in CODE_TOOL_SCHEMAS}
    tools["scaffold_file"]("x.py", "p", [{"signature": "def h():", "does": "d"}])
    assert "[JFI stub]" in tools["list_symbols"]("x.py")


# ------------------------------------------------------------------ outline (map before reading)

def _html(views=12, css_lines=320, js_functions=15):
    css = "\n".join(f"  .c{i}{{color:red}}" for i in range(css_lines))
    sections = "\n".join(f'<!-- ========== VIEW {i} ========== -->\n<section class="view" id="view-{i}">\n'
                         + "\n".join(f"  <p>row {j}</p>" for j in range(30)) + "\n</section>" for i in range(views))
    js = "\n".join(f"function f{i}(){{\n  return {i};\n}}" for i in range(js_functions))
    return f"<!DOCTYPE html>\n<html>\n<head>\n<style>\n{css}\n</style>\n</head>\n<body>\n{sections}\n" \
           f"<script>\n/* ---------- helpers ---------- */\n{js}\n</script>\n</body>\n</html>\n"


def test_a_big_file_read_whole_returns_its_outline(root):
    """Observed on the stui run: the Architect's first move was a whole-file
    read of a 1,621-line HTML page; it got the first ~3k tokens of CSS and
    spent its budget reading the rest in chunks, ending with no plan."""
    from JFI.tool.code_tools import read_file_range
    (Path(root) / "page.html").write_text(_html(), encoding="utf-8")
    text = read_file_range(root, "page.html")
    assert "too big to read whole" in text
    assert '<section class="view" id="view-11">' in text and "<style>  (" in text and "f14()" in text
    assert "-- VIEW 3" in text and "-- helpers" in text
    assert len(text) < 6000


def test_outline_is_structure_for_any_kind_of_file(root):
    from JFI.tool.code_tools import outline_file
    files = {
        "svc.py": "class Store:\n    def get(self):\n        pass\n\n    def put(self):\n        pass\n\n\ndef main():\n    pass\n",
        "README.md": "# Title\n\n## Install\n\n```bash\n# not a heading\npip install x\n```\n\n## Usage\n",
        "notes.rst": "Intro\n=====\n\ntext\n\nDetails\n-------\n",
        "app.toml": "[server]\nport = 1\n\n[db]\nurl = 'x'\n",
        "schema.sql": "CREATE TABLE users (id int);\nCREATE INDEX idx ON users(id);\n",
        "styles.css": "/* ---- layout ---- */\n.a{}\n@media (max-width: 600px) {\n.a{}\n}\n",
        "data.json": '{"name": "x", "items": [1, 2, 3]}',
    }
    for name, text in files.items():
        (Path(root) / name).write_text(text, encoding="utf-8")
    assert all(s in outline_file(root, "svc.py") for s in ("class Store", ".get()", ".put()", "def main()"))
    md = outline_file(root, "README.md")
    assert "Install" in md and "Usage" in md and "not a heading" not in md
    assert "Intro" in outline_file(root, "notes.rst") and "Details" in outline_file(root, "notes.rst")
    assert "[server]" in outline_file(root, "app.toml") and "[db]" in outline_file(root, "app.toml")
    assert "TABLE users" in outline_file(root, "schema.sql") and "INDEX idx" in outline_file(root, "schema.sql")
    css = outline_file(root, "styles.css")
    assert "-- layout" in css and "@media (max-width: 600px)" in css
    assert "items (3 items)" in outline_file(root, "data.json")


def test_unstructured_big_text_is_chunked_by_size_not_just_lines(root):
    """Observed while building this: a 131-line, 92 KB JSONL file was
    outlined as "short, read it whole" -- line count alone misses it."""
    from JFI.tool.code_tools import outline_file
    rows = "\n".join('{"goal": "' + "x" * 700 + f'", "n": {i}}}' for i in range(130))
    (Path(root) / "data.jsonl").write_text(rows, encoding="utf-8")
    text = outline_file(root, "data.jsonl")
    assert text.count("\n  L") >= 10 and "read it whole" not in text


def test_folder_outline_is_a_repo_map(root):
    from JFI.tool.code_tools import outline_file
    (Path(root) / "pkg").mkdir()
    (Path(root) / "pkg/api.py").write_text("def list_todos():\n    pass\n", encoding="utf-8")
    (Path(root) / "README.md").write_text("# App\n## Run\n", encoding="utf-8")
    (Path(root) / "node_modules").mkdir()
    (Path(root) / "node_modules/x.js").write_text("function x(){}", encoding="utf-8")
    text = outline_file(root, ".")
    assert "pkg/api.py" in text and "def list_todos()" in text and "README.md" in text and "App" in text
    assert "node_modules" not in text.split("\n", 1)[1]


def test_scaffold_normalises_what_models_actually_send(root):
    """Observed on the stui run: Lead wrote the header itself ("JFI-FILE: …",
    giving "JFI-FILE: JFI-FILE: …"), ended a JS declaration with "{" (refused)
    or Python's ":" (accepted as `f(): {`, invalid JS) -- and spent its turns
    deleting and redoing files."""
    text = scaffold_file(root, "view.js", "JFI-FILE: Performance view", [
        {"signature": "export function a() {", "does": "x"},
        {"signature": "export function b():", "does": "y"}])
    assert text.startswith("Created")
    body = read(root, "view.js")
    assert body.count("JFI-FILE:") == 1
    assert "export function a() {" in body and "export function b() {" in body and "):" not in body


def test_typescript_annotations_are_stripped_from_plain_js_stubs(root):
    """Observed on the stui run: a Lead wrote `getSettings(): object` and
    `saveSetting(key: string, value): void` into a .js file."""
    scaffold_file(root, "settings.js", "Settings view", [
        {"signature": "export function getSettings(): object", "does": "a"},
        {"signature": "export function saveSetting(key: string, value): void", "does": "b"},
        {"signature": "export function initSettings(root = document): void", "does": "c"},
        {"signature": "export function pick({ a, b }, list: Array<string> = []): Map<string, number>", "does": "d"}])
    body = read(root, "settings.js")
    assert "export function getSettings() {" in body
    assert "export function saveSetting(key, value) {" in body
    assert "export function initSettings(root = document) {" in body
    assert "export function pick({ a, b }, list = []) {" in body
    scaffold_file(root, "keep.ts", "TS keeps its types", [{"signature": "export function f(a: string): void", "does": "x"}])
    assert "f(a: string): void {" in read(root, "keep.ts")


def test_copy_lines_moves_text_without_the_model_retyping_it(root):
    """Observed on the stui run: a conversion plan is mostly verbatim moves
    (index.html body = reference L341-957), and Dev could only move text by
    re-typing it through write_file (G29)."""
    from JFI.tool.code_tools import copy_lines
    (Path(root) / "ref.html").write_text("".join(f"line {i}\n" for i in range(1, 11)), encoding="utf-8")
    scaffold_file(root, "index.html", "page", fill=["body: copy ref L3-5", "script tag"])
    result = copy_lines(root, "ref.html", 3, 5, "index.html", at_marker="body: copy")
    assert result.startswith("Copied ref.html lines 3-5 (3 lines)")
    body = read(root, "index.html")
    assert "line 3\nline 4\nline 5\n" in body and "body: copy" not in body and "script tag" in body
    assert copy_lines(root, "ref.html", 9, 12, "index.html").startswith("Error: ref.html has 10 lines")
    assert copy_lines(root, "ref.html", 1, 2, "index.html", at_marker="nothing like it").startswith("Error: 0 JFI:")
    assert copy_lines(root, "ref.html", 1, 1, "../outside.txt").startswith("Error")
    assert copy_lines(root, "ref.html", 1, 2, "new/copy.txt").startswith("Copied")
    assert read(root, "new/copy.txt") == "line 1\nline 2\n"


def test_a_constant_is_not_stubbed_as_a_function(root):
    """Observed on the stui run (gemma): Lead stubbed `export const TITLES`
    (a data table) and the tool wrapped it in a `{ throw ... }` body, which
    is invalid JavaScript. Constants go in fill lines."""
    refused = scaffold_file(root, "data.js", "data", [{"signature": "export const TITLES", "does": "titles map"}])
    assert refused.startswith("Error: 'export const TITLES' isn't a function declaration")
    assert not (Path(root) / "data.js").exists()
    for sig in ("export function f(x)", "export const f = (x) =>", "export async function g()"):
        assert scaffold_file(root, "ok.js", "p", [{"signature": sig, "does": "d"}]).startswith(("Created", "Appended"))
    assert scaffold_file(root, "m.go", "p", [{"signature": "func Run(x int) error", "does": "d"}]).startswith("Created")
    assert scaffold_file(root, "m.rs", "p", [{"signature": "pub fn run(x: i32)", "does": "d"}]).startswith("Created")


def test_json_is_scaffolded_as_valid_json(root):
    """Observed on the stui run (neo-coder): package.json was scaffolded with
    "#" comments, which JSON doesn't allow, and a Lead spent two whole
    conversations creating, reading and deleting it without adding its node."""
    import json
    from JFI.tool.code_tools import scan_markers
    result = scaffold_file(root, "package.json", "JFI-FILE: npm manifest", fill=["name + scripts", "devDependencies"])
    assert result.startswith("Created package.json with 2 fill line(s) (valid JSON)")
    data = json.loads(read(root, "package.json"))
    assert data == {"//": "JFI-FILE: npm manifest", "//fill": ["JFI: name + scripts", "JFI: devDependencies"]}
    scaffold_file(root, "package.json", "ignored on append", fill=["engines"])
    assert json.loads(read(root, "package.json"))["//fill"][-1] == "JFI: engines"
    assert len([m for m in scan_markers(root) if m[0] == "package.json"]) == 3
    assert unscaffold_file(root, "package.json").startswith("Deleted package.json")
    (Path(root) / "real.json").write_text('{"name": "x"}', encoding="utf-8")
    scaffold_file(root, "real.json", "p", fill=["version"])
    assert unscaffold_file(root, "real.json").startswith("Error")
    assert scaffold_file(root, "bad.json", "p", [{"signature": "export function f()", "does": "x"}]).startswith("Error")


# ---------------------------------------------------------------- find_references / apply_patch

def test_find_references_separates_definitions_calls_and_imports_and_skips_comments(tmp_path):
    """A change or delete leaf needs every caller; search_code also returned
    comments, strings and look-alike names."""
    from JFI.tool.code_tools import find_references
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "ops.py").write_text("def add(a, b):\n    return a + b\n\n\ndef add_all(xs):\n"
                                              "    # add every item\n    return sum(add(0, x) for x in xs)\n")
    (tmp_path / "main.py").write_text('from calc.ops import add\n\nprint(add(1, 2), "add")\n')
    out = find_references(tmp_path, "add")
    assert "definitions (1):\n  calc/ops.py:1: def add(a, b):" in out
    assert "imports (1):\n  main.py:1: from calc.ops import add" in out
    assert "calc/ops.py:7:" in out and "main.py:3:" in out
    assert "calc/ops.py:6" not in out  # the comment
    assert "add_all" not in out.split("definitions")[1].split("calls")[0]  # a look-alike name


def test_apply_patch_is_all_or_nothing(tmp_path):
    from JFI.tool.code_tools import apply_patch
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n")
    (tmp_path / "b.py").write_text("z = 3\n")
    good = ("--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 20\n"
            "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-z = 3\n+z = 30\n"
            "--- /dev/null\n+++ b/c.py\n@@ -0,0 +1 @@\n+w = 4\n")
    assert apply_patch(tmp_path, good).startswith("Applied the patch to 3 file(s)")
    assert (tmp_path / "a.py").read_text() == "x = 1\ny = 20\n"
    assert (tmp_path / "b.py").read_text() == "z = 30\n" and (tmp_path / "c.py").read_text() == "w = 4\n"

    stale = ("--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 10\n"
             "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-z = 3\n+z = 300\n")  # b.py no longer has z = 3
    answer = apply_patch(tmp_path, stale)
    assert answer.startswith("Error: a hunk isn't in b.py") and "nothing was changed" in answer
    assert (tmp_path / "a.py").read_text() == "x = 1\ny = 20\n", "the first file wasn't written either"
