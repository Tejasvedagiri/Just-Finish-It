"""Unit tests for ask_llm (src/JFI/tool/llm_tools.py) — a stateless, single-
turn LLM call for one-off text work that doesn't warrant its own tool."""

from JFI.tool.llm_tools import ask_llm, make_ask_llm


class _Delta:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Chunk:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _UsageOnlyChunk:
    """A trailer chunk with no choices — print_agent_response's own comment
    notes some servers send one of these when stream_options.include_usage
    is requested; ask_llm doesn't request it, but must not crash if it
    still arrives."""
    choices = []


class _ScriptedLLM:
    def __init__(self, chunks, expected_messages=None):
        self.chunks = chunks
        self.expected_messages = expected_messages
        self.calls = []

    def send_message(self, messages, tools=None):
        self.calls.append((messages, tools))
        if self.expected_messages is not None:
            assert messages == self.expected_messages
        assert tools is None  # a stateless side call must never carry tool schemas
        return iter(self.chunks)


class _RaisingLLM:
    def send_message(self, messages, tools=None):
        raise RuntimeError("connection reset")


def test_concatenates_streamed_content():
    llm = _ScriptedLLM([_Chunk("Hel"), _Chunk("lo"), _Chunk(None)])
    assert ask_llm("Say hi", llm) == "Hello"


def test_sends_prompt_as_a_single_stateless_user_turn():
    llm = _ScriptedLLM(
        [_Chunk("ok")],
        expected_messages=[{"role": "user", "content": "Say hi"}],
    )
    ask_llm("Say hi", llm)
    assert len(llm.calls) == 1


def test_ignores_a_usage_only_trailer_chunk():
    llm = _ScriptedLLM([_Chunk("Hi"), _UsageOnlyChunk()])
    assert ask_llm("hello", llm) == "Hi"


def test_strips_surrounding_whitespace_from_the_reply():
    llm = _ScriptedLLM([_Chunk("  padded  \n")])
    assert ask_llm("hello", llm) == "padded"


def test_empty_prompt_is_an_error_and_never_calls_the_llm():
    llm = _ScriptedLLM([_Chunk("unused")])
    result = ask_llm("", llm)
    assert result.startswith("Error:")
    assert llm.calls == []


def test_whitespace_only_prompt_is_an_error():
    result = ask_llm("   \n  ", _ScriptedLLM([_Chunk("unused")]))
    assert result.startswith("Error:")


def test_empty_reply_is_reported_as_an_error():
    result = ask_llm("hello", _ScriptedLLM([_Chunk("")]))
    assert result.startswith("Error:")


def test_llm_exception_is_caught_and_reported_as_an_error():
    result = ask_llm("hello", _RaisingLLM())
    assert result.startswith("Error")  # _is_failure's own convention: no fixed "Error:" prefix
    assert "connection reset" in result


def test_make_ask_llm_binds_the_given_llm():
    llm = _ScriptedLLM([_Chunk("bound reply")])
    bound = make_ask_llm(llm)
    assert bound("hello") == "bound reply"
    assert len(llm.calls) == 1
