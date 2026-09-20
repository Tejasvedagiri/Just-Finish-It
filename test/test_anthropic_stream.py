"""AnthropicStream (src/JFI/llm/anthropic_stream.py) -- Claude's Messages
API is NOT OpenAI-compatible, so this translates both directions: JFI's
own OpenAI-shaped messages/tools going out, and Anthropic's native
streaming events translated into the same duck-typed "OpenAI chunk" shape
coming back (see the module's own docstring for the full list of shape
differences this bridges).
"""

import json

import pytest

from JFI.llm.anthropic_stream import (
    AnthropicStream,
    _fake_openai_chunk,
    _image_block_from_data_url,
    _split_system,
    _stream_events,
    _translate_messages,
    _translate_tools,
)


# ---------------------------------------------------------------------------
# Outgoing translation: JFI's OpenAI-shaped messages/tools -> Anthropic's
# ---------------------------------------------------------------------------

class TestSplitSystem:
    def test_pulls_the_leading_system_message_out(self):
        messages = [{"role": "system", "content": "be helpful"}, {"role": "user", "content": "hi"}]
        system_text, rest = _split_system(messages)
        assert system_text == "be helpful"
        assert rest == [{"role": "user", "content": "hi"}]

    def test_no_system_message_returns_empty_string(self):
        messages = [{"role": "user", "content": "hi"}]
        system_text, rest = _split_system(messages)
        assert system_text == ""
        assert rest == messages

    def test_only_a_leading_system_message_is_pulled_never_a_later_one(self):
        """A 'system'-role message appearing mid-history (shouldn't happen in
        practice, but must never be silently merged into the real system
        prompt) stays in the regular message list."""
        messages = [
            {"role": "system", "content": "real prompt"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "not the real system prompt"},
        ]
        system_text, rest = _split_system(messages)
        assert system_text == "real prompt"
        assert rest == messages[1:]


class TestTranslateTools:
    def test_none_or_empty_returns_none(self):
        assert _translate_tools(None) is None
        assert _translate_tools([]) is None

    def test_unwraps_the_openai_function_wrapper(self):
        openai_tools = [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Reads a file.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }]
        [tool] = _translate_tools(openai_tools)
        assert tool == {
            "name": "read_file",
            "description": "Reads a file.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }


class TestImageBlockFromDataUrl:
    def test_parses_media_type_and_base64_payload(self):
        block = _image_block_from_data_url("data:image/png;base64,AAAA")
        assert block == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}

    def test_non_data_url_returns_none(self):
        assert _image_block_from_data_url("https://example.com/x.png") is None


