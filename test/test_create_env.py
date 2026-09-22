"""`uv run create-env` (src/JFI/create_env.py) -- a standalone, stdlib-only
system check + .env_bk setup helper. Each check is a pure/small function so it
can be tested in isolation without actually needing a real LLM server or
touching this repo's own real .env_bk.
"""

import urllib.error

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
        env_file = tmp_path / ".env_bk"
        env_file.write_text("OPENAI_URL=http://127.0.0.1:1234/v1\nMODEL=qwen3:8b\n", encoding="utf-8")
        values = create_env._parse_env_file(env_file)
        assert values["OPENAI_URL"] == "http://127.0.0.1:1234/v1"
        assert values["MODEL"] == "qwen3:8b"

    def test_ignores_comments_and_blank_lines(self, tmp_path):
        env_file = tmp_path / ".env_bk"
        env_file.write_text("# a comment\n\nMODEL=x\n", encoding="utf-8")
        assert create_env._parse_env_file(env_file) == {"MODEL": "x"}

    def test_strips_surrounding_quotes(self, tmp_path):
        env_file = tmp_path / ".env_bk"
        env_file.write_text('MODEL="qwen3:8b"\n', encoding="utf-8")
        assert create_env._parse_env_file(env_file) == {"MODEL": "qwen3:8b"}

    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert create_env._parse_env_file(tmp_path / "nope.env") == {}


class TestEnsureEnvFile:
    def test_creates_env_from_template_when_missing(self, tmp_path, monkeypatch, capsys):
        env_path = tmp_path / ".env_bk"
        template_path = tmp_path / "JFI_ENV_TEMPLATE"
        template_path.write_text("OPENAI_URL=http://127.0.0.1:11434/v1\nMODEL=qwen3:8b\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "ENV_TEMPLATE_PATH", template_path)

        values = create_env.ensure_env_file()

        assert env_path.exists()
        assert env_path.read_text(encoding="utf-8") == template_path.read_text(encoding="utf-8")
        assert values["MODEL"] == "qwen3:8b"
        assert "Created .env_bk" in capsys.readouterr().out

    def test_leaves_an_existing_env_untouched(self, tmp_path, monkeypatch, capsys):
        env_path = tmp_path / ".env_bk"
        env_path.write_text("MODEL=already-configured\n", encoding="utf-8")
        template_path = tmp_path / "JFI_ENV_TEMPLATE"
        template_path.write_text("MODEL=template-default\n", encoding="utf-8")
        monkeypatch.setattr(create_env, "ENV_PATH", env_path)
        monkeypatch.setattr(create_env, "ENV_TEMPLATE_PATH", template_path)

        values = create_env.ensure_env_file()

        assert values["MODEL"] == "already-configured"
        assert "already exists" in capsys.readouterr().out

    def test_reports_clearly_when_both_env_and_template_are_missing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(create_env, "ENV_PATH", tmp_path / ".env_bk")
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
