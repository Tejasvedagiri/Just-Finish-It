"""Prefix-matching edge cases for the approval gate's Save behaviour."""

from JFI.manager.abstract_manager import AbstractManager
from JFI.tool.cmd_tools import get_approved_cmd_prefixes, request_cmd_approval


class _Console(AbstractManager):
    def __init__(self, answers):
        self.answers = list(answers)

    def safe_get_user_input(self, prompt_label="You", **kwargs):
        return self.answers.pop(0)

    def display_system(self, *a, **k): pass
    def display_assistant(self, *a, **k): pass
    def display_user(self, *a, **k): pass
    def get_user_input(self, *a, **k): pass
    def print_agent_response(self, *a, **k): pass


class TestPrefixMatching:

    def test_saved_prefix_matches_same_base_word_different_flags(self, tmp_path):
        cache = tmp_path / "context.json"
        request_cmd_approval("ls -la", _Console(["s"]), str(cache))
        assert get_approved_cmd_prefixes(str(cache)) == ["ls"]

        # Different flags, same leading word: auto-approved, no prompt needed.
        console2 = _Console([])
        assert request_cmd_approval("ls -R /tmp", console2, str(cache)) is True

    def test_empty_approved_list_always_prompts(self, tmp_path):
        cache = tmp_path / "context.json"
        console = _Console(["y"])
        assert request_cmd_approval("git status", console, str(cache)) is True
        # "y" (not "s") never wrote a prefix, so the list stays empty.
        assert get_approved_cmd_prefixes(str(cache)) == []

    def test_multiple_saved_prefixes_all_match_independently(self, tmp_path):
        cache = tmp_path / "context.json"
        request_cmd_approval("npm install", _Console(["s"]), str(cache))
        request_cmd_approval("uv run pytest", _Console(["s"]), str(cache))
        assert get_approved_cmd_prefixes(str(cache)) == ["npm", "uv"]

        assert request_cmd_approval("npm run build", _Console([]), str(cache)) is True
        assert request_cmd_approval("uv sync", _Console([]), str(cache)) is True
        # An unrelated command still prompts.
        assert request_cmd_approval("rm file.txt", _Console(["n"]), str(cache)) is False

    def test_prefix_match_is_substring_not_whole_word(self, tmp_path):
        """Documents the current startswith() semantics: 'ls' also matches a
        command whose leading token merely starts with it, e.g. 'lsof' —
        not a bug fix here, just pinning the known behaviour."""
        cache = tmp_path / "context.json"
        request_cmd_approval("ls", _Console(["s"]), str(cache))
        assert request_cmd_approval("lsof -i", _Console([]), str(cache)) is True