class TestTranslateMessages:
    def test_plain_user_and_assistant_text_pass_through(self):
        messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        assert _translate_messages(messages) == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        ]

    def test_assistant_tool_calls_become_tool_use_blocks_with_parsed_input(self):
        messages = [{
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})},
            }],
        }]
        [translated] = _translate_messages(messages)
        assert translated == {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a.py"}}],
        }

    def test_assistant_with_both_text_and_a_tool_call_keeps_both_blocks(self):
        messages = [{
            "role": "assistant", "content": "Let me check that file.",
            "tool_calls": [{"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}}],
        }]
        [translated] = _translate_messages(messages)
        assert translated["content"][0] == {"type": "text", "text": "Let me check that file."}
        assert translated["content"][1]["type"] == "tool_use"

    def test_malformed_tool_call_arguments_json_becomes_empty_input_not_a_crash(self):
        messages = [{
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "x", "arguments": "not json"}}],
        }]
        [translated] = _translate_messages(messages)
        assert translated["content"][0]["input"] == {}

    def test_single_tool_result_becomes_a_user_message_with_one_block(self):
        messages = [{"role": "tool", "tool_call_id": "call_1", "content": "file contents here"}]
        assert _translate_messages(messages) == [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "file contents here"}]},
        ]

    def test_consecutive_tool_results_merge_into_one_user_message(self):
        """Anthropic requires every tool_result for one assistant turn to
        land in a SINGLE following user message -- JFI's own history has
        one {"role": "tool", ...} entry per call, so multiple in a row must
        merge, not become multiple separate (invalid) user turns."""
        messages = [
            {"role": "tool", "tool_call_id": "call_1", "content": "result one"},
            {"role": "tool", "tool_call_id": "call_2", "content": "result two"},
        ]
        [translated] = _translate_messages(messages)
        assert translated["role"] == "user"
        assert translated["content"] == [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "result one"},
            {"type": "tool_result", "tool_use_id": "call_2", "content": "result two"},
        ]

    def test_a_real_user_message_between_two_tool_results_starts_a_new_batch(self):
        messages = [
            {"role": "tool", "tool_call_id": "call_1", "content": "result one"},
            {"role": "user", "content": "thanks"},
            {"role": "tool", "tool_call_id": "call_2", "content": "result two"},
        ]
        translated = _translate_messages(messages)
        assert len(translated) == 3
        assert translated[0]["content"] == [{"type": "tool_result", "tool_use_id": "call_1", "content": "result one"}]
        assert translated[2]["content"] == [{"type": "tool_result", "tool_use_id": "call_2", "content": "result two"}]

    def test_user_message_with_image_content_translates_to_an_image_block(self):
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "see this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }]
        [translated] = _translate_messages(messages)
        assert translated["content"] == [
            {"type": "text", "text": "see this"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        ]


# ---------------------------------------------------------------------------
# Incoming translation: Anthropic's native stream events -> fake OpenAI chunks
# ---------------------------------------------------------------------------

class _FakeEvent:
    def __init__(self, type, **kwargs):
        self.type = type
        for k, v in kwargs.items():
            setattr(self, k, v)


class _NS:
    """Tiny attribute bag -- stands in for Anthropic's own typed objects
    (TextBlock, ToolUseBlock, Usage, ...) wherever only a couple of fields
    matter to the code under test."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeMessagesStream:
    """Stands in for `client.messages.stream(...)`'s context manager,
    yielding a canned sequence of events."""
    def __init__(self, events):
        self._events = events

    def __enter__(self):
        return iter(self._events)

    def __exit__(self, *a):
        return False


class _FakeMessages:
    def __init__(self, events):
        self._events = events

    def stream(self, **kwargs):
        return _FakeMessagesStream(self._events)


class _FakeClient:
    def __init__(self, events):
        self.messages = _FakeMessages(events)


class TestFakeOpenAIChunk:
    def test_usage_only_chunk_has_empty_choices(self):
        chunk = _fake_openai_chunk(usage=_NS(prompt_tokens=1, completion_tokens=2), usage_only=True)
        assert chunk.choices == []
        assert chunk.usage.prompt_tokens == 1

    def test_content_chunk_shape_matches_openai(self):
        chunk = _fake_openai_chunk(content="hi")
        assert chunk.choices[0].delta.content == "hi"
        assert chunk.choices[0].delta.tool_calls is None


class TestStreamEvents:
    def test_text_delta_becomes_a_content_chunk(self):
        events = [
            _FakeEvent("message_start", message=_NS(usage=_NS(input_tokens=10))),
            _FakeEvent("content_block_delta", index=0, delta=_NS(type="text_delta", text="Hello")),
        ]
        client = _FakeClient(events)
        chunks = list(_stream_events(client, model="x", messages=[]))
        text_chunks = [c for c in chunks if c.choices and c.choices[0].delta.content]
        assert text_chunks[0].choices[0].delta.content == "Hello"

    def test_tool_use_start_then_input_json_deltas_accumulate_like_openai(self):
        events = [
            _FakeEvent("message_start", message=_NS(usage=_NS(input_tokens=10))),
            _FakeEvent("content_block_start", index=0, content_block=_NS(type="tool_use", id="call_1", name="read_file")),
            _FakeEvent("content_block_delta", index=0, delta=_NS(type="input_json_delta", partial_json='{"path": ')),
            _FakeEvent("content_block_delta", index=0, delta=_NS(type="input_json_delta", partial_json='"a.py"}')),
        ]
        client = _FakeClient(events)
        chunks = [c for c in _stream_events(client, model="x", messages=[]) if c.choices]

        # First chunk: real id/name, empty arguments (matches OpenAI's own
        # shape -- see pt_console_manager.py's tool_calls_dict init).
        first_tc = chunks[0].choices[0].delta.tool_calls[0]
        assert first_tc.id == "call_1"
        assert first_tc.function.name == "read_file"
        assert first_tc.function.arguments == ""

        # Later chunks: id/name absent, arguments accumulate.
        assembled = "".join(c.choices[0].delta.tool_calls[0].function.arguments for c in chunks[1:])
        assert assembled == '{"path": "a.py"}'
        assert chunks[1].choices[0].delta.tool_calls[0].id is None

    def test_message_delta_yields_a_usage_only_trailer_chunk(self):
        events = [
            _FakeEvent("message_start", message=_NS(usage=_NS(input_tokens=42))),
            _FakeEvent("message_delta", usage=_NS(output_tokens=7)),
        ]
        client = _FakeClient(events)
        chunks = list(_stream_events(client, model="x", messages=[]))
        usage_chunks = [c for c in chunks if not c.choices]
        assert len(usage_chunks) == 1
        assert usage_chunks[0].usage.prompt_tokens == 42
        assert usage_chunks[0].usage.completion_tokens == 7

    def test_thinking_delta_maps_to_reasoning_content(self):
        events = [
            _FakeEvent("message_start", message=_NS(usage=_NS(input_tokens=1))),
            _FakeEvent("content_block_delta", index=0, delta=_NS(type="thinking_delta", thinking="pondering...")),
        ]
        client = _FakeClient(events)
        [chunk] = [c for c in _stream_events(client, model="x", messages=[]) if c.choices]
        assert chunk.choices[0].delta.reasoning_content == "pondering..."
        assert chunk.choices[0].delta.content is None


# ---------------------------------------------------------------------------
# AnthropicStream itself
# ---------------------------------------------------------------------------

class TestAnthropicStreamInit:
    def test_missing_api_key_raises_a_clear_error(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(KeyError, match="ANTHROPIC_API_KEY"):
            AnthropicStream()

    def test_missing_api_key_for_a_phase_names_the_prefixed_var_too(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("REVIEWER_ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(KeyError, match="REVIEWER_ANTHROPIC_API_KEY"):
            AnthropicStream("REVIEWER")

    def test_constructs_a_real_client_when_key_is_present(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
        stream = AnthropicStream()
        assert stream.client is not None
        assert stream.max_tokens == 8192  # default

    def test_max_tokens_overridable_via_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
        monkeypatch.setenv("ANTHROPIC_MAX_TOKENS", "2048")
        assert AnthropicStream().max_tokens == 2048


class TestAnthropicStreamSendMessage:
    def test_builds_correct_kwargs_and_delegates_to_stream_events(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
        monkeypatch.setenv("MODEL", "claude-sonnet-5")
        stream = AnthropicStream()

        captured = {}

        def fake_stream_events(client, **kwargs):
            captured.update(kwargs)
            return iter([])

        monkeypatch.setattr("JFI.llm.anthropic_stream._stream_events", fake_stream_events)

        messages = [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "hi"},
        ]
        list(stream.send_message(messages))

        assert captured["model"] == "claude-sonnet-5"
        assert captured["system"] == "be helpful"
        assert captured["messages"] == [{"role": "user", "content": "hi"}]
        assert "tools" not in captured

    def test_tools_omitted_entirely_when_none_given(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
        stream = AnthropicStream()
        captured = {}
        monkeypatch.setattr(
            "JFI.llm.anthropic_stream._stream_events",
            lambda client, **kwargs: captured.update(kwargs) or iter([]),
        )
        list(stream.send_message([{"role": "user", "content": "hi"}], tools=None))
        assert "tools" not in captured

    def test_tools_translated_and_attached_when_given(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
        stream = AnthropicStream()
        captured = {}
        monkeypatch.setattr(
            "JFI.llm.anthropic_stream._stream_events",
            lambda client, **kwargs: captured.update(kwargs) or iter([]),
        )
        openai_tools = [{
            "type": "function",
            "function": {"name": "x", "description": "d", "parameters": {"type": "object", "properties": {"a": {"type": "string"}}}},
        }]
        list(stream.send_message([{"role": "user", "content": "hi"}], tools=openai_tools))
        assert captured["tools"] == [
            {"name": "x", "description": "d", "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}}},
        ]
