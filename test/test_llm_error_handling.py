"""
Tests for how a failed LLM call is handled (JFI.llm.retry, used by every
episode in JFI.episode.engine).

Regression: openai's SDK embeds a non-JSON error response's raw body
verbatim into str(exception) (see its _make_status_error_from_response) — a
crashed local LLM server that falls back to a framework's generic HTML error
page (nginx/Flask/Werkzeug/whatever's fronting the model) dumps that whole
page into the console instead of a clean message. format_error detects
this (via status_code/response, not string-sniffing) and summarizes it.
is_retryable classifies which failures are worth an automatic
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
        from JFI.llm.retry import format_error as _format_llm_error

        message = _format_llm_error(_html_500_error())

        assert "\n" not in message  # single line, not the raw sprawling page
        assert "HTTP 500" in message
        assert "Internal Server Error" in message  # still present, as a bounded preview

    def test_preview_stays_bounded_regardless_of_body_size(self):
        """The invariant that actually matters: the preview doesn't grow
        unboundedly with the error page's size (a large HTML dump must not
        flood the console just because the server's page happened to be
        large)."""
        from JFI.llm.retry import format_error as _format_llm_error

        huge_html = "<!DOCTYPE html><html><body>" + ("x" * 50_000) + "</body></html>"
        response = httpx.Response(500, request=_REQUEST, text=huge_html,
                                   headers={"content-type": "text/html"})
        err = InternalServerError("Error code: 500", response=response, body=None)

        message = _format_llm_error(err)
        assert len(message) < 500

    def test_json_error_passes_through_unchanged(self):
        """A well-behaved server's proper JSON error body is left as the SDK
        formats it — only the HTML-fallback case needs summarizing."""
        from JFI.llm.retry import format_error as _format_llm_error

        message = _format_llm_error(_json_500_error())
        assert message == str(_json_500_error())
        assert "out of memory" in message

    def test_non_api_exception_passes_through(self):
        from JFI.llm.retry import format_error as _format_llm_error

        assert _format_llm_error(ValueError("boom")) == "boom"


class TestIsRetryableLlmError:
    def test_connection_error_is_retryable(self):
        from JFI.llm.retry import is_retryable as _is_retryable_llm_error

        assert _is_retryable_llm_error(APIConnectionError(request=_REQUEST)) is True

    def test_5xx_status_is_retryable(self):
        from JFI.llm.retry import is_retryable as _is_retryable_llm_error

        assert _is_retryable_llm_error(_html_500_error()) is True
        assert _is_retryable_llm_error(_json_500_error()) is True

    def test_4xx_status_is_not_retryable(self):
        """A client error (bad request, auth, context length) is the server
        rejecting THIS request on its merits — retrying it unchanged can't
        produce a different result."""
        from JFI.llm.retry import is_retryable as _is_retryable_llm_error

        assert _is_retryable_llm_error(_bad_request_error()) is False

    def test_unrelated_exception_is_not_retryable(self):
        from JFI.llm.retry import is_retryable as _is_retryable_llm_error

        assert _is_retryable_llm_error(ValueError("boom")) is False


class _FakeConsole:
    """Just enough of AbstractManager's surface for run_episode; every
    successful LLM call answers with the episode's finish."""

    def __init__(self):
        self.errors = []
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

    def display_error(self, text):
        self.errors.append(text)

    def display_tool_call(self, *a, **k):
        pass

    def display_tool_result(self, *a, **k):
        pass

    def drain_forced_input(self):
        return []

    def wait_while_paused(self):
        pass

    def print_agent_response(self, response, prompt_tokens_estimate=0):
        return {"content": None, "usage": None, "tool_calls": [{
            "id": "c1", "type": "function", "function": {"name": "finish", "arguments": '{"node_id": 0, "summary": "ok"}'}}]}


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


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    from JFI.llm import retry
    monkeypatch.setattr(retry, "LLM_RETRY_DELAY_SECONDS", 0)


def _episode(tmp_path, console, llm):
    from JFI.episode.brief import ScopeAnchor, build_system_message
    from JFI.episode.engine import run_episode
    from JFI.episode.tools import EpisodeTools
    from JFI.models import SessionRecord, get_engine, get_session

    engine = get_engine(tmp_path)
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="s", repo_path="."))
        db.commit()
    anchor = ScopeAnchor(role="cleanup", node_id=None, node="tidy", finish="finish(0, summary)")
    tools = EpisodeTools("cleanup", {"finish": lambda node_id=0, summary="": "done"})
    return run_episode(llm, console, engine, "s", role="cleanup", mode="cleanup", anchor=anchor,
                       system_message=build_system_message(anchor, "tidy up", []), tools=tools, budget=20_000)


class TestEpisodeRetry:
    """The run never gives up on an LLM failure by itself. Observed on stui
    run 12: LM Studio returned "failed to decode" mid-episode, the episode
    just ended on "error", and three views lost their breakdown."""

    def test_retries_transient_error_then_succeeds(self, tmp_path):
        console = _FakeConsole()
        llm = _ScriptedLLM([_html_500_error()])

        result = _episode(tmp_path, console, llm)

        assert result.end_reason == "finish"
        assert llm.calls == 2  # one failure, then the retry succeeded
        assert any("retrying" in e.lower() for e in console.errors)
        assert not any("\n" in e for e in console.errors)  # no raw multi-line HTML dump

    def test_asks_after_exhausting_the_retry_limit_and_stops_on_request(self, tmp_path):
        from JFI.llm.retry import LLM_RETRY_LIMIT
        console = _FakeConsole()
        console.choice_answers = ["s"]
        llm = _ScriptedLLM([_html_500_error() for _ in range(LLM_RETRY_LIMIT + 1)])

        result = _episode(tmp_path, console, llm)

        assert result.end_reason == "stopped"
        assert llm.calls == LLM_RETRY_LIMIT + 1
        assert len(console.choice_prompts) == 1
        assert console.should_stop() is True

    def test_non_retryable_error_asks_instead_of_giving_up_immediately(self, tmp_path):
        console = _FakeConsole()
        console.choice_answers = ["s"]
        llm = _ScriptedLLM([_bad_request_error()])

        result = _episode(tmp_path, console, llm)

        assert result.end_reason == "stopped"
        assert llm.calls == 1  # never auto-retried
        assert not any("retrying" in e.lower() for e in console.errors)

    def test_user_can_retry_after_a_failure_and_succeed(self, tmp_path):
        """A manual retry earns a fresh automatic-retry budget."""
        console = _FakeConsole()
        console.choice_answers = ["r"]
        llm = _ScriptedLLM([_bad_request_error()])

        result = _episode(tmp_path, console, llm)

        assert result.end_reason == "finish"
        assert llm.calls == 2
        assert len(console.choice_prompts) == 1 and console.should_stop() is False

    def test_already_stopped_returns_without_asking(self, tmp_path):
        """Ctrl+C before or during a failure must not still pop up the menu."""
        console = _FakeConsole()
        console._stopped = True
        llm = _ScriptedLLM([_bad_request_error()])

        result = _episode(tmp_path, console, llm)

        assert result.end_reason == "stopped"
        assert console.choice_prompts == []
