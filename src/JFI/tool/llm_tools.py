from typing import Callable

# ask_llm: a general-purpose escape hatch for one-off text work (a
# description, a clarification, a rephrase, brainstorming a name, ...) that
# doesn't warrant its own dedicated Python tool. Rather than writing bespoke
# code for every conceivable small text operation a phase might want, this
# lets the model delegate it to a fresh, stateless LLM call instead.


def ask_llm(prompt: str, llm) -> str:
    """
    Sends `prompt` as a single, stateless user turn — no tools, no
    conversation history, no plan/file context — and returns the reply as
    plain text.

    Mirrors BaseLLMStream.check_user_approval's own silent-stream-
    consumption pattern: this is a side call, not a turn in the phase's own
    conversation, so it must never touch the console or the session history.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return "Error: ask_llm needs a non-empty prompt."

    try:
        stream = llm.send_message([{"role": "user", "content": prompt}])
        text = ""
        for chunk in stream:
            if not chunk.choices:
                continue  # a usage-only trailer chunk, if the server sends one
            delta = chunk.choices[0].delta
            if delta.content:
                text += delta.content
    except Exception as e:
        return f"Error calling ask_llm: {e}"

    text = text.strip()
    return text if text else "Error: ask_llm got an empty response — try rephrasing the prompt."


def make_ask_llm(llm) -> Callable[[str], str]:
    """Binds ask_llm to one phase's own LLM stream (see runner.PHASE_ENV_PREFIX)
    — rebound in runner.run_phase every phase, the same way execute_command/
    context_save are rebound per session in _run_session, just per phase
    here since .env may point each phase at a different model."""
    def bound(prompt: str) -> str:
        return ask_llm(prompt, llm)
    return bound
