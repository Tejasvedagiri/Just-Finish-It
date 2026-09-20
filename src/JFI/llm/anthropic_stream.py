"""AnthropicStream -- talks to Claude's own Messages API directly.

Unlike Ollama/llama.cpp/vLLM/LM Studio (all genuinely OpenAI-compatible --
see openai_compatable_stream.py, which already covers them with zero extra
code), Anthropic's API is a DIFFERENT shape end to end: the system prompt is
its own top-level `system` param rather than a message; tool calls are
`tool_use` content blocks inside an assistant message instead of a
`tool_calls` array; tool results are `tool_result` blocks inside a
following USER message instead of separate `role: "tool"` messages; and
streaming is a sequence of typed events (`content_block_delta`,
`message_delta`, ...) rather than OpenAI's `choices[0].delta` chunks.

Rather than teach every caller (runner.py, pt_console_manager.py's
print_agent_response, log_llm_call, ...) a second shape, this module
translates BOTH directions at the edges: JFI's own OpenAI-shaped
`messages`/`tools` going out, and Anthropic's native streaming events
translated into the exact same duck-typed "OpenAI chunk" shape
(`chunk.choices[0].delta.content` / `.tool_calls` / `chunk.usage`) coming
back -- so print_agent_response's loop runs unmodified either way.

Selected via LLM_BACKEND=anthropic (or "claude") -- see backend_select.py.
Needs the `anthropic` extra (`uv sync --extra anthropic`) and
ANTHROPIC_API_KEY in .env (optionally per-phase, like OPENAI_API_KEY).
"""

import json
import types

from JFI.llm.base_llm_stream import BaseLLMStream, phase_env

DEFAULT_MAX_TOKENS = 8192
DEFAULT_REQUEST_TIMEOUT = 120.0


def _split_system(messages: list) -> tuple[str, list]:
    """Anthropic wants the system prompt as a top-level `system` param,
    never inside `messages` -- pulls out any leading system-role message(s)
    (JFI only ever sends one, but concatenate defensively) and returns
    (system_text, remaining_messages)."""
    system_parts = []
    rest = []
    for m in messages:
        if m.get("role") == "system" and not rest:
            system_parts.append(str(m.get("content") or ""))
        else:
            rest.append(m)
    return "\n\n".join(system_parts), rest


def _translate_tools(tools):
    """OpenAI's {"type": "function", "function": {name, description,
    parameters}} -> Anthropic's flat {name, description, input_schema} --
    same JSON Schema either way, just a different wrapper/key name."""
    if not tools:
        return None
    out = []
    for t in tools:
        fn = t.get("function", t)
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _image_block_from_data_url(url: str):
    """``"data:image/png;base64,AAAA…"`` -> Anthropic's
    ``{"type": "image", "source": {"type": "base64", "media_type": …, "data": …}}``.
    Returns None for anything that isn't a data: URL (Anthropic's Messages
    API takes base64 image bytes directly, not a fetchable http(s) link)."""
    if not url.startswith("data:"):
        return None
    header, _, data = url.partition(",")
    media_type = header[len("data:"):].split(";")[0] or "image/png"
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def _translate_messages(messages: list) -> list:
    """JFI's OpenAI-shaped history (system already split out by
    _split_system) -> Anthropic's alternating user/assistant messages with
    content BLOCKS.

    Two shape differences that need real translation, not just renaming:
    - An assistant message's `tool_calls` become `tool_use` content blocks
      inside that SAME assistant message (arguments JSON-decoded into a
      real `input` dict -- Anthropic wants the parsed object, not a string).
    - Each `role: "tool"` message becomes a `tool_result` block, but
      Anthropic requires every tool_result for one assistant turn to land
      in a SINGLE following user message (multiple content blocks), not
      one user message per call the way JFI's own history stores them (one
      `{"role": "tool", ...}` entry per tool_call_id) -- consecutive tool
      messages are merged into one user turn here.
    """
    out = []
    tool_batch_open = False
    for m in messages:
        role = m.get("role")

        if role == "assistant":
            blocks = []
            content = m.get("content")
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc["id"], "name": fn.get("name", ""), "input": args})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            tool_batch_open = False

        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id"), "content": str(m.get("content") or "")}
            if tool_batch_open:
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
                tool_batch_open = True

        elif role == "user":
            content = m.get("content")
            if isinstance(content, list):
                blocks = []
                for part in content:
                    if part.get("type") == "text":
                        blocks.append({"type": "text", "text": part.get("text", "")})
                    elif part.get("type") == "image_url":
                        image = _image_block_from_data_url(part.get("image_url", {}).get("url", ""))
                        if image:
                            blocks.append(image)
                out.append({"role": "user", "content": blocks})
            else:
                out.append({"role": "user", "content": str(content or "")})
            tool_batch_open = False
        # role == "system" should never reach here -- _split_system already
        # pulled it out before this function runs.

    return out


