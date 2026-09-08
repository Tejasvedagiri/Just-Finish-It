"""
Tests for how JFI.runner handles a failed LLM call.

Regression: openai's SDK embeds a non-JSON error response's raw body
verbatim into str(exception) (see its _make_status_error_from_response) — a
crashed local LLM server that falls back to a framework's generic HTML error
page (nginx/Flask/Werkzeug/whatever's fronting the model) dumps that whole
page into the console instead of a clean message. _format_llm_error detects
this (via status_code/response, not string-sniffing) and summarizes it.
_is_retryable_llm_error classifies which failures are worth an automatic
retry: a transient network/server issue, never an identical request the
server already rejected on its merits (4xx).
"""

import httpx
import pytest
from openai import APIConnectionError, BadRequestError, InternalServerError

_REQUEST = httpx.Request("POST", "http://127.0.0.1:1234/v1/chat/completions")

_HTML_500_BODY = (
    '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    "<title>Error</title>\n</head>\n<body>\n<pre>Internal Server Error</pre>\n"
    "</body>\n</html>"
)


def _html_500_error() -> InternalServerError:
    response = httpx.Response(500, request=_REQUEST, text=_HTML_500_BODY,
                               headers={"content-type": "text/html"})
    return InternalServerError("Error code: 500", response=response, body=None)


def _json_500_error() -> InternalServerError:
    body = {"error": {"message": "out of memory"}}
    response = httpx.Response(500, request=_REQUEST, text='{"error": {"message": "out of memory"}}',
                               headers={"content-type": "application/json"})
    return InternalServerError("Error code: 500 - " + str(body), response=response, body=body)


def _bad_request_error() -> BadRequestError:
    body = {"error": "context length exceeded"}
    response = httpx.Response(400, request=_REQUEST, text='{"error": "context length exceeded"}',
                               headers={"content-type": "application/json"})
    return BadRequestError("Error code: 400 - " + str(body), response=response, body=body)


class TestFormatLlmError:
    def test_html_error_page_is_summarized_not_dumped_verbatim(self):
        """A bounded, single-line preview is fine (still useful for
        debugging); the raw multi-line HTML body is not — the original bug
        was the whole page, newlines and all, landing in the console."""
        from JFI.runner import _format_llm_error

        message = _format_llm_error(_html_500_error())

        assert "\n" not in message  # single line, not the raw sprawling page
        assert "HTTP 500" in message
        assert "Internal Server Error" in message  # still present, as a bounded preview

    def test_preview_stays_bounded_regardless_of_body_size(self):
        """The invariant that actually matters: the preview doesn't grow
        unboundedly with the error page's size (a large HTML dump must not
        flood the console just because the server's page happened to be
        large)."""
        from JFI.runner import _format_llm_error

        huge_html = "<!DOCTYPE html><html><body>" + ("x" * 50_000) + "</body></html>"
        response = httpx.Response(500, request=_REQUEST, text=huge_html,
                                   headers={"content-type": "text/html"})
        err = InternalServerError("Error code: 500", response=response, body=None)

        message = _format_llm_error(err)
        assert len(message) < 500

    def test_json_error_passes_through_unchanged(self):
        """A well-behaved server's proper JSON error body is left as the SDK
        formats it — only the HTML-fallback case needs summarizing."""
        from JFI.runner import _format_llm_error

        message = _format_llm_error(_json_500_error())
        assert message == str(_json_500_error())
        assert "out of memory" in message

    def test_non_api_exception_passes_through(self):
        from JFI.runner import _format_llm_error

        assert _format_llm_error(ValueError("boom")) == "boom"


class TestIsRetryableLlmError:
    def test_connection_error_is_retryable(self):
        from JFI.runner import _is_retryable_llm_error

        assert _is_retryable_llm_error(APIConnectionError(request=_REQUEST)) is True

    def test_5xx_status_is_retryable(self):
        from JFI.runner import _is_retryable_llm_error

        assert _is_retryable_llm_error(_html_500_error()) is True
        assert _is_retryable_llm_error(_json_500_error()) is True

    def test_4xx_status_is_not_retryable(self):
        """A client error (bad request, auth, context length) is the server
        rejecting THIS request on its merits — retrying it unchanged can't
        produce a different result."""
        from JFI.runner import _is_retryable_llm_error

        assert _is_retryable_llm_error(_bad_request_error()) is False

    def test_unrelated_exception_is_not_retryable(self):
        from JFI.runner import _is_retryable_llm_error

        assert _is_retryable_llm_error(ValueError("boom")) is False


