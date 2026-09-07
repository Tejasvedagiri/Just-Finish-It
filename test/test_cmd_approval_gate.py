"""Tests for the execute_command approval gate (Y / S / A / N)."""

from JFI.manager.abstract_manager import AbstractManager
from JFI.tool.cmd_tools import (
    CmdApprovalGate,
    get_approved_cmd_prefixes,
    make_gated_execute_command,
    request_cmd_approval,
)


class _Console(AbstractManager):
    """Stub console: feeds scripted answers to safe_get_user_input and
    records what was shown via display_system."""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.system_messages: list[str] = []

    def safe_get_user_input(self, prompt_label="You", **kwargs):
        return self.answers.pop(0)

    def display_system(self, text):
        self.system_messages.append(text)

    def display_assistant(self, *a, **k): pass
    def display_user(self, *a, **k): pass
    def get_user_input(self, *a, **k): pass
    def print_agent_response(self, *a, **k): pass


class TestRequestCmdApproval:

    def test_yes_runs_once_without_saving(self, tmp_path):
        cache = tmp_path / "context.json"
        console = _Console(answers=["y"])
        assert request_cmd_approval("ls -la", console, str(cache)) is True
        assert get_approved_cmd_prefixes(str(cache)) == []

    def test_no_does_not_run(self, tmp_path):
        cache = tmp_path / "context.json"
        console = _Console(answers=["n"])
        assert request_cmd_approval("rm -rf /", console, str(cache)) is False

    def test_invalid_answer_reprompts(self, tmp_path):
        cache = tmp_path / "context.json"
        console = _Console(answers=["banana", "y"])
        assert request_cmd_approval("ls", console, str(cache)) is True
        assert any("Please answer" in m for m in console.system_messages)

    def test_save_persists_prefix_and_second_call_is_silent(self, tmp_path):
        cache = tmp_path / "context.json"
        console = _Console(answers=["s"])
        assert request_cmd_approval("npm install", console, str(cache)) is True
        assert get_approved_cmd_prefixes(str(cache)) == ["npm"]

        # A fresh gate (no in-memory state) still auto-approves from the file.
        console2 = _Console(answers=[])
        assert request_cmd_approval("npm run build", console2, str(cache)) is True
        assert console2.answers == []  # never had to prompt

    def test_save_preserves_unrelated_keys_in_context_file(self, tmp_path):
        cache = tmp_path / "context.json"
        cache.write_text('{"db_schema": "users: id, email"}', encoding="utf-8")
        console = _Console(answers=["s"])
        request_cmd_approval("uv run pytest", console, str(cache))

        import json
        data = json.loads(cache.read_text(encoding="utf-8"))
        assert data["db_schema"] == "users: id, email"
        assert data["approved-cmd"] == ["uv"]


class TestCmdApprovalGateSessionState:

    def test_yes_for_all_skips_future_prompts_same_gate(self, tmp_path):
        cache = tmp_path / "context.json"
        console = _Console(answers=["a"])
        gate = CmdApprovalGate(console, str(cache))

        assert gate.request("pip install foo") is True
        # Second, unrelated command: no further answers queued, so a
        # prompt here would raise IndexError — proves it wasn't asked.
        assert gate.request("rm important_file.txt") is True
        assert get_approved_cmd_prefixes(str(cache)) == []  # "all" is not persisted

    def test_yes_for_all_does_not_leak_into_a_new_gate(self, tmp_path):
        cache = tmp_path / "context.json"
        gate1 = CmdApprovalGate(_Console(answers=["a"]), str(cache))
        gate1.request("ls")

        console2 = _Console(answers=["n"])
        gate2 = CmdApprovalGate(console2, str(cache))
        assert gate2.request("ls") is False


class TestMakeGatedExecuteCommand:

    def test_no_answer_does_not_invoke_subprocess(self, tmp_path):
        cache = tmp_path / "context.json"
        console = _Console(answers=["n"])
        gated = make_gated_execute_command(console, str(cache))
        result = gated("touch should_not_exist.txt")
        assert "not executed" in result.lower()
        assert not (tmp_path.parent / "should_not_exist.txt").exists()

    def test_yes_runs_the_real_command(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cache = tmp_path / "context.json"
        console = _Console(answers=["y"])
        gated = make_gated_execute_command(console, str(cache))
        result = gated("echo hello")
        assert result.startswith("Success:")
        assert "hello" in result
