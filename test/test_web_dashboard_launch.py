"""Tests for auto-launching the `jfi-web` dashboard alongside `./jfi` (see
runner._launch_web_dashboard / _stop_web_dashboard / run_pipeline): with
JFI_WEB_BRIDGE=1, a single `./jfi` should give both the terminal and the
browser, without a second command in a second terminal -- unless
JFI_WEB_DASHBOARD=0 opts back out (e.g. the dashboard is meant to run on a
different machine, per the README's split-process setup).
"""

import os
import socket
import subprocess

import pytest

import JFI.runner as runner
from JFI.manager.abstract_manager import AbstractManager


class FakeConsole(AbstractManager):
    def __init__(self):
        self.system_messages: list[str] = []

    def display_system(self, text, *a, **k):
        self.system_messages.append(text)

    def display_assistant(self, *a, **k): pass
    def display_user(self, *a, **k): pass
    def get_user_input(self, *a, **k): pass
    def print_agent_response(self, *a, **k): pass


class TestWebDashboardDisabled:
    @pytest.mark.parametrize("value", ["0", "false", "False", "no", "off"])
    def test_recognized_falsy_values_disable_it(self, monkeypatch, value):
        monkeypatch.setenv("JFI_WEB_DASHBOARD", value)
        assert runner._web_dashboard_disabled() is True

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", ""])
    def test_everything_else_leaves_it_enabled(self, monkeypatch, value):
        monkeypatch.setenv("JFI_WEB_DASHBOARD", value)
        assert runner._web_dashboard_disabled() is False

    def test_unset_leaves_it_enabled(self, monkeypatch):
        monkeypatch.delenv("JFI_WEB_DASHBOARD", raising=False)
        assert runner._web_dashboard_disabled() is False


class TestWebDashboardPort:
    def test_defaults_to_launcher_default_port(self, monkeypatch):
        from JFI.web.launcher import DEFAULT_PORT

        monkeypatch.delenv("JFI_WEB_PORT", raising=False)
        assert runner._web_dashboard_port() == DEFAULT_PORT

    def test_honors_jfi_web_port(self, monkeypatch):
        monkeypatch.setenv("JFI_WEB_PORT", "9999")
        assert runner._web_dashboard_port() == 9999

    def test_falls_back_on_garbage_value(self, monkeypatch):
        from JFI.web.launcher import DEFAULT_PORT

        monkeypatch.setenv("JFI_WEB_PORT", "not-a-port")
        assert runner._web_dashboard_port() == DEFAULT_PORT


class TestPortListening:
    def test_false_for_a_free_port(self):
        # Bind briefly to grab a genuinely free ephemeral port, then release it.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        assert runner._port_listening(free_port) is False

    def test_true_for_a_bound_listening_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            assert runner._port_listening(port) is True


class TestWebDashboardCommand:
    """_web_dashboard_command's fallback for the standalone PyInstaller
    binary: no separate `jfi-web` install needed there, since build_binary
    bundles streamlit in and runner.py can re-invoke itself instead."""

    def test_prefers_jfi_web_on_path_when_present(self, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/jfi-web")
        monkeypatch.setattr(runner.sys, "frozen", True, raising=False)

        assert runner._web_dashboard_command() == ["/usr/bin/jfi-web"]

    def test_falls_back_to_self_reinvocation_when_frozen_and_jfi_web_missing(self, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda name: None)
        monkeypatch.setattr(runner.sys, "frozen", True, raising=False)
        monkeypatch.setattr(runner.sys, "executable", "/path/to/dist/jfi", raising=False)

        assert runner._web_dashboard_command() == ["/path/to/dist/jfi", "--internal-web-dashboard"]

    def test_returns_none_when_not_frozen_and_jfi_web_missing(self, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda name: None)
        monkeypatch.delattr(runner.sys, "frozen", raising=False)

        assert runner._web_dashboard_command() is None


class TestLaunchWebDashboard:
    def test_skips_when_something_already_listens_on_the_port(self, monkeypatch):
        console = FakeConsole()
        monkeypatch.setattr(runner, "_port_listening", lambda port, host="127.0.0.1": True)
        spy = []
        monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: spy.append((a, k)))

        result = runner._launch_web_dashboard(console)

        assert result is None
        assert not spy  # never even tried to spawn one
        assert any("already running" in m for m in console.system_messages)

    def test_skips_and_hints_when_jfi_web_is_not_on_path(self, monkeypatch):
        console = FakeConsole()
        monkeypatch.setattr(runner, "_port_listening", lambda port, host="127.0.0.1": False)
        monkeypatch.setattr(runner.shutil, "which", lambda name: None)

        result = runner._launch_web_dashboard(console)

        assert result is None
        assert any("jfi-web" in m and "not" in m for m in console.system_messages)

    def test_launches_the_subprocess_when_jfi_web_is_found_and_port_is_free(self, monkeypatch):
        console = FakeConsole()
        monkeypatch.setattr(runner, "_port_listening", lambda port, host="127.0.0.1": False)
        monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/jfi-web")

        captured = {}

        class FakeProc:
            pass

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return FakeProc()

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)

        result = runner._launch_web_dashboard(console)

        assert isinstance(result, FakeProc)
        assert captured["cmd"] == ["/usr/bin/jfi-web"]
        assert any("Web dashboard starting" in m for m in console.system_messages)

    def test_popen_failure_is_reported_but_not_raised(self, monkeypatch):
        console = FakeConsole()
        monkeypatch.setattr(runner, "_port_listening", lambda port, host="127.0.0.1": False)
        monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/jfi-web")

        def raising_popen(*a, **k):
            raise OSError("no such file")

        monkeypatch.setattr(runner.subprocess, "Popen", raising_popen)

        result = runner._launch_web_dashboard(console)

        assert result is None
        assert any("failed to start" in m for m in console.system_messages)


