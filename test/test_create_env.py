"""`uv run create-env` (src/JFI/create_env.py) -- a standalone, stdlib-only
system check + .env setup helper. Each check is a pure/small function so it
can be tested in isolation without actually needing a real LLM server or
touching this repo's own real .env.
"""

import urllib.error
from unittest import mock

import pytest

from JFI import create_env


class TestCheckPythonVersion:
    def test_passes_on_the_actual_running_interpreter(self, capsys):
        """This test suite itself only runs on >=3.12 (see pyproject.toml's
        requires-python), so the real interpreter must always pass here."""
        assert create_env.check_python_version() is True
        assert "✅" in capsys.readouterr().out

    def test_fails_when_below_the_minimum(self, monkeypatch, capsys):
        monkeypatch.setattr(create_env, "MIN_PYTHON", (99, 0))
        assert create_env.check_python_version() is False
        assert "❌" in capsys.readouterr().out


class TestCheckUvAvailable:
    def test_reports_found_when_on_path(self, monkeypatch, capsys):
        monkeypatch.setattr(create_env.shutil, "which", lambda name: "/usr/bin/uv")
        assert create_env.check_uv_available() is True
        assert "✅" in capsys.readouterr().out

    def test_reports_missing_without_crashing(self, monkeypatch, capsys):
        monkeypatch.setattr(create_env.shutil, "which", lambda name: None)
        assert create_env.check_uv_available() is False
        assert "optional" in capsys.readouterr().out


