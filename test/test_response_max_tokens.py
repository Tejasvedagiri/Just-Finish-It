"""RESPONSE_MAX_TOKEN: a single response that keeps generating past this cap
is treated as a runaway generation — print_agent_response aborts the stream
mid-way with ResponseTooLongError instead of letting it run to completion,
and runner._is_retryable_llm_error folds that into the normal LLM retry loop
(same path as a dropped connection) so the turn just gets re-sent.
"""

import types

import pytest

from JFI.manager.abstract_manager import ResponseTooLongError
from JFI.manager.pt_console_manager import DEFAULT_RESPONSE_MAX_TOKENS, PromptToolkitConsoleManager


def _new_manager() -> PromptToolkitConsoleManager:
    return PromptToolkitConsoleManager(title="max-tokens")


def _content_chunk(text):
    return types.SimpleNamespace(
        usage=None,
        choices=[types.SimpleNamespace(delta=types.SimpleNamespace(content=text, tool_calls=None))],
    )


def _tool_call_chunk(index, name=None, arguments=None):
    function = types.SimpleNamespace(name=name, arguments=arguments)
    tc = types.SimpleNamespace(index=index, id=f"call_{index}" if name else None, function=function)
    return types.SimpleNamespace(
        usage=None,
        choices=[types.SimpleNamespace(delta=types.SimpleNamespace(content=None, tool_calls=[tc]))],
    )


class TestMaxResponseTokensEnv:
    def test_defaults_to_ten_thousand(self, monkeypatch):
        monkeypatch.delenv("RESPONSE_MAX_TOKEN", raising=False)
        assert PromptToolkitConsoleManager._max_response_tokens() == DEFAULT_RESPONSE_MAX_TOKENS
        assert DEFAULT_RESPONSE_MAX_TOKENS == 10000

    def test_reads_env_override(self, monkeypatch):
        monkeypatch.setenv("RESPONSE_MAX_TOKEN", "42")
        assert PromptToolkitConsoleManager._max_response_tokens() == 42

    def test_malformed_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("RESPONSE_MAX_TOKEN", "not-a-number")
        assert PromptToolkitConsoleManager._max_response_tokens() == DEFAULT_RESPONSE_MAX_TOKENS


class TestPrintAgentResponseEnforcesCap:
    def test_response_under_cap_completes_normally(self, monkeypatch):
        monkeypatch.setenv("RESPONSE_MAX_TOKEN", "1000")
        console = _new_manager()
        chunks = [_content_chunk("hello "), _content_chunk("world")]

        result = console.print_agent_response(chunks)

        assert result["content"] == "hello world"

    def test_response_over_cap_raises_and_stops_consuming(self, monkeypatch):
        """A tiny cap plus an (in principle) endless generator: the raise
        must happen well before the generator is exhausted, proving the
        stream is actually abandoned rather than drained to the end."""
        monkeypatch.setenv("RESPONSE_MAX_TOKEN", "5")  # ~20 characters
        console = _new_manager()

        seen = []

        def endless_chunks():
            while True:
                seen.append(1)
                yield _content_chunk("x" * 50)

        with pytest.raises(ResponseTooLongError) as exc_info:
            console.print_agent_response(endless_chunks())

        assert "RESPONSE_MAX_TOKEN" in str(exc_info.value)
        assert len(seen) < 10  # aborted almost immediately, not drained

    def test_tool_call_arguments_count_toward_the_cap(self, monkeypatch):
        """A model that loops emitting huge tool-call arguments (not just
        plain text) must trip the same cap."""
        monkeypatch.setenv("RESPONSE_MAX_TOKEN", "5")
        console = _new_manager()

        def endless_tool_call_chunks():
            yield _tool_call_chunk(0, name="write_file", arguments="")
            while True:
                yield _tool_call_chunk(0, arguments="x" * 50)

        with pytest.raises(ResponseTooLongError):
            console.print_agent_response(endless_tool_call_chunks())

    def test_error_message_reports_configured_cap(self, monkeypatch):
        monkeypatch.setenv("RESPONSE_MAX_TOKEN", "5")
        console = _new_manager()

        def endless_chunks():
            while True:
                yield _content_chunk("x" * 50)

        with pytest.raises(ResponseTooLongError) as exc_info:
            console.print_agent_response(endless_chunks())

        assert "(5)" in str(exc_info.value)