class TestStopWebDashboard:
    def test_none_is_a_no_op(self):
        runner._stop_web_dashboard(None)  # must not raise

    def test_already_exited_process_is_left_alone(self):
        class FakeProc:
            def poll(self):
                return 0  # already exited

            def terminate(self):
                raise AssertionError("must not terminate an already-exited process")

        runner._stop_web_dashboard(FakeProc())

    def test_running_process_is_terminated(self):
        calls = []

        class FakeProc:
            def poll(self):
                return None  # still running

            def terminate(self):
                calls.append("terminate")

            def wait(self, timeout=None):
                calls.append(("wait", timeout))

            def kill(self):
                calls.append("kill")

        runner._stop_web_dashboard(FakeProc())

        assert calls[0] == "terminate"
        assert calls[1][0] == "wait"

    def test_hung_process_is_killed_after_terminate_times_out(self):
        class FakeProc:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="jfi-web", timeout=timeout)

            def kill(self):
                self.killed = True

        proc = FakeProc()
        runner._stop_web_dashboard(proc)

        assert proc.killed is True


class TestRunPipelineLaunchesAndStopsTheDashboard:
    def _fake_console(self):
        return FakeConsole()

    def test_launches_when_bridge_enabled_and_stops_on_exit(self, monkeypatch):
        console = self._fake_console()
        monkeypatch.setattr(runner, "_web_bridge_enabled", lambda: True)
        monkeypatch.setattr(runner, "_web_dashboard_disabled", lambda: False)
        monkeypatch.setattr(runner, "_run_session", lambda console, llms: None)

        calls = []
        monkeypatch.setattr(runner, "_launch_web_dashboard", lambda console: calls.append("launch") or "proc")
        monkeypatch.setattr(runner, "_stop_web_dashboard", lambda proc: calls.append(("stop", proc)))

        runner.run_pipeline(console, {})

        assert calls == ["launch", ("stop", "proc")]

    def test_skipped_when_bridge_disabled(self, monkeypatch):
        console = self._fake_console()
        monkeypatch.setattr(runner, "_web_bridge_enabled", lambda: False)
        monkeypatch.setattr(runner, "_run_session", lambda console, llms: None)

        calls = []
        monkeypatch.setattr(runner, "_launch_web_dashboard", lambda console: calls.append("launch"))
        monkeypatch.setattr(runner, "_stop_web_dashboard", lambda proc: calls.append(("stop", proc)))

        runner.run_pipeline(console, {})

        assert calls == [("stop", None)]  # stop is still called, harmlessly, with nothing to stop

    def test_skipped_when_dashboard_explicitly_disabled(self, monkeypatch):
        console = self._fake_console()
        monkeypatch.setattr(runner, "_web_bridge_enabled", lambda: True)
        monkeypatch.setattr(runner, "_web_dashboard_disabled", lambda: True)
        monkeypatch.setattr(runner, "_run_session", lambda console, llms: None)

        calls = []
        monkeypatch.setattr(runner, "_launch_web_dashboard", lambda console: calls.append("launch"))
        monkeypatch.setattr(runner, "_stop_web_dashboard", lambda proc: calls.append(("stop", proc)))

        runner.run_pipeline(console, {})

        assert calls == [("stop", None)]  # explicitly-disabled variant


