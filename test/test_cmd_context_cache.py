"""Unit tests for the approved-cmd context-cache helpers in cmd_tools.py."""

import json

from JFI.tool.cmd_tools import get_approved_cmd_prefixes, save_approved_cmd_prefix


class TestGetApprovedCmdPrefixes:

    def test_missing_file_returns_empty_list(self, tmp_path):
        assert get_approved_cmd_prefixes(str(tmp_path / "nope.json")) == []

    def test_malformed_json_returns_empty_list(self, tmp_path):
        p = tmp_path / "context.json"
        p.write_text("{not json", encoding="utf-8")
        assert get_approved_cmd_prefixes(str(p)) == []

    def test_non_list_value_is_ignored(self, tmp_path):
        p = tmp_path / "context.json"
        p.write_text('{"approved-cmd": "not-a-list"}', encoding="utf-8")
        assert get_approved_cmd_prefixes(str(p)) == []

    def test_reads_existing_prefixes(self, tmp_path):
        p = tmp_path / "context.json"
        p.write_text('{"approved-cmd": ["ls", "npm"]}', encoding="utf-8")
        assert get_approved_cmd_prefixes(str(p)) == ["ls", "npm"]


class TestSaveApprovedCmdPrefix:

    def test_creates_file_and_parent_dirs(self, tmp_path):
        p = tmp_path / "nested" / "context.json"
        save_approved_cmd_prefix("ls", str(p))
        assert json.loads(p.read_text(encoding="utf-8")) == {"approved-cmd": ["ls"]}

    def test_appends_without_duplicating(self, tmp_path):
        p = tmp_path / "context.json"
        save_approved_cmd_prefix("ls", str(p))
        save_approved_cmd_prefix("npm", str(p))
        save_approved_cmd_prefix("ls", str(p))
        assert get_approved_cmd_prefixes(str(p)) == ["ls", "npm"]

    def test_preserves_unrelated_keys(self, tmp_path):
        p = tmp_path / "context.json"
        p.write_text('{"db_schema": "users: id, email"}', encoding="utf-8")
        save_approved_cmd_prefix("uv", str(p))
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["db_schema"] == "users: id, email"
        assert data["approved-cmd"] == ["uv"]
