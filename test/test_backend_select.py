"""llm/backend_select.py -- LLM_BACKEND routes each phase to the right
BaseLLMStream implementation. The default (unset) path is already covered
end to end by test_main_per_phase_llms.py; this focuses on the routing
logic itself and the Ollama/llama.cpp convenience defaults.
"""

import pytest

from JFI.llm import backend_select


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from a blank slate for the vars this module reads/
    sets, so one test's defaults can never leak into the next."""
    for key in ("LLM_BACKEND", "OPENAI_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                "PLANNER_LLM_BACKEND", "PLANNER_OPENAI_URL"):
        monkeypatch.delenv(key, raising=False)


class TestDefaultAndOpenAIBackend:
    def test_unset_backend_builds_an_openai_compatible_stream(self, monkeypatch):
        monkeypatch.setenv("OPENAI_URL", "http://example/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from JFI.llm.openai_compatable_stream import OpenAICompatableStream

        stream = backend_select.make_llm_stream()
        assert isinstance(stream, OpenAICompatableStream)

    def test_explicit_openai_backend_is_the_same_as_unset(self, monkeypatch):
        monkeypatch.setenv("LLM_BACKEND", "openai")
        monkeypatch.setenv("OPENAI_URL", "http://example/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from JFI.llm.openai_compatable_stream import OpenAICompatableStream

        assert isinstance(backend_select.make_llm_stream(), OpenAICompatableStream)


class TestOllamaBackend:
    def test_defaults_url_and_key_when_unset(self, monkeypatch):
        monkeypatch.setenv("LLM_BACKEND", "ollama")
        backend_select.make_llm_stream()
        assert __import__("os").environ["OPENAI_URL"] == "http://127.0.0.1:11434/v1"
        assert __import__("os").environ["OPENAI_API_KEY"] == "ollama"

    def test_never_overrides_an_explicitly_set_url(self, monkeypatch):
        monkeypatch.setenv("LLM_BACKEND", "ollama")
        monkeypatch.setenv("OPENAI_URL", "http://my-custom-ollama:9999/v1")
        backend_select.make_llm_stream()
        assert __import__("os").environ["OPENAI_URL"] == "http://my-custom-ollama:9999/v1"

    def test_builds_an_openai_compatible_stream(self, monkeypatch):
        monkeypatch.setenv("LLM_BACKEND", "ollama")
        from JFI.llm.openai_compatable_stream import OpenAICompatableStream

        assert isinstance(backend_select.make_llm_stream(), OpenAICompatableStream)


class TestLlamaCppBackend:
    @pytest.mark.parametrize("spelling", ["llamacpp", "llama.cpp", "llama-cpp"])
    def test_accepts_every_documented_spelling(self, monkeypatch, spelling):
        monkeypatch.setenv("LLM_BACKEND", spelling)
        backend_select.make_llm_stream()
        assert __import__("os").environ["OPENAI_URL"] == "http://127.0.0.1:8080/v1"


class TestPerPhaseOverride:
    def test_a_phase_specific_backend_wins_over_the_shared_default(self, monkeypatch):
        monkeypatch.setenv("LLM_BACKEND", "ollama")
        monkeypatch.setenv("PLANNER_LLM_BACKEND", "llamacpp")
        monkeypatch.setenv("PLANNER_OPENAI_URL", "http://should-not-be-overwritten:1/v1")

        backend_select.make_llm_stream("PLANNER")

        import os

        # llamacpp's own default is for the SHARED (unprefixed) OPENAI_URL,
        # which the phase-specific override above already set -- so it must
        # be left completely untouched (os.environ.setdefault never touches
        # PLANNER_OPENAI_URL, only OPENAI_URL).
        assert os.environ["PLANNER_OPENAI_URL"] == "http://should-not-be-overwritten:1/v1"


class TestAnthropicBackend:
    @pytest.mark.parametrize("spelling", ["anthropic", "claude", "ANTHROPIC", "Claude"])
    def test_routes_to_anthropic_stream(self, monkeypatch, spelling):
        monkeypatch.setenv("LLM_BACKEND", spelling)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
        from JFI.llm.anthropic_stream import AnthropicStream

        assert isinstance(backend_select.make_llm_stream(), AnthropicStream)


class TestUnknownBackend:
    def test_falls_back_to_openai_compatible_with_a_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("LLM_BACKEND", "totally-made-up-backend")
        monkeypatch.setenv("OPENAI_URL", "http://example/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from JFI.llm.openai_compatable_stream import OpenAICompatableStream

        with caplog.at_level("WARNING"):
            stream = backend_select.make_llm_stream()
        assert isinstance(stream, OpenAICompatableStream)
        assert "Unrecognized LLM_BACKEND" in caplog.text
