from typing import Callable, Optional

# ask_llm: a general-purpose escape hatch for one-off text work (a
# description, a clarification, a rephrase, brainstorming a name, ...) that
# doesn't warrant its own dedicated Python tool. Rather than writing bespoke
# code for every conceivable small text operation a phase might want, this
# lets the model delegate it to a fresh, stateless LLM call instead.


def ask_llm(prompt: str, llm, console=None) -> str:
    """
    Sends `prompt` as a single, stateless user turn — no tools, no
    conversation history, no plan/file context — and returns the reply as
    plain text.

    Mirrors BaseLLMStream.check_user_approval's own silent-stream-
    consumption pattern for the actual LLM call: it's a side call, not a
    turn in the phase's own conversation, so it never touches the AI-space
    transcript or session history. `console`, when given, only gets a
    record_token_usage(...) call afterward — real usage if the server
    reports it in the stream, else the same char/4 estimate
    print_agent_response falls back to — so this call's cost still shows
    up in the header's cumulative ↓/↑ totals instead of silently not
    counting against them.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return "Error: ask_llm needs a non-empty prompt."

    text = ""
    prompt_tokens = 0
    completion_tokens = 0
    usage_seen = False
    try:
        stream = llm.send_message([{"role": "user", "content": prompt}])
        for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                usage_seen = True
                prompt_tokens += usage.prompt_tokens or 0
                completion_tokens += usage.completion_tokens or 0

            if not chunk.choices:
                continue  # a usage-only trailer chunk, if the server sends one
            delta = chunk.choices[0].delta
            if delta.content:
                text += delta.content
    except Exception as e:
        return f"Error calling ask_llm: {e}"

    if not usage_seen:
        # Most OpenAI-compatible servers omit real usage in streaming mode
        # unless stream_options.include_usage was requested (not universally
        # supported) — same char/4 fallback print_agent_response uses.
        prompt_tokens = (len(prompt) + 8) // 4
        completion_tokens = (len(text) + 8) // 4
    if console is not None:
        console.record_token_usage(prompt_tokens, completion_tokens)

    text = text.strip()
    return text if text else "Error: ask_llm got an empty response — try rephrasing the prompt."


def make_ask_llm(llm, console: Optional[object] = None) -> Callable[[str], str]:
    """Binds ask_llm to one phase's own LLM stream (see runner.PHASE_ENV_PREFIX)
    and console — rebound in runner.run_phase every phase, the same way
    execute_command/context_save are rebound per session in _run_session,
    just per phase here since .env may point each phase at a different
    model."""
    def bound(prompt: str) -> str:
        return ask_llm(prompt, llm, console)
    return bound
