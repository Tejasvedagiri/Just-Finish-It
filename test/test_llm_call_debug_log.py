"""LOG_LLM_CALL_DEBUG=1: every LLM request/response pair of every episode is
appended to .jfi/llm_debug.jsonl. When the planner, Dev, the reviewer and
cleanup moved to episodes, the flag silently stopped logging anything -- only
the old one-conversation turn loop wrote the file."""

import json
from pathlib import Path

from JFI.episode.brief import ScopeAnchor, build_system_message
from JFI.episode.engine import run_episode
from JFI.episode.tools import EpisodeTools
from JFI.manager.abstract_manager import AbstractManager
from JFI.models import SessionRecord, get_engine, get_session


class _Console(AbstractManager):
    def __init__(self, responses):
        self._responses = list(responses)

    def wait_while_paused(self):
        pass

    def print_agent_response(self, response, prompt_tokens_estimate=0):
        return self._responses.pop(0)

    def display_tool_call(self, *a, **k):
        pass

    def display_tool_result(self, *a, **k):
        pass

    def display_system(self, *a, **k):
        pass

    def display_user(self, *a, **k):
        pass

    def display_assistant(self, *a, **k):
        pass

    def get_user_input(self, *a, **k):
        return ""


class _LLM:
    def send_message(self, messages, tools=None):
        return object()


_FINISH = {"content": None, "usage": None, "tool_calls": [
    {"id": "c1", "type": "function", "function": {"name": "finish", "arguments": '{"node_id": 0, "summary": "ok"}'}}]}


def _episode(project, responses):
    engine = get_engine(project)
    with get_session(engine) as db:
        if db.get(SessionRecord, "s") is None:
            db.add(SessionRecord(session_id="s", repo_path="."))
            db.commit()
    anchor = ScopeAnchor(role="cleanup", node_id=None, node="tidy", finish="finish(0, summary)")
    run_episode(_LLM(), _Console(responses), engine, "s", role="cleanup", mode="cleanup", anchor=anchor,
                system_message=build_system_message(anchor, "tidy up", []),
                tools=EpisodeTools("cleanup", {"finish": lambda node_id=0, summary="": "done"}), budget=20_000)


def _records(project: Path):
    path = project / ".jfi" / "llm_debug.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_no_log_file_when_flag_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("LOG_LLM_CALL_DEBUG", raising=False)
    monkeypatch.setenv("SESSION_PATH", str(tmp_path))
    _episode(tmp_path, [_FINISH])
    assert not (tmp_path / ".jfi" / "llm_debug.jsonl").exists()


def test_logs_one_record_per_call_when_flag_set(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_LLM_CALL_DEBUG", "1")
    monkeypatch.setenv("SESSION_PATH", str(tmp_path))
    _episode(tmp_path, [{"content": "still working", "tool_calls": None, "usage": None}, _FINISH])

    records = _records(tmp_path)
    assert len(records) == 2
    for record in records:
        assert record["role"] == "cleanup" and record["episode_id"] and "timestamp" in record
        assert isinstance(record["request"]["messages"], list) and record["request"]["tools"]
    assert records[0]["response"]["content"] == "still working"
    assert records[1]["response"]["tool_calls"][0]["function"]["name"] == "finish"


def test_accepts_truthy_variants(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_PATH", str(tmp_path))
    for n, value in enumerate(("true", "Yes", "ON"), 1):
        monkeypatch.setenv("LOG_LLM_CALL_DEBUG", value)
        _episode(tmp_path, [_FINISH])
        assert len(_records(tmp_path)) == n