class TestParseEnvFile:
    def test_reads_simple_key_value_pairs(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("OPENAI_URL=http://127.0.0.1:1234/v1\nMODEL=qwen3:8b\n", encoding="utf-8")
        values = create_env._parse_env_file(env_file)
        assert values["OPENAI_URL"] == "http://127.0.0.1:1234/v1"
        assert values["MODEL"] == "qwen3:8b"

    def test_ignores_comments_and_blank_lines(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# a comment\n\nMODEL=x\n", encoding="utf-8")
        assert create_env._parse_env_file(env_file) == {"MODEL": "x"}

    def test_strips_surrounding_quotes(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text('MODEL="qwen3:8b"\n', encoding="utf-8")
        assert create_env._parse_env_file(env_file) == {"MODEL": "qwen3:8b"}

    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert create_env._parse_env_file(tmp_path / "nope.env") == {}


class TestEnsureEnvFile:
    def test_creates_env_from_template_when_missing(self, tmp_path, monkeypatch, capsys):
        env_path = tmp_path / ".env"
        template_path = tmp_path / "JFI_ENV_TEMPLATE"
        template_path.write_text("OPENAI_URL=http://127.0.0.1:11434/v1\nMODEL=qwen3:8b\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "ENV_TEMPLATE_PATH", template_path)

        values = create_env.ensure_env_file()

        assert env_path.exists()
        assert env_path.read_text(encoding="utf-8") == template_path.read_text(encoding="utf-8")
        assert values["MODEL"] == "qwen3:8b"
        assert "Created .env" in capsys.readouterr().out

    def test_leaves_an_existing_env_untouched(self, tmp_path, monkeypatch, capsys):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=already-configured\n", encoding="utf-8")
        template_path = tmp_path / "JFI_ENV_TEMPLATE"
        template_path.write_text("MODEL=template-default\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "ENV_TEMPLATE_PATH", template_path)

        values = create_env.ensure_env_file()

        assert values["MODEL"] == "already-configured"
        assert "already exists" in capsys.readouterr().out

    def test_reports_clearly_when_both_env_and_template_are_missing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(create_env, "ENV_PATH", tmp_path / ".env")
        monkeypatch.setattr(create_env, "ENV_TEMPLATE_PATH", tmp_path / "JFI_ENV_TEMPLATE")

        values = create_env.ensure_env_file()

        assert values == {}
        assert "❌" in capsys.readouterr().out


class TestCheckLlmReachable:
    def test_reports_reachable_on_a_real_2xx_response(self, monkeypatch, capsys):
        class _FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(create_env.urllib.request, "urlopen", lambda *a, **k: _FakeResponse())
        assert create_env.check_llm_reachable("http://127.0.0.1:1234/v1") is True
        assert "✅" in capsys.readouterr().out

    def test_a_4xx_still_counts_as_something_is_listening(self, monkeypatch, capsys):
        def _raise(*a, **k):
            raise urllib.error.HTTPError("http://x/models", 404, "not found", {}, None)

        monkeypatch.setattr(create_env.urllib.request, "urlopen", _raise)
        assert create_env.check_llm_reachable("http://127.0.0.1:1234/v1") is True

    def test_a_5xx_counts_as_unreachable(self, monkeypatch):
        def _raise(*a, **k):
            raise urllib.error.HTTPError("http://x/models", 500, "server error", {}, None)

        monkeypatch.setattr(create_env.urllib.request, "urlopen", _raise)
        assert create_env.check_llm_reachable("http://127.0.0.1:1234/v1") is False

    def test_connection_refused_reports_unreachable_not_a_crash(self, monkeypatch, capsys):
        def _raise(*a, **k):
            raise ConnectionRefusedError("nobody home")

        monkeypatch.setattr(create_env.urllib.request, "urlopen", _raise)
        assert create_env.check_llm_reachable("http://127.0.0.1:9/v1") is False
        assert "not reachable" in capsys.readouterr().out


class TestCheckAnthropicReachable:
    def test_reports_reachable_on_a_real_2xx_response(self, monkeypatch, capsys):
        class _FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(create_env.urllib.request, "urlopen", lambda *a, **k: _FakeResponse())
        assert create_env.check_anthropic_reachable("sk-ant-fake") is True
        assert "✅" in capsys.readouterr().out

    def test_a_401_is_reported_as_a_bad_key_not_a_generic_4xx(self, monkeypatch, capsys):
        def _raise(*a, **k):
            raise urllib.error.HTTPError("https://api.anthropic.com/v1/models", 401, "unauthorized", {}, None)

        monkeypatch.setattr(create_env.urllib.request, "urlopen", _raise)
        assert create_env.check_anthropic_reachable("sk-ant-bad") is False
        assert "rejected ANTHROPIC_API_KEY" in capsys.readouterr().out

    def test_a_404_still_counts_as_something_is_listening(self, monkeypatch):
        def _raise(*a, **k):
            raise urllib.error.HTTPError("https://api.anthropic.com/v1/models", 404, "not found", {}, None)

        monkeypatch.setattr(create_env.urllib.request, "urlopen", _raise)
        assert create_env.check_anthropic_reachable("sk-ant-fake") is True

    def test_connection_refused_reports_unreachable_not_a_crash(self, monkeypatch, capsys):
        def _raise(*a, **k):
            raise ConnectionRefusedError("nobody home")

        monkeypatch.setattr(create_env.urllib.request, "urlopen", _raise)
        assert create_env.check_anthropic_reachable("sk-ant-fake") is False
        assert "not reachable" in capsys.readouterr().out


class TestCheckAnthropicExtraInstalled:
    def test_reports_installed_when_import_succeeds(self, monkeypatch, capsys):
        import sys
        import types

        monkeypatch.setitem(sys.modules, "anthropic", types.ModuleType("anthropic"))
        assert create_env.check_anthropic_extra_installed() is True
        assert "✅" in capsys.readouterr().out

    def test_reports_missing_without_crashing(self, monkeypatch, capsys):
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *a, **k):
            if name == "anthropic":
                raise ImportError("no module named anthropic")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        assert create_env.check_anthropic_extra_installed() is False
        assert "uv sync --extra anthropic" in capsys.readouterr().out


class TestCheckAnthropicBackend:
    """Observed bug: create-env only ever checked OPENAI_URL, so a session
    correctly configured with LLM_BACKEND=anthropic + ANTHROPIC_API_KEY (no
    OPENAI_URL at all -- see AnthropicStream, which never reads it) was
    always reported "Not fully ready" even though JFI itself would run fine."""

    def test_ready_when_key_and_model_and_extra_and_reachability_all_pass(self, monkeypatch, capsys):
        monkeypatch.setattr(create_env, "check_anthropic_extra_installed", lambda: True)
        monkeypatch.setattr(create_env, "check_anthropic_reachable", lambda key, **k: True)
        values = {"LLM_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-fake", "MODEL": "claude-sonnet-5"}

        ok = create_env._check_anthropic_backend(values, "anthropic", "claude-sonnet-5")

        assert ok is True
        assert "OPENAI_URL" not in capsys.readouterr().out

    def test_not_ready_without_touching_openai_url_at_all(self, monkeypatch):
        monkeypatch.setattr(create_env, "check_anthropic_extra_installed", lambda: True)
        values = {"LLM_BACKEND": "anthropic", "MODEL": "claude-sonnet-5"}

        ok = create_env._check_anthropic_backend(values, "anthropic", "claude-sonnet-5")

        assert ok is False

    def test_not_ready_when_extra_missing_even_with_a_good_key(self, monkeypatch):
        monkeypatch.setattr(create_env, "check_anthropic_extra_installed", lambda: False)
        monkeypatch.setattr(create_env, "check_anthropic_reachable", lambda key, **k: True)
        values = {"LLM_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-fake", "MODEL": "claude-sonnet-5"}

        ok = create_env._check_anthropic_backend(values, "anthropic", "claude-sonnet-5")

        assert ok is False


class TestCheckOpenAICompatibleBackend:
    def test_ready_with_explicit_url_key_and_model(self, monkeypatch):
        monkeypatch.setattr(create_env, "check_llm_reachable", lambda url, **k: True)
        values = {"OPENAI_URL": "http://example/v1", "OPENAI_API_KEY": "k", "MODEL": "m"}

        assert create_env._check_openai_compatible_backend(values, "") is True

    def test_ollama_backend_is_ready_from_its_own_defaults_alone(self, monkeypatch, capsys):
        """Observed bug: LLM_BACKEND=ollama with no OPENAI_URL set is a valid,
        runnable config (backend_select fills in the ollama default at
        runtime) but was reported "OPENAI_URL not set -- skipping" as if
        nothing was configured at all."""
        monkeypatch.setattr(create_env, "check_llm_reachable", lambda url, **k: True)
        values = {"LLM_BACKEND": "ollama", "MODEL": "qwen3:8b"}

        ok = create_env._check_openai_compatible_backend(values, "ollama")

        out = capsys.readouterr().out
        assert ok is True
        assert "http://127.0.0.1:11434/v1" in out

    def test_not_ready_when_openai_url_missing_and_backend_has_no_defaults(self, capsys):
        values = {"MODEL": "m"}

        ok = create_env._check_openai_compatible_backend(values, "")

        assert ok is False
        assert "skipping the reachability check" in capsys.readouterr().out

    def test_warns_on_unrecognized_backend_but_still_checks_openai_url(self, monkeypatch, capsys):
        monkeypatch.setattr(create_env, "check_llm_reachable", lambda url, **k: True)
        values = {"LLM_BACKEND": "totally-made-up", "OPENAI_URL": "http://example/v1", "OPENAI_API_KEY": "k", "MODEL": "m"}

        ok = create_env._check_openai_compatible_backend(values, "totally-made-up")

        assert ok is True
        assert "Unrecognized LLM_BACKEND" in capsys.readouterr().out

    def test_lmstudio_backend_is_ready_from_its_own_defaults_alone(self, monkeypatch, capsys):
        monkeypatch.setattr(create_env, "check_llm_reachable", lambda url, **k: True)
        values = {"LLM_BACKEND": "lmstudio", "MODEL": "qwen3:8b"}

        ok = create_env._check_openai_compatible_backend(values, "lmstudio")

        out = capsys.readouterr().out
        assert ok is True
        assert "http://127.0.0.1:1234/v1" in out


class TestFetchModelIds:
    """The setup wizard's model list -- both an OpenAI-compatible /models
    endpoint and Anthropic's own /v1/models share this exact {"data":
    [{"id": ...}]} shape, just with different auth headers."""

    def test_parses_data_ids_from_a_2xx_response(self, monkeypatch):
        class _FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                import json
                return json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode("utf-8")

        monkeypatch.setattr(create_env.urllib.request, "urlopen", lambda *a, **k: _FakeResponse())
        assert create_env._fetch_model_ids("http://x/models", {}) == ["model-a", "model-b"]

    def test_returns_none_on_any_failure(self, monkeypatch):
        def _raise(*a, **k):
            raise ConnectionRefusedError("nobody home")

        monkeypatch.setattr(create_env.urllib.request, "urlopen", _raise)
        assert create_env._fetch_model_ids("http://x/models", {}) is None

    def test_returns_none_on_unexpected_shape(self, monkeypatch):
        class _FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return b'{"unexpected": true}'

        monkeypatch.setattr(create_env.urllib.request, "urlopen", lambda *a, **k: _FakeResponse())
        assert create_env._fetch_model_ids("http://x/models", {}) is None

    def test_openai_compatible_sends_bearer_auth(self, monkeypatch):
        captured = {}

        def _fake_request(url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return mock.Mock()

        class _FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return b'{"data": [{"id": "m"}]}'

        monkeypatch.setattr(create_env.urllib.request, "Request", _fake_request)
        monkeypatch.setattr(create_env.urllib.request, "urlopen", lambda *a, **k: _FakeResponse())

        create_env.fetch_openai_compatible_models("http://example/v1", "sk-abc")

        assert captured["url"] == "http://example/v1/models"
        assert captured["headers"] == {"Authorization": "Bearer sk-abc"}

    def test_anthropic_sends_x_api_key_header(self, monkeypatch):
        captured = {}

        def _fake_request(url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return mock.Mock()

        class _FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return b'{"data": [{"id": "claude-sonnet-5"}]}'

        monkeypatch.setattr(create_env.urllib.request, "Request", _fake_request)
        monkeypatch.setattr(create_env.urllib.request, "urlopen", lambda *a, **k: _FakeResponse())

        create_env.fetch_anthropic_models("sk-ant-fake")

        assert captured["url"] == "https://api.anthropic.com/v1/models"
        assert captured["headers"] == {"x-api-key": "sk-ant-fake", "anthropic-version": "2023-06-01"}


class TestGuessContextSize:
    def test_falls_back_to_default_when_model_is_blank(self):
        assert create_env.guess_context_size("ollama", "") == 32768

    def test_hosted_claude_model_gets_a_large_default(self):
        assert create_env.guess_context_size("anthropic", "claude-sonnet-5") == 200000

    def test_hosted_gpt_4o_gets_its_known_window(self):
        assert create_env.guess_context_size("openai", "gpt-4o") == 128000

    def test_unrecognized_hosted_model_falls_back_to_default(self):
        assert create_env.guess_context_size("openai", "some-brand-new-model") == 32768

    @pytest.mark.parametrize("model,expected", [
        ("qwen3:8b", 8192),
        ("gemma-4:31b", 32768),
        ("llama-3.1-70b-instruct", 65536),
        ("qwen3.5:35b-a3b", 32768),
    ])
    def test_local_models_are_bucketed_by_parameter_count(self, model, expected):
        assert create_env.guess_context_size("ollama", model) == expected

    def test_local_model_with_no_parseable_size_falls_back_to_default(self):
        assert create_env.guess_context_size("ollama", "mystery-model") == 32768

    def test_moe_tag_does_not_false_match_the_active_param_count(self):
        """Observed risk: "35b-a3b" (35B total, 3B active) must bucket on
        the 35, not accidentally match the "3b" hiding inside "a3b"."""
        assert create_env.guess_context_size("ollama", "qwen3.5:35b-a3b") == 32768


class TestWriteEnvValues:
    def test_replaces_an_existing_uncommented_line_in_place(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("OPENAI_URL=http://old/v1\nMODEL=old-model\n", encoding="utf-8")

        create_env._write_env_values(path, {"MODEL": "new-model"})

        text = path.read_text(encoding="utf-8")
        assert "MODEL=new-model" in text
        assert "OPENAI_URL=http://old/v1" in text
        assert "old-model" not in text

    def test_uncomments_a_commented_template_line(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("# ANTHROPIC_API_KEY=sk-ant-your-real-key\nMODEL=m\n", encoding="utf-8")

        create_env._write_env_values(path, {"ANTHROPIC_API_KEY": "sk-ant-real"})

        text = path.read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY=sk-ant-real" in text
        assert "# ANTHROPIC_API_KEY" not in text

    def test_appends_a_key_not_present_in_the_file_at_all(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("MODEL=m\n", encoding="utf-8")

        create_env._write_env_values(path, {"CONTEXT_SIZE": "16384"})

        assert "CONTEXT_SIZE=16384" in path.read_text(encoding="utf-8")

    def test_preserves_unrelated_lines_and_comments(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("# a helpful comment\nMODEL=m\n\n# another\n", encoding="utf-8")

        create_env._write_env_values(path, {"MODEL": "m2"})

        text = path.read_text(encoding="utf-8")
        assert "# a helpful comment" in text
        assert "# another" in text
        assert "MODEL=m2" in text


class TestIsConfigured:
    def test_anthropic_needs_key_and_model(self):
        assert create_env._is_configured({"ANTHROPIC_API_KEY": "k", "MODEL": "m"}, "anthropic") is True
        assert create_env._is_configured({"MODEL": "m"}, "anthropic") is False

    def test_local_backends_only_need_a_model_since_url_and_key_default(self):
        assert create_env._is_configured({"MODEL": "m"}, "ollama") is True
        assert create_env._is_configured({"MODEL": "m"}, "lmstudio") is True
        assert create_env._is_configured({}, "ollama") is False

    def test_plain_openai_compatible_needs_url_key_and_model(self):
        values = {"OPENAI_URL": "http://x/v1", "OPENAI_API_KEY": "k", "MODEL": "m"}
        assert create_env._is_configured(values, "") is True
        assert create_env._is_configured({"MODEL": "m"}, "") is False


# After the shared backend/key/model/CONTEXT_SIZE sub-flow, run_setup_wizard
# asks, in order: SESSION_MANAGER, the 6 _TUNING_KNOBS, THEME, whether to
# enable JFI_WEB_BRIDGE, whether to set MASTER_WS_URL (fleet dashboard), and
# whether to configure any per-phase override -- an empty answer to each of
# these 11 prompts accepts its default/says "no". Verified against the real
# prompt sequence (see _TAIL's index comments) rather than hand-counted,
# since this flow is long enough to miscount by hand.
_TAIL = [""] * 11  # [session_manager, TEMP, FREQ, COMPRESSION, STREAM, REASONING, TIMEOUT, THEME, BRIDGE, FLEET, PER_PHASE]


class TestRunSetupWizard:
    """Scripts a full interactive run by monkeypatching input()/getpass() in
    sequence, then checks what actually landed in .env -- this is the
    feature the bug report asked for: create-env should ask for the
    backend/key, show live models to pick from, default CONTEXT_SIZE from
    the model's own size, ask every other documented .env tuning knob
    (not just silently apply the code's own built-in defaults), and offer
    a completely separate backend/model per phase -- not just report
    pass/fail on whatever was already (or wasn't) hand-written into the
    file."""

    def test_anthropic_flow_writes_backend_key_model_and_guessed_context_size(self, tmp_path, monkeypatch, capsys):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_anthropic_models", lambda key: ["claude-sonnet-5", "claude-haiku-4-5"])

        # backend menu: anthropic; model menu: pick #1; CONTEXT_SIZE: accept guess; rest default/no
        answers = iter(["5", "1", "", *_TAIL])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: "sk-ant-fake")

        values = create_env.run_setup_wizard({})

        assert values["LLM_BACKEND"] == "anthropic"
        assert values["ANTHROPIC_API_KEY"] == "sk-ant-fake"
        assert values["MODEL"] == "claude-sonnet-5"
        assert values["CONTEXT_SIZE"] == "200000"
        assert "SESSION_MANAGER" not in values  # adaptive is the default, so left unwritten
        assert "JFI_WEB_BRIDGE" not in values  # declined, so left unwritten
        assert "Saved to" in capsys.readouterr().out

    def test_ollama_flow_defaults_url_and_key_and_lets_model_list_drive_context_size(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_openai_compatible_models", lambda url, key: ["gemma-4:31b"])

        # backend menu: ollama(1); OPENAI_URL: accept default; OPENAI_API_KEY: accept default;
        # model menu: pick #1; CONTEXT_SIZE: accept guess; rest default/no
        answers = iter(["1", "", "", "1", "", *_TAIL])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

        values = create_env.run_setup_wizard({})

        assert values["LLM_BACKEND"] == "ollama"
        assert values["OPENAI_URL"] == "http://127.0.0.1:11434/v1"
        assert values["OPENAI_API_KEY"] == "ollama"
        assert values["MODEL"] == "gemma-4:31b"
        assert values["CONTEXT_SIZE"] == "32768"

    def test_switching_backend_does_not_leak_the_old_backends_url_as_the_default(self, tmp_path, monkeypatch, capsys):
        """Observed bug: .env already had LLM_BACKEND=ollama (URL
        127.0.0.1:11434). Picking LM Studio in the wizard's backend menu
        still showed 127.0.0.1:11434 as the OPENAI_URL default instead of
        LM Studio's own 127.0.0.1:1234 -- the leftover value from the
        PREVIOUS backend was silently winning over the newly picked
        backend's own default."""
        env_path = tmp_path / ".env"
        env_path.write_text("LLM_BACKEND=ollama\nOPENAI_URL=http://127.0.0.1:11434/v1\nOPENAI_API_KEY=ollama\nMODEL=qwen3:8b\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_openai_compatible_models", lambda url, key: ["some-model"])

        # backend menu: lmstudio(3); OPENAI_URL: accept whatever default is shown;
        # OPENAI_API_KEY: accept default; model menu: pick #1; CONTEXT_SIZE: accept; rest default/no
        answers = iter(["3", "", "", "1", "", *_TAIL])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

        values = create_env.run_setup_wizard(create_env._parse_env_file(env_path))

        assert values["LLM_BACKEND"] == "lmstudio"
        assert values["OPENAI_URL"] == "http://127.0.0.1:1234/v1"
        assert values["OPENAI_API_KEY"] == "lm-studio"

    def test_reconfiguring_the_same_backend_still_prefills_its_prior_custom_url(self, tmp_path, monkeypatch):
        """The fix above must not break the opposite, legitimate case: rerunning
        --setup for the SAME backend should still offer the previously
        hand-set URL/key as the default, not silently reset to localhost."""
        env_path = tmp_path / ".env"
        env_path.write_text(
            "LLM_BACKEND=ollama\nOPENAI_URL=http://my-remote-ollama:9999/v1\nOPENAI_API_KEY=custom-key\nMODEL=old\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_openai_compatible_models", lambda url, key: ["some-model"])

        # backend menu: ollama(1); OPENAI_URL: accept shown default; OPENAI_API_KEY: accept shown default;
        # model menu: pick #1; CONTEXT_SIZE: accept; rest default/no
        answers = iter(["1", "", "", "1", "", *_TAIL])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

        values = create_env.run_setup_wizard(create_env._parse_env_file(env_path))

        assert values["OPENAI_URL"] == "http://my-remote-ollama:9999/v1"
        assert values["OPENAI_API_KEY"] == "custom-key"

    def test_custom_backend_asks_for_url_and_writes_no_llm_backend(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_openai_compatible_models", lambda url, key: None)

        # backend menu: custom(6); OPENAI_URL: typed; OPENAI_API_KEY (secret): handled by getpass;
        # model list fetch fails -> falls back to a plain input() for the model id; CONTEXT_SIZE:
        # accept; rest default/no
        answers = iter(["6", "http://my-vllm:8000/v1", "some-model-id", "", *_TAIL])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: "real-key")

        values = create_env.run_setup_wizard({})

        assert "LLM_BACKEND" not in values
        assert values["OPENAI_URL"] == "http://my-vllm:8000/v1"
        assert values["OPENAI_API_KEY"] == "real-key"
        assert values["MODEL"] == "some-model-id"

    def test_explicit_simple_session_manager_choice_is_persisted(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_anthropic_models", lambda key: None)

        tail = list(_TAIL)
        tail[0] = "2"  # SESSION_MANAGER -> simple
        # backend: anthropic(5); model: fetch fails -> typed; CONTEXT_SIZE: accept; rest as above
        answers = iter(["5", "claude-sonnet-5", "", *tail])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: "sk-ant-fake")

        values = create_env.run_setup_wizard({})

        assert values["SESSION_MANAGER"] == "simple"

    def test_tuning_knobs_default_to_the_documented_values_and_are_all_written(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_anthropic_models", lambda key: None)

        answers = iter(["5", "claude-sonnet-5", "", *_TAIL])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: "sk-ant-fake")

        values = create_env.run_setup_wizard({})

        assert values["TEMPERATURE"] == "0.7"
        assert values["FREQUENCY_PENALTY"] == "0.0"
        assert values["CONTEXT_COMPRESSION_RATIO"] == "0.7"
        assert values["STREAM_OUTPUT_CAP"] == "10000"
        assert values["REASONING_OUTPUT_CAP"] == "3000"
        assert values["LLM_REQUEST_TIMEOUT"] == "120"
        assert "THEME" not in values  # left blank -> auto-detect, so not written

    def test_tuning_knobs_can_be_overridden(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_anthropic_models", lambda key: None)

        tail = list(_TAIL)
        tail[1] = "0.3"          # TEMPERATURE
        tail[4] = "20000"        # STREAM_OUTPUT_CAP
        tail[7] = "dark-ocean"   # THEME
        answers = iter(["5", "claude-sonnet-5", "", *tail])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: "sk-ant-fake")

        values = create_env.run_setup_wizard({})

        assert values["TEMPERATURE"] == "0.3"
        assert values["STREAM_OUTPUT_CAP"] == "20000"
        assert values["THEME"] == "dark-ocean"

    def test_web_bridge_declined_writes_nothing(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_anthropic_models", lambda key: None)

        answers = iter(["5", "claude-sonnet-5", "", *_TAIL])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: "sk-ant-fake")

        values = create_env.run_setup_wizard({})

        assert "JFI_WEB_BRIDGE" not in values
        assert "JFI_WEB_PORT" not in values

    def test_web_bridge_accepted_also_asks_for_the_port(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_anthropic_models", lambda key: None)

        tail = list(_TAIL)
        tail[8] = "y"  # enable JFI_WEB_BRIDGE
        # JFI_WEB_PORT is only asked when the bridge is enabled, so it's inserted right after
        # index 8 (BRIDGE) and before index 9 (PER_PHASE) rather than living in _TAIL itself.
        answers = iter(["5", "claude-sonnet-5", "", *tail[:9], "9000", *tail[9:]])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: "sk-ant-fake")

        values = create_env.run_setup_wizard({})

        assert values["JFI_WEB_BRIDGE"] == "1"
        assert values["JFI_WEB_PORT"] == "9000"

    def test_fleet_dashboard_declined_writes_nothing(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_anthropic_models", lambda key: None)

        answers = iter(["5", "claude-sonnet-5", "", *_TAIL])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: "sk-ant-fake")

        values = create_env.run_setup_wizard({})

        assert "MASTER_WS_URL" not in values

    def test_fleet_dashboard_accepted_defaults_to_port_7776(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_anthropic_models", lambda key: None)

        tail = list(_TAIL)
        tail[9] = "y"  # report to a fleet dashboard: yes
        # MASTER_WS_URL is only asked when accepted, inserted right after index 9 (FLEET) and
        # before index 10 (PER_PHASE); "" here accepts the suggested default URL.
        answers = iter(["5", "claude-sonnet-5", "", *tail[:10], "", *tail[10:]])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: "sk-ant-fake")

        values = create_env.run_setup_wizard({})

        assert values["MASTER_WS_URL"] == "ws://127.0.0.1:7776/report"

    def test_fleet_dashboard_url_can_be_overridden(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_anthropic_models", lambda key: None)

        tail = list(_TAIL)
        tail[9] = "y"
        answers = iter(["5", "claude-sonnet-5", "", *tail[:10], "ws://fleet-host:7776/report", *tail[10:]])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: "sk-ant-fake")

        values = create_env.run_setup_wizard({})

        assert values["MASTER_WS_URL"] == "ws://fleet-host:7776/report"

    def test_per_phase_override_writes_prefixed_keys_only_for_that_phase(self, tmp_path, monkeypatch):
        """The user-named example: PLANNER_MODEL (and a whole separate
        backend/key/context for it) should be configurable without touching
        the shared settings or any other phase."""
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_anthropic_models", lambda key: None)
        monkeypatch.setattr(create_env, "fetch_openai_compatible_models", lambda url, key: ["gpt-4.1"])

        tail = list(_TAIL)
        tail[10] = "y"  # configure per-phase overrides: yes
        # after PER_PHASE: override PLANNER? yes; PLANNER backend=openai(4); PLANNER OPENAI_URL:
        # accept default; PLANNER model: pick #1 (gpt-4.1); PLANNER CONTEXT_SIZE: accept guess;
        # then decline IMP/TESTING/REVIEWER/CLEANUP in turn
        phase_answers = ["y", "4", "", "1", "", "n", "n", "n", "n"]
        input_answers = iter(["5", "claude-sonnet-5", "", *tail, *phase_answers])
        getpass_answers = iter(["sk-ant-fake", "sk-openai-fake"])  # shared ANTHROPIC key, then PLANNER's openai key
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(input_answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: next(getpass_answers))

        values = create_env.run_setup_wizard({})

        assert values["PLANNER_LLM_BACKEND"] == "openai"
        assert values["PLANNER_OPENAI_URL"] == "https://api.openai.com/v1"
        assert values["PLANNER_OPENAI_API_KEY"] == "sk-openai-fake"
        assert values["PLANNER_MODEL"] == "gpt-4.1"
        assert values["PLANNER_CONTEXT_SIZE"] == "1000000"
        assert "IMP_LLM_BACKEND" not in values
        assert "TESTING_LLM_BACKEND" not in values
        assert "REVIEWER_LLM_BACKEND" not in values
        assert "CLEANUP_LLM_BACKEND" not in values
        # the shared settings must be completely untouched by the phase override
        assert values["LLM_BACKEND"] == "anthropic"
        assert values["MODEL"] == "claude-sonnet-5"

    def test_declining_per_phase_setup_entirely_skips_every_phase_prompt(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_anthropic_models", lambda key: None)

        # If declining "configure per-phase overrides?" didn't short-circuit, the wizard would
        # keep asking (5 more phase prompts) and this iterator would run dry -> StopIteration.
        answers = iter(["5", "claude-sonnet-5", "", *_TAIL])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: "sk-ant-fake")

        values = create_env.run_setup_wizard({})

        for phase in create_env.PHASE_PREFIXES:
            assert f"{phase}_LLM_BACKEND" not in values
            assert f"{phase}_MODEL" not in values


class TestNextVersionedEnvPath:
    def test_first_call_returns_env_v1(self, tmp_path, monkeypatch):
        monkeypatch.setattr(create_env, "ENV_PATH", tmp_path / ".env")
        assert create_env._next_versioned_env_path() == tmp_path / ".env_v1"

    def test_skips_versions_that_already_exist(self, tmp_path, monkeypatch):
        monkeypatch.setattr(create_env, "ENV_PATH", tmp_path / ".env")
        (tmp_path / ".env_v1").write_text("x", encoding="utf-8")
        (tmp_path / ".env_v2").write_text("x", encoding="utf-8")
        assert create_env._next_versioned_env_path() == tmp_path / ".env_v3"


class TestMainSetupWizardTrigger:
    """main() should only launch the interactive wizard in a real terminal,
    and should skip it entirely once .env is already fully configured --
    otherwise a perfectly working setup gets re-prompted on every run."""

    def _isolated_env(self, tmp_path, monkeypatch, content):
        env_path = tmp_path / ".env"
        env_path.write_text(content, encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "ENV_TEMPLATE_PATH", tmp_path / "nope")
        return env_path

    def test_non_interactive_session_never_calls_the_wizard(self, tmp_path, monkeypatch):
        self._isolated_env(tmp_path, monkeypatch, "MODEL=m\n")
        monkeypatch.setattr(create_env.sys.stdin, "isatty", lambda: False)
        wizard = mock.Mock()
        monkeypatch.setattr(create_env, "run_setup_wizard", wizard)
        monkeypatch.setattr(create_env, "check_llm_reachable", lambda url, **k: True)

        create_env.main()

        wizard.assert_not_called()

    def test_interactive_session_with_a_fully_configured_env_skips_the_wizard(self, tmp_path, monkeypatch, capsys):
        self._isolated_env(tmp_path, monkeypatch, "OPENAI_URL=http://x/v1\nOPENAI_API_KEY=k\nMODEL=m\n")
        monkeypatch.setattr(create_env.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(create_env.sys, "argv", ["create-env"])
        wizard = mock.Mock()
        monkeypatch.setattr(create_env, "run_setup_wizard", wizard)
        monkeypatch.setattr(create_env, "check_llm_reachable", lambda url, **k: True)

        create_env.main()

        wizard.assert_not_called()
        assert "skipping setup" in capsys.readouterr().out

    def test_interactive_session_with_missing_config_runs_the_wizard(self, tmp_path, monkeypatch):
        env_path = self._isolated_env(tmp_path, monkeypatch, "MODEL=m\n")
        monkeypatch.setattr(create_env.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(create_env.sys, "argv", ["create-env"])
        wizard = mock.Mock(return_value={"OPENAI_URL": "http://x/v1", "OPENAI_API_KEY": "k", "MODEL": "m"})
        monkeypatch.setattr(create_env, "run_setup_wizard", wizard)
        monkeypatch.setattr(create_env, "check_llm_reachable", lambda url, **k: True)

        create_env.main()

        wizard.assert_called_once()
        # .env already existed (even if incomplete) BEFORE this run, so the wizard must be told
        # to write a new version rather than overwrite it -- see TestVersionedWizardWrites below
        # for the case where .env genuinely didn't exist yet.
        assert wizard.call_args[0][1] == env_path.parent / ".env_v1"

    def test_setup_flag_forces_the_wizard_even_when_already_configured(self, tmp_path, monkeypatch):
        env_path = self._isolated_env(tmp_path, monkeypatch, "OPENAI_URL=http://x/v1\nOPENAI_API_KEY=k\nMODEL=m\n")
        monkeypatch.setattr(create_env.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(create_env.sys, "argv", ["create-env", "--setup"])
        wizard = mock.Mock(return_value={"OPENAI_URL": "http://x/v1", "OPENAI_API_KEY": "k", "MODEL": "m"})
        monkeypatch.setattr(create_env, "run_setup_wizard", wizard)
        monkeypatch.setattr(create_env, "check_llm_reachable", lambda url, **k: True)

        create_env.main()

        wizard.assert_called_once()
        assert wizard.call_args[0][1] == env_path.parent / ".env_v1"


class TestVersionedWizardWrites:
    """The user-requested behavior: create .env itself the very first time
    (nothing existed before this run at all), but once .env exists, never
    touch it again -- every later wizard save (including --setup reruns)
    goes to a fresh .env_v1/.env_v2/... instead."""

    def test_genuinely_fresh_run_writes_env_itself(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        template_path = tmp_path / "JFI_ENV_TEMPLATE"
        template_path.write_text("MODEL=qwen3:8b\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "ENV_TEMPLATE_PATH", template_path)
        monkeypatch.setattr(create_env.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(create_env.sys, "argv", ["create-env"])
        wizard = mock.Mock(return_value={"OPENAI_URL": "http://x/v1", "OPENAI_API_KEY": "k", "MODEL": "m"})
        monkeypatch.setattr(create_env, "run_setup_wizard", wizard)
        monkeypatch.setattr(create_env, "check_llm_reachable", lambda url, **k: True)

        create_env.main()

        wizard.assert_called_once()
        assert wizard.call_args[0][1] == env_path

    def test_second_reconfigure_after_a_version_already_exists_increments_again(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=m\n", encoding="utf-8")
        (tmp_path / ".env_v1").write_text("MODEL=m\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "ENV_TEMPLATE_PATH", tmp_path / "nope")
        monkeypatch.setattr(create_env.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(create_env.sys, "argv", ["create-env", "--setup"])
        wizard = mock.Mock(return_value={"OPENAI_URL": "http://x/v1", "OPENAI_API_KEY": "k", "MODEL": "m"})
        monkeypatch.setattr(create_env, "run_setup_wizard", wizard)
        monkeypatch.setattr(create_env, "check_llm_reachable", lambda url, **k: True)

        create_env.main()

        assert wizard.call_args[0][1] == tmp_path / ".env_v2"

    def test_run_setup_wizard_writes_to_the_given_target_not_env_path(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        versioned_path = tmp_path / ".env_v1"
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_anthropic_models", lambda key: None)

        answers = iter(["5", "claude-sonnet-5", "", *_TAIL])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: "sk-ant-fake")

        values = create_env.run_setup_wizard({}, versioned_path)

        assert versioned_path.exists()
        assert values["MODEL"] == "claude-sonnet-5"
        assert env_path.read_text(encoding="utf-8") == "MODEL=old\n"  # untouched

    def test_run_setup_wizard_warns_when_writing_a_version_instead_of_env(self, tmp_path, monkeypatch, capsys):
        env_path = tmp_path / ".env"
        env_path.write_text("MODEL=old\n", encoding="utf-8")
        versioned_path = tmp_path / ".env_v1"
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "fetch_anthropic_models", lambda key: None)

        answers = iter(["5", "claude-sonnet-5", "", *_TAIL])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(create_env.getpass, "getpass", lambda *a, **k: "sk-ant-fake")

        create_env.run_setup_wizard({}, versioned_path)

        out = capsys.readouterr().out
        assert "already existed" in out
        assert ".env_v1" in out
