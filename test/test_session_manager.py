"""Session lifecycle tests: folder creation, resume, the queued-requests
round-trip, and history persistence across manager instances."""


def test_constructor_creates_jfi_session_folder(manager):
    from pathlib import Path

    assert manager.session_path.is_dir()
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
    """Regression: the old history file was rewritten in full on every
    message, so a turn's save cost grew with the whole session's size. save_history()
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
