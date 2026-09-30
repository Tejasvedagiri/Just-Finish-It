"""Prefix-matching edge cases for the approval gate's Save behaviour."""

from JFI.manager.abstract_manager import AbstractManager
from JFI.models import get_engine
from JFI.tool.cmd_tools import CmdApprovalGate, get_approved_cmd_prefixes


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


def _engine(tmp_path):
    return get_engine(tmp_path)


class TestPrefixMatching:

    def test_saved_prefix_matches_same_base_word_different_flags(self, tmp_path):
        engine = _engine(tmp_path)
        CmdApprovalGate(_Console(["s"]), engine, "demo").request("ls -la")
        assert get_approved_cmd_prefixes(engine, "demo") == ["ls"]

        # Different flags, same leading word: auto-approved, no prompt needed.
        console2 = _Console([])
        assert CmdApprovalGate(console2, engine, "demo").request("ls -R /tmp") is True

    def test_empty_approved_list_always_prompts(self, tmp_path):
        engine = _engine(tmp_path)
        console = _Console(["y"])
        assert CmdApprovalGate(console, engine, "demo").request("git status") is True
        # "y" (not "s") never wrote a prefix, so the list stays empty.
        assert get_approved_cmd_prefixes(engine, "demo") == []

    def test_multiple_saved_prefixes_all_match_independently(self, tmp_path):
        engine = _engine(tmp_path)
        CmdApprovalGate(_Console(["s"]), engine, "demo").request("npm install")
        CmdApprovalGate(_Console(["s"]), engine, "demo").request("uv run pytest")
        assert get_approved_cmd_prefixes(engine, "demo") == ["npm", "uv"]

        assert CmdApprovalGate(_Console([]), engine, "demo").request("npm run build") is True
        assert CmdApprovalGate(_Console([]), engine, "demo").request("uv sync") is True
        # An unrelated command still prompts.
        assert CmdApprovalGate(_Console(["n"]), engine, "demo").request("rm file.txt") is False

    def test_prefix_match_is_substring_not_whole_word(self, tmp_path):
        """Documents the current startswith() semantics: 'ls' also matches a
        command whose leading token merely starts with it, e.g. 'lsof' —
        not a bug fix here, just pinning the known behaviour."""
        engine = _engine(tmp_path)
        CmdApprovalGate(_Console(["s"]), engine, "demo").request("ls")
        assert CmdApprovalGate(_Console([]), engine, "demo").request("lsof -i") is True
