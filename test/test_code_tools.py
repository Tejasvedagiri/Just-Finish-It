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