class TestInternalWebDashboardFlag:
    """--internal-web-dashboard is how a frozen `./jfi` re-invokes itself to
    serve the dashboard (see TestWebDashboardCommand) -- hidden from
    --help, parsed by runner's own argparser, and never forwarded on to
    Streamlit's (see JFI.web.launcher.main's extra_args param)."""

    def test_parsed_as_a_hidden_flag(self):
        args = runner._parse_args(["--internal-web-dashboard"])
        assert args.internal_web_dashboard is True

    def test_defaults_to_false(self):
        args = runner._parse_args([])
        assert args.internal_web_dashboard is False

    def test_hidden_from_help_text(self, capsys):
        with pytest.raises(SystemExit):
            runner._parse_args(["--help"])
        assert "--internal-web-dashboard" not in capsys.readouterr().out

    def test_main_dispatches_straight_to_the_dashboard_and_returns(self, monkeypatch):
        import sys

        monkeypatch.setattr(sys, "argv", ["jfi", "--internal-web-dashboard"])

        called = {}
        monkeypatch.setattr(
            "JFI.web.launcher.main", lambda extra_args=None: called.setdefault("extra_args", extra_args)
        )
        # If it fell through instead of returning early, this would explode
        # (no .env, no real console) -- proves the early return actually happened.
        monkeypatch.setattr(runner, "load_dotenv", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not reach the normal pipeline setup")
        ))

        runner.main()

        assert called == {"extra_args": []}


class TestDashboardPathResolution:
    """JFI.web.launcher._dashboard_path: a real file streamlit can exec,
    both from source and from inside a frozen PyInstaller bundle."""

    def test_uses_file_relative_path_when_not_frozen(self):
        from JFI.web.launcher import _dashboard_path

        path = _dashboard_path()
        assert path.endswith("dashboard.py")
        assert "web" in path

    def test_uses_meipass_when_frozen(self, monkeypatch, tmp_path):
        import sys

        from JFI.web import launcher

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

        assert launcher._dashboard_path() == str(tmp_path / "JFI" / "web" / "dashboard.py")


class TestForceProductionMode:
    """Streamlit's global.developmentMode auto-detects True inside a frozen
    PyInstaller bundle (its own config.py's __file__ isn't under
    site-packages there), which then makes it refuse --server.port outright
    -- see _force_production_mode's docstring. Must be forced off via env
    var for the dashboard to bind the port at all when frozen."""

    def test_sets_the_env_var_when_unset(self, monkeypatch):
        from JFI.web.launcher import _force_production_mode

        monkeypatch.delenv("STREAMLIT_GLOBAL_DEVELOPMENT_MODE", raising=False)
        _force_production_mode()
        assert os.environ["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] == "false"

    def test_never_overrides_an_explicit_setting(self, monkeypatch):
        from JFI.web.launcher import _force_production_mode

        monkeypatch.setenv("STREAMLIT_GLOBAL_DEVELOPMENT_MODE", "true")
        _force_production_mode()
        assert os.environ["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] == "true"


class TestLauncherMainExtraArgs:
    def test_explicit_empty_extra_args_never_reads_sys_argv(self, monkeypatch):
        """The internal re-invocation path passes extra_args=[] precisely so
        --internal-web-dashboard (still sitting in this process's own
        sys.argv) never leaks through to Streamlit's own arg parser.

        Only meaningful with the optional `web` extra installed (streamlit
        itself) -- skipped otherwise, same as the rest of the dashboard
        would be unavailable without it."""
        import sys

        pytest.importorskip("streamlit")
        from streamlit.web import cli as real_stcli

        from JFI.web import launcher

        monkeypatch.setattr(sys, "argv", ["jfi", "--internal-web-dashboard"])
        monkeypatch.setattr(launcher, "_skip_first_run_email_prompt", lambda: None)
        monkeypatch.setattr(launcher, "_dashboard_path", lambda: "/x/dashboard.py")
        monkeypatch.delenv("JFI_WEB_PORT", raising=False)

        captured = {}
        monkeypatch.setattr(real_stcli, "main", lambda: captured.setdefault("argv", list(sys.argv)) or 0)

        with pytest.raises(SystemExit):
            launcher.main(extra_args=[])

        assert "--internal-web-dashboard" not in captured["argv"]
        assert captured["argv"] == ["streamlit", "run", "/x/dashboard.py", "--server.port", "7777"]