class _FakeConsole:
    """Just enough of AbstractManager's surface for run_phase."""

    def __init__(self):
        self.errors = []
        self.systems = []
        self._stopped = False
        self.choice_prompts = []  # (prompt_label, options) for every get_user_choice call
        self.choice_answers = []  # queued answers get_user_choice returns, in order

    def should_stop(self):
        return self._stopped

    def request_stop(self):
        self._stopped = True

    def get_user_choice(self, prompt_label, options):
        self.choice_prompts.append((prompt_label, options))
        return self.choice_answers.pop(0) if self.choice_answers else options[0][0]

    def set_status(self, **kwargs):
        pass

    def display_rule(self, label=""):
        pass

    def display_error(self, text):
        self.errors.append(text)

    def display_system(self, text):
        self.systems.append(text)

    def drain_forced_input(self):
        return []

    def drain_skip_request(self):
        return False

    def drain_skip_all_request(self):
        return False

    def wait_while_paused(self):
        pass

    def mark_phase_done(self, phase):
        pass

    def print_agent_response(self, response, prompt_tokens_estimate=0):
        return {"content": "IMP_COMPLETE", "tool_calls": None}


class _ScriptedLLM:
    """send_message raises `exceptions` in order, then succeeds."""

    def __init__(self, exceptions):
        self.exceptions = list(exceptions)
        self.calls = 0

    def send_message(self, messages, tools=None):
        self.calls += 1
        if self.exceptions:
            raise self.exceptions.pop(0)
        return object()  # never iterated: print_agent_response is faked


class TestRunPhaseRetry:
    def test_retries_transient_error_then_succeeds(self, make_manager, monkeypatch):
        from JFI.runner import run_phase
        import JFI.runner as runner_module

        monkeypatch.setattr(runner_module, "LLM_RETRY_DELAY_SECONDS", 0)
        ssm = make_manager("retry_ok")
        console = _FakeConsole()
        llm = _ScriptedLLM([_html_500_error()])

        result = run_phase(console, {"imp": llm}, ssm, "imp")

        assert result is True
        assert llm.calls == 2  # one failure, then the retry succeeded
        assert any("retrying" in e.lower() for e in console.errors)
        assert not any("\n" in e for e in console.errors)  # no raw multi-line HTML dump

    def test_prompts_user_after_exhausting_retry_limit_and_stops_on_request(self, make_manager, monkeypatch):
        """Automatic retries running out must NOT end the run on its own —
        the user is asked, and the run only actually stops if they say so."""
        from JFI.runner import run_phase, LLM_RETRY_LIMIT
        import JFI.runner as runner_module

        monkeypatch.setattr(runner_module, "LLM_RETRY_DELAY_SECONDS", 0)
        ssm = make_manager("retry_fail")
        console = _FakeConsole()
        console.choice_answers = ["s"]  # user chooses to stop
        llm = _ScriptedLLM([_html_500_error() for _ in range(LLM_RETRY_LIMIT + 1)])

        result = run_phase(console, {"imp": llm}, ssm, "imp")

        assert result is False
        assert llm.calls == LLM_RETRY_LIMIT + 1
        assert len(console.choice_prompts) == 1
        assert console.should_stop() is True  # request_stop() was called on "Stop"

    def test_non_retryable_error_prompts_instead_of_giving_up_immediately(self, make_manager, monkeypatch):
        from JFI.runner import run_phase
        import JFI.runner as runner_module

        monkeypatch.setattr(runner_module, "LLM_RETRY_DELAY_SECONDS", 0)
        ssm = make_manager("retry_client_err")
        console = _FakeConsole()
        console.choice_answers = ["s"]
        llm = _ScriptedLLM([_bad_request_error()])

        result = run_phase(console, {"imp": llm}, ssm, "imp")

        assert result is False
        assert llm.calls == 1  # never auto-retried
        assert not any("retrying" in e.lower() for e in console.errors)
        assert console.should_stop() is True  # request_stop() was called on "Stop"

    def test_user_can_retry_after_a_failure_and_succeed(self, make_manager, monkeypatch):
        """Choosing "Retry now" tries the LLM call again -- and a manual
        retry earns its own fresh automatic-retry budget, so it isn't
        treated as already having exhausted the last one."""
        from JFI.runner import run_phase
        import JFI.runner as runner_module

        monkeypatch.setattr(runner_module, "LLM_RETRY_DELAY_SECONDS", 0)
        ssm = make_manager("retry_manual")
        console = _FakeConsole()
        console.choice_answers = ["r"]
        llm = _ScriptedLLM([_bad_request_error()])  # fails once, then succeeds

        result = run_phase(console, {"imp": llm}, ssm, "imp")

        assert result is True
        assert llm.calls == 2
        assert len(console.choice_prompts) == 1
        assert console.should_stop() is False

    def test_already_stopped_before_the_prompt_returns_without_asking(self, make_manager, monkeypatch):
        """Ctrl+C during the automatic-retry sleep (or any earlier point)
        must not still pop up a "retry or stop?" menu afterward."""
        from JFI.runner import run_phase
        import JFI.runner as runner_module

        monkeypatch.setattr(runner_module, "LLM_RETRY_DELAY_SECONDS", 0)
        ssm = make_manager("retry_already_stopped")
        console = _FakeConsole()
        console._stopped = True
        llm = _ScriptedLLM([_bad_request_error()])

        result = run_phase(console, {"imp": llm}, ssm, "imp")

        assert result is False
        assert console.choice_prompts == []