def _fake_openai_chunk(content=None, reasoning_content=None, tool_call_delta=None, usage=None, usage_only=False):
    """One object shaped exactly like an OpenAI streaming chunk --
    ``.choices[0].delta.content`` / ``.reasoning_content`` / ``.tool_calls``,
    ``.usage`` -- so print_agent_response's existing loop (which only ever
    duck-types against this shape) needs no Anthropic-specific branch."""
    if usage_only:
        return types.SimpleNamespace(choices=[], usage=usage)
    delta = types.SimpleNamespace(
        content=content, reasoning_content=reasoning_content,
        tool_calls=[tool_call_delta] if tool_call_delta else None,
    )
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)], usage=usage)


def _stream_events(client, **kwargs):
    """Opens one Anthropic streaming request and yields _fake_openai_chunk
    objects translated from its native event sequence. The `with` block
    stays open for exactly as long as this generator is being iterated --
    print_agent_response's `for chunk in agent_response:` fully drains it
    in one synchronous pass, so this needs no separate lifecycle management."""
    prompt_tokens = 0
    with client.messages.stream(**kwargs) as stream:
        for event in stream:
            if event.type == "message_start":
                prompt_tokens = event.message.usage.input_tokens

            elif event.type == "content_block_start":
                block = event.content_block
                if block.type == "tool_use":
                    tc = types.SimpleNamespace(
                        index=event.index, id=block.id,
                        function=types.SimpleNamespace(name=block.name, arguments=""),
                    )
                    yield _fake_openai_chunk(tool_call_delta=tc)

            elif event.type == "content_block_delta":
                d = event.delta
                if d.type == "text_delta":
                    yield _fake_openai_chunk(content=d.text)
                elif d.type == "thinking_delta":
                    # Extended thinking (opt-in, not requested by default
                    # here) -- maps onto the same reasoning_content display
                    # path a reasoning-model OpenAI-compatible server uses.
                    yield _fake_openai_chunk(reasoning_content=d.thinking)
                elif d.type == "input_json_delta":
                    tc = types.SimpleNamespace(
                        index=event.index, id=None,
                        function=types.SimpleNamespace(name=None, arguments=d.partial_json),
                    )
                    yield _fake_openai_chunk(tool_call_delta=tc)

            elif event.type == "message_delta":
                usage = types.SimpleNamespace(
                    prompt_tokens=prompt_tokens, completion_tokens=event.usage.output_tokens,
                )
                yield _fake_openai_chunk(usage=usage, usage_only=True)


class AnthropicStream(BaseLLMStream):
    def __init__(self, prefix: str = ""):
        super().__init__(prefix)
        import anthropic  # deferred: only imported when this backend is actually selected

        api_key = phase_env(prefix, "ANTHROPIC_API_KEY")
        if not api_key:
            hint = f" (or {prefix}_ANTHROPIC_API_KEY, for the {prefix} phase)" if prefix else ""
            raise KeyError(f"Missing required .env setting: ANTHROPIC_API_KEY{hint}")
        try:
            timeout = float(phase_env(prefix, "LLM_REQUEST_TIMEOUT", str(DEFAULT_REQUEST_TIMEOUT)))
        except ValueError:
            timeout = DEFAULT_REQUEST_TIMEOUT
        try:
            self.max_tokens = int(phase_env(prefix, "ANTHROPIC_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
        except ValueError:
            self.max_tokens = DEFAULT_MAX_TOKENS

        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        self.stream_service = self.client  # matches OpenAICompatableStream's own attribute name

    def close(self):
        self.client.close()

    def send_message(self, message, tools=None):
        system_text, rest = _split_system(message)
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": _translate_messages(rest),
            "temperature": float(self.temperature),
        }
        if system_text:
            kwargs["system"] = system_text
        anthropic_tools = _translate_tools(tools)
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
        return _stream_events(self.client, **kwargs)
