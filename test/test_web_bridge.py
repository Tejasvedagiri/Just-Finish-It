"""Tests for WebBridge's web_answer.json relay: telling apart an answer to
a pending prompt from a brand-new queued request (see the "type" field
documented in web_bridge.py's module docstring), and staying safe against
stale/late writes.
"""

from JFI.manager.web_bridge import WebBridge, atomic_write_json


class FakeConsole:
    def __init__(self, awaiting=None):
        self._awaiting = awaiting
        self.answers: list[str] = []
        self.queued: list[str] = []

    def get_status_snapshot(self):
        return {"awaiting": self._awaiting}

    def submit_external_answer(self, key: str) -> None:
        self.answers.append(key)

    def submit_external_queue_item(self, text: str) -> None:
        self.queued.append(text)


AWAITING = {"prompt": "What is your goal?", "options": []}


def _bridge(tmp_path, console):
    return WebBridge(console, tmp_path)


class TestAnswerRelay:
    def test_explicit_answer_type_is_relayed_while_awaiting(self, tmp_path):
        console = FakeConsole(awaiting=AWAITING)
        bridge = _bridge(tmp_path, console)
        atomic_write_json(bridge._answer_path, {"type": "answer", "key": "build a calculator"})

        bridge._relay_answer(console.get_status_snapshot())

        assert console.answers == ["build a calculator"]
        assert console.queued == []
        assert not bridge._answer_path.exists()

    def test_legacy_payload_with_no_type_is_treated_as_answer(self, tmp_path):
        """Backward compatibility: an older dashboard (or a plain button
        click) only ever wrote {"key": ...} with no "type" field."""
        console = FakeConsole(awaiting=AWAITING)
        bridge = _bridge(tmp_path, console)
        atomic_write_json(bridge._answer_path, {"key": "y"})

        bridge._relay_answer(console.get_status_snapshot())

        assert console.answers == ["y"]
        assert console.queued == []

    def test_answer_is_dropped_when_nothing_is_awaiting(self, tmp_path):
        """A stale/late click -- e.g. the terminal answered first -- must
        never be replayed against some later, unrelated choice."""
        console = FakeConsole(awaiting=None)
        bridge = _bridge(tmp_path, console)
        atomic_write_json(bridge._answer_path, {"type": "answer", "key": "y"})

        bridge._relay_answer(console.get_status_snapshot())

        assert console.answers == []
        assert console.queued == []
        assert not bridge._answer_path.exists()  # still consumed, not left to be replayed

    def test_missing_answer_file_is_a_silent_no_op(self, tmp_path):
        console = FakeConsole(awaiting=AWAITING)
        bridge = _bridge(tmp_path, console)

        bridge._relay_answer(console.get_status_snapshot())  # no file written at all

        assert console.answers == [] and console.queued == []

    def test_malformed_json_is_dropped_without_raising(self, tmp_path):
        console = FakeConsole(awaiting=AWAITING)
        bridge = _bridge(tmp_path, console)
        bridge._answer_path.write_text("not json", encoding="utf-8")

        bridge._relay_answer(console.get_status_snapshot())  # must not raise

        assert console.answers == [] and console.queued == []


class TestQueueRelay:
    def test_queue_type_is_relayed_regardless_of_awaiting(self, tmp_path):
        console = FakeConsole(awaiting=None)  # idle -- nothing pending
        bridge = _bridge(tmp_path, console)
        atomic_write_json(bridge._answer_path, {"type": "queue", "text": "also add a README"})

        bridge._relay_answer(console.get_status_snapshot())

        assert console.queued == ["also add a README"]
        assert console.answers == []

    def test_queue_type_works_even_while_awaiting(self, tmp_path):
        """Unlike an answer, a queue submission never depends on whatever
        else happens to be pending right now."""
        console = FakeConsole(awaiting=AWAITING)
        bridge = _bridge(tmp_path, console)
        atomic_write_json(bridge._answer_path, {"type": "queue", "text": "also add a README"})

        bridge._relay_answer(console.get_status_snapshot())

        assert console.queued == ["also add a README"]
        assert console.answers == []

    def test_blank_queue_text_is_dropped(self, tmp_path):
        console = FakeConsole(awaiting=None)
        bridge = _bridge(tmp_path, console)
        atomic_write_json(bridge._answer_path, {"type": "queue", "text": ""})

        bridge._relay_answer(console.get_status_snapshot())

        assert console.queued == []
