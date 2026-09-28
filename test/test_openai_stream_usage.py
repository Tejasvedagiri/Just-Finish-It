"""OpenAICompatableStream asks servers for real token counts
(stream_options.include_usage) -- the chars/4 estimate can be 20-30% off on
code, and the v2 episode budget is a hard stop -- without breaking servers
that reject the option."""
from __future__ import annotations

import pytest

from JFI.llm.openai_compatable_stream import OpenAICompatableStream


class _BadRequest(Exception):
    status_code = 400


class _FakeCompletions:
    def __init__(self, reject_stream_options):
        self.calls = []
        self.reject = reject_stream_options

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject and "stream_options" in kwargs:
            raise _BadRequest("Unrecognized request argument supplied: stream_options")
        return iter(())


class _FakeService:
    def __init__(self, reject):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(reject)})()

    def close(self):
        pass


@pytest.fixture
def stream(monkeypatch):
    monkeypatch.setenv("OPENAI_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("MODEL", "m")
    return OpenAICompatableStream()


def test_asks_for_usage(stream):
    stream.stream_service = _FakeService(reject=False)
    stream.send_message([{"role": "user", "content": "hi"}])
    assert stream.stream_service.chat.completions.calls[0]["stream_options"] == {"include_usage": True}


def test_server_rejecting_the_option_falls_back_once_and_for_good(stream):
    stream.stream_service = _FakeService(reject=True)
    stream.send_message([{"role": "user", "content": "hi"}])
    stream.send_message([{"role": "user", "content": "again"}])
    calls = stream.stream_service.chat.completions.calls
    assert ["stream_options" in c for c in calls] == [True, False, False]


def test_other_errors_are_not_swallowed(stream):
    class Boom(_FakeCompletions):
        def create(self, **kwargs):
            raise RuntimeError("connection refused")

    stream.stream_service = _FakeService(reject=False)
    stream.stream_service.chat.completions = Boom(False)
    with pytest.raises(RuntimeError):
        stream.send_message([{"role": "user", "content": "hi"}])
