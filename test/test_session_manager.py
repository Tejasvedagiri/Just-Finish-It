"""Session lifecycle tests: folder creation, resume, the queued-requests
round-trip, and history persistence across manager instances."""


def test_constructor_creates_jfi_session_folder(manager):
    from pathlib import Path

    assert (manager.session_path / "history.jsonl.gz").parent.is_dir()
    # `.jfi/` is flat -- one folder per PROJECT, not per session (see
    # SimpleSessionManager.__init__'s own note on why).
    assert manager.session_path.name == ".jfi"
    # The on-disk folder is inside the test cwd.
    assert (Path.cwd() / ".jfi").is_dir()


def test_constructor_normalises_session_id(make_manager):
    ssm = make_manager("My Demo")
    assert ssm.session_id == "my_demo"
    assert ssm.session_path.name == ".jfi"


def test_resume_flag_reflects_existing_history(make_manager, manager):
    # First construction: fresh session.
    assert not manager.is_resuming

    # Add a message (which persists the pickle), then rebuild on the same id.
    manager.add_message("user", "hello")
    resumed = make_manager(manager.session_id)
    assert resumed.is_resuming
    assert len(resumed.history) == 1


def test_queued_requests_round_trip(make_manager):
    first = make_manager("meta")
    assert first.load_queued_requests() == []

    first.save_queued_requests(["add a dark mode", "fix the footer"])

    # Metadata is DB-backed (JFI.session.metadata_store): a second manager
    # for the same session_id reads it back from the project's .jfi/JFI.db.
    assert make_manager("meta").load_queued_requests() == ["add a dark mode", "fix the footer"]


def _append(ssm, message):
    ssm.history.append(message)
    ssm.save_history()


def test_history_persistence_across_instances(make_manager):
    first = make_manager("hist")
    first.add_message("user", "turn one")
    _append(first, {"role": "assistant", "content": "turn two"})

    second = make_manager("hist")
    assert [m["content"] for m in second.history] == ["turn one", "turn two"]


def test_save_history_appends_instead_of_rewriting(make_manager, monkeypatch):
    """Regression: history.pkl used to be rewritten in full on every message,
    so a turn's save cost grew with the whole session's size. save_history()
    must only ever hand the newly added messages to the DB append helper
    (JFI.session.history_store.append_history_to_db), never the whole
    history again."""
    import JFI.session.simple_session_manager as ssm_module

    seen_batches = []
    original = ssm_module.append_history_to_db

    def spy(engine, session_id, messages):
        seen_batches.append(list(messages))
        return original(engine, session_id, messages)

    monkeypatch.setattr(ssm_module, "append_history_to_db", spy)

    ssm = make_manager("append-only")
    ssm.add_message("user", "one")
    ssm.add_message("user", "two")
    _append(ssm, {"role": "assistant", "content": "three"})

    assert [b[0]["content"] for b in seen_batches] == ["one", "two", "three"]
    # No call ever saw more than the single new message that triggered it.
    assert all(len(b) == 1 for b in seen_batches)


def test_legacy_pickle_history_is_migrated_on_load(make_manager):
    """A pre-existing history.pkl (the old full-rewrite format, from before
    the DB cutover) must still load, and gets migrated straight into the DB
    immediately so every save from then on appends there instead of
    resurrecting the pickle."""
    import pickle

    from JFI.session.history_store import has_history

    ssm = make_manager("legacy")
    legacy_path = ssm.session_path / "history.pkl"
    with open(legacy_path, "wb") as f:
        pickle.dump([{"role": "user", "content": "old goal"},
                     {"role": "assistant", "content": "old reply"}], f)

    migrated = make_manager("legacy")
    assert migrated.is_resuming
    assert [m["content"] for m in migrated.history] == ["old goal", "old reply"]
    assert has_history(migrated.db_engine, migrated.session_id)

    # A save after migration appends cleanly, and a fresh load sees it all.
    migrated.add_message("user", "new turn after migration")
    reloaded = make_manager("legacy")
    assert [m["content"] for m in reloaded.history] == [
        "old goal", "old reply", "new turn after migration"
    ]


class TestDanglingToolCallRepair:
    """Regression for a real crash-on-resume bug: killing/crashing the
    process between the assistant's tool-call message being saved and its
    tool result being appended leaves a transcript that most OpenAI-
    compatible servers reject outright on the very next request with
    "Cannot continue an assistant message that contains tool calls" — the
    session could never resume at all until this repair runs on load."""

    def test_repairs_assistant_message_with_zero_recorded_results(self, make_manager):
        ssm = make_manager("crash1")
        ssm.add_message("user", "goal")
        _append(ssm, {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "write_file", "arguments": "{}"}}],
        })
        # Process dies here — no tool result ever recorded.

        resumed = make_manager("crash1")
        assert resumed.history[-1]["role"] == "tool"
        assert resumed.history[-1]["tool_call_id"] == "call_1"
        assert "interrupted" in resumed.history[-1]["content"]

    def test_repairs_only_the_unanswered_call_in_a_partial_turn(self, make_manager):
        """Multiple tool calls in one turn; only some got results before the
        crash. The repair must fill in exactly the missing ones and leave
        the already-recorded result untouched."""
        ssm = make_manager("crash2")
        ssm.add_message("user", "goal")
        _append(ssm, {
            "role": "assistant", "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": "{}"}},
                {"id": "call_2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            ],
        })
        _append(ssm, {"role": "tool", "tool_call_id": "call_1", "name": "write_file", "content": "Success"})
        # Crash here — call_2's result never recorded.

        resumed = make_manager("crash2")
        tool_msgs = {m["tool_call_id"]: m for m in resumed.history if m.get("role") == "tool"}
        assert set(tool_msgs) == {"call_1", "call_2"}
        assert tool_msgs["call_1"]["content"] == "Success"
        assert "interrupted" in tool_msgs["call_2"]["content"]

    def test_repair_is_idempotent_across_repeated_resumes(self, make_manager):
        ssm = make_manager("crash3")
        ssm.add_message("user", "goal")
        _append(ssm, {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "write_file", "arguments": "{}"}}],
        })

        make_manager("crash3")  # first resume: repairs it
        third = make_manager("crash3")  # second resume: must not double-repair
        tool_msgs = [m for m in third.history if m.get("role") == "tool"]
        assert len(tool_msgs) == 1

    def test_no_repair_needed_when_last_turn_completed_normally(self, make_manager):
        ssm = make_manager("clean")
        ssm.add_message("user", "goal")
        _append(ssm, {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "write_file", "arguments": "{}"}}],
        })
        _append(ssm, {"role": "tool", "tool_call_id": "call_1", "name": "write_file", "content": "Success"})
        ssm.add_message("assistant", "all done")

        resumed = make_manager("clean")
        assert [m.get("content") for m in resumed.history] == [
            "goal", "", "Success", "all done"
        ]

    def test_no_repair_when_history_has_no_assistant_message(self, make_manager):
        ssm = make_manager("nomessages")
        ssm.add_message("user", "goal")

        resumed = make_manager("nomessages")
        assert [m.get("content") for m in resumed.history] == ["goal"]
