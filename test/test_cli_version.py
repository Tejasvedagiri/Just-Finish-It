"""--version / --help: main() used to ignore argv entirely, so checking the
version accidentally launched the full interactive session prompt instead
of just printing something and exiting."""

import subprocess
import sys

import pytest

from JFI.runner import _parse_args, _version


def test_version_flag_is_recognized():
    args = _parse_args(["--version"])
    assert args.version is True


def test_no_args_leaves_version_false():
    args = _parse_args([])
    assert args.version is False


def test_unknown_flag_exits_nonzero_instead_of_being_silently_ignored():
    with pytest.raises(SystemExit) as exc_info:
        _parse_args(["--nonsense"])
    assert exc_info.value.code != 0


def test_version_string_has_a_fallback_when_not_installed_as_a_package(monkeypatch):
    import JFI.runner as runner
    from importlib.metadata import PackageNotFoundError

    def raise_not_found(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(runner, "_package_version", raise_not_found)
    assert "unknown" in _version()


def test_main_prints_version_and_exits_without_starting_a_session(monkeypatch, capsys):
    """--version must exit before load_dotenv/console construction -- it
    should never reach, let alone start, the interactive prompt."""
    import JFI.runner as runner

    monkeypatch.setattr(sys, "argv", ["jfi", "--version"])

    def boom(*a, **k):
        raise AssertionError("main() kept going past --version instead of exiting early")

    monkeypatch.setattr(runner, "load_dotenv", boom)

    runner.main()

    out = capsys.readouterr().out
    assert out.strip().startswith("JFI ")


def test_cli_version_flag_end_to_end_via_subprocess():
    """A real subprocess invocation, not just calling main() in-process --
    proves the console-script entry point / -m JFI.runner path both work."""
    result = subprocess.run(
        [sys.executable, "-m", "JFI.runner", "--version"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip().startswith("JFI ")


def test_cli_help_flag_end_to_end_via_subprocess():
    result = subprocess.run(
        [sys.executable, "-m", "JFI.runner", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "--version" in result.stdout


def test_cli_unknown_flag_end_to_end_via_subprocess():
    result = subprocess.run(
        [sys.executable, "-m", "JFI.runner", "--nonsense"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr
