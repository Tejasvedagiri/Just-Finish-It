"""STREAM_OUTPUT_CAP: a single streamed response is abandoned (raising
ResponseTooLongError) once its running char/4 estimate crosses the cap, so
a runaway/looping generation can't stream forever burning tokens and
context. See PromptToolkitConsoleManager.print_agent_response."""

import pytest

from JFI.manager.abstract_manager import ResponseTooLongError
from JFI.manager.pt_console_manager import DEFAULT_STREAM_OUTPUT_CAP, PromptToolkitConsoleManager


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Chunk:
    def __init__(self, content=None, tool_calls=None):
        self.choices = [_Choice(_Delta(content, tool_calls))]


class _ToolCallDelta:
    def __init__(self, index, name=None, arguments=None, call_id=None):
        self.index = index
        self.id = call_id

        class _Function:
            pass

        self.function = _Function()
        self.function.name = name
        self.function.arguments = arguments


def test_default_cap_lets_a_normal_short_response_through():
    manager = PromptToolkitConsoleManager()
    chunks = [_Chunk("Hello, "), _Chunk("world!")]
    result = manager.print_agent_response(iter(chunks))
    assert result["content"] == "Hello, world!"


def test_content_past_the_cap_raises_response_too_long_error(monkeypatch):
    monkeypatch.setenv("STREAM_OUTPUT_CAP", "10")  # 10 tokens -> 40 chars
    manager = PromptToolkitConsoleManager()
    # 5 chunks of 20 chars = 100 chars = ~25 tokens, well past a 10-token cap.
    chunks = [_Chunk("x" * 20) for _ in range(5)]
    with pytest.raises(ResponseTooLongError):
        manager.print_agent_response(iter(chunks))


def test_tool_call_arguments_also_count_toward_the_cap(monkeypatch):
    monkeypatch.setenv("STREAM_OUTPUT_CAP", "10")
    manager = PromptToolkitConsoleManager()
    chunks = [
        _Chunk(tool_calls=[_ToolCallDelta(0, name="write_file", arguments="x" * 100, call_id="1")]),
    ]
    with pytest.raises(ResponseTooLongError):
        manager.print_agent_response(iter(chunks))


def test_unset_env_falls_back_to_the_documented_default(monkeypatch):
    monkeypatch.delenv("STREAM_OUTPUT_CAP", raising=False)
    manager = PromptToolkitConsoleManager()
    assert manager._stream_output_cap() == DEFAULT_STREAM_OUTPUT_CAP


def test_non_numeric_env_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("STREAM_OUTPUT_CAP", "not-a-number")
    manager = PromptToolkitConsoleManager()
    assert manager._stream_output_cap() == DEFAULT_STREAM_OUTPUT_CAP


def test_is_retryable_llm_error_treats_response_too_long_as_retryable():
    from JFI.runner import _is_retryable_llm_error

    assert _is_retryable_llm_error(ResponseTooLongError("too long")) is True
