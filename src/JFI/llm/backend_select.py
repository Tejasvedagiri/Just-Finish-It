"""Picks which BaseLLMStream implementation a phase actually talks to,
based on LLM_BACKEND (optionally per-phase, like MODEL/OPENAI_URL/...) --
one place to add a new backend rather than teaching runner.py about each.

- unset / "openai" (default): any OpenAI-compatible /v1/chat/completions
  endpoint via OpenAICompatableStream -- OpenAI itself, LM Studio, vLLM,
  and both of the below are ALREADY this exact shape, so nothing further
  is needed for them beyond pointing OPENAI_URL at the right port.
- "ollama": OpenAICompatableStream, with OPENAI_URL/OPENAI_API_KEY defaulted
  to Ollama's own local server (http://127.0.0.1:11434/v1, key "ollama" --
  Ollama itself doesn't check it) whenever those aren't already set in
  .env -- a named, discoverable shortcut for what typing the URL by hand
  already let you do.
- "llamacpp" (or "llama.cpp"/"llama-cpp"): same idea, defaulted to
  llama.cpp server's usual port (http://127.0.0.1:8080/v1, key
  "llamacpp" -- its server doesn't check the key either, any non-empty
  string works).
- "lmstudio" (or "lm-studio"/"lm studio"): same idea, defaulted to LM
  Studio's usual local server port (http://127.0.0.1:1234/v1, key
  "lm-studio" -- LM Studio's own convention, not checked either).
- "anthropic" (or "claude"): AnthropicStream -- Claude's own Messages API,
  which is NOT OpenAI-compatible (different request/response/streaming
  shape, tool_use/tool_result content blocks instead of tool_calls) -- see
  anthropic_stream.py. Needs the `anthropic` extra installed
  (`uv sync --extra anthropic`) and ANTHROPIC_API_KEY set.

An unrecognized value falls back to the plain OpenAI-compatible path with a
logged warning, rather than crashing a whole session over a typo.
"""

import logging
import os

from JFI.llm.base_llm_stream import phase_env

logger = logging.getLogger(__name__)

_OLLAMA_DEFAULTS = {"OPENAI_URL": "http://127.0.0.1:11434/v1", "OPENAI_API_KEY": "ollama"}
_LLAMACPP_DEFAULTS = {"OPENAI_URL": "http://127.0.0.1:8080/v1", "OPENAI_API_KEY": "llamacpp"}
_LMSTUDIO_DEFAULTS = {"OPENAI_URL": "http://127.0.0.1:1234/v1", "OPENAI_API_KEY": "lm-studio"}
_KNOWN_BACKENDS = {
    "", "openai", "ollama", "llamacpp", "llama.cpp", "llama-cpp", "lmstudio", "lm-studio", "lm studio",
    "anthropic", "claude",
}


def make_llm_stream(prefix: str = ""):
    """Constructs the right BaseLLMStream for `prefix`'s own LLM_BACKEND
    (falling back to the shared, unprefixed setting exactly like MODEL/
    OPENAI_URL/... already do -- see phase_env)."""
    backend = phase_env(prefix, "LLM_BACKEND", "").strip().lower()

    if backend in ("anthropic", "claude"):
        from JFI.llm.anthropic_stream import AnthropicStream

        return AnthropicStream(prefix)

    if backend == "ollama":
        for key, value in _OLLAMA_DEFAULTS.items():
            os.environ.setdefault(key, value)
    elif backend in ("llamacpp", "llama.cpp", "llama-cpp"):
        for key, value in _LLAMACPP_DEFAULTS.items():
            os.environ.setdefault(key, value)
    elif backend in ("lmstudio", "lm-studio", "lm studio"):
        for key, value in _LMSTUDIO_DEFAULTS.items():
            os.environ.setdefault(key, value)
    elif backend not in _KNOWN_BACKENDS:
        logger.warning(
            "Unrecognized LLM_BACKEND=%r; falling back to a plain OpenAI-compatible endpoint.", backend
        )

    from JFI.llm.openai_compatable_stream import OpenAICompatableStream

    return OpenAICompatableStream(prefix)
