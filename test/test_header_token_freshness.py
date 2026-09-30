"""
Regression: the header's ctx figure (console.set_status(tokens=...)) must
refresh every turn, including a turn with no tool calls (a model thinking out
loud, a reply that gets an AUTO-RECTIFY nudge). The old turn loop once pushed
it only on tool-call turns and at phase completion, so the header showed a
figure several turns stale. Episodes push (request tokens, episode budget)
before every LLM call.
"""

from JFI.episode.brief import ScopeAnchor, build_system_message
from JFI.episode.engine import run_episode
from JFI.episode.tools import EpisodeTools
from JFI.manager.abstract_manager import AbstractManager
from JFI.models import SessionRecord, get_engine, get_session


class _RecordingConsole(AbstractManager):
    def __init__(self, responses):
        self._responses = list(responses)
        self.status_calls: list[dict] = []

    def set_status(self, **kwargs):
        self.status_calls.append(kwargs)

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


def test_tokens_pushed_to_header_on_a_plain_content_turn(tmp_path):
    engine = get_engine(tmp_path)
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="s", repo_path="."))
        db.commit()
    console = _RecordingConsole([
        {"content": "Thinking out loud, not done yet.", "tool_calls": None, "usage": None},
        {"content": None, "usage": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "finish", "arguments": '{"node_id": 0, "summary": "ok"}'}}]},
    ])
    anchor = ScopeAnchor(role="cleanup", node_id=None, node="tidy", finish="finish(0, summary)")
    run_episode(_LLM(), console, engine, "s", role="cleanup", mode="cleanup", anchor=anchor,
                system_message=build_system_message(anchor, "tidy up", []),
                tools=EpisodeTools("cleanup", {"finish": lambda node_id=0, summary="": "done"}), budget=20_000)

    tokens = [c["tokens"] for c in console.status_calls if "tokens" in c]
    assert len(tokens) == 2, "one push per turn, the plain-content turn included"
    assert tokens[1][0] > tokens[0][0] and tokens[0][1] == 20_000
