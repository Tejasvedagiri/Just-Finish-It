"""display_tool_call's argument preview: execute_command's command must
always show in full (knowing exactly what's running matters, especially for
a long-running or backgrounded command), while other tools' args still
truncate at 60 chars to keep the console from being flooded by a large
write_file/replace_in_file payload."""

from JFI.manager.pt_console_manager import PromptToolkitConsoleManager


def _new_manager() -> PromptToolkitConsoleManager:
    return PromptToolkitConsoleManager()


def _rendered_text(console: PromptToolkitConsoleManager) -> str:
    return "".join(text for _, text in console._output_fragments())


def test_execute_command_shows_full_command_even_when_long():
    console = _new_manager()
    long_command = "cat " + " ".join(f"file{i}.txt" for i in range(20))
    assert len(long_command) > 60

    console.display_tool_call("execute_command", {"command": long_command})

    assert long_command in _rendered_text(console)
    assert "…" not in _rendered_text(console)


def test_other_tools_still_truncate_long_args():
    console = _new_manager()
    console.display_tool_call("write_file", {"file_path": "a.py", "content": "x" * 200})

    text = _rendered_text(console)
    assert "…" in text
    assert "x" * 200 not in text


def test_execute_command_short_command_unaffected():
    console = _new_manager()
    console.display_tool_call("execute_command", {"command": "ls -la"})

    text = _rendered_text(console)
    assert "command=ls -la" in text
    assert "…" not in text
