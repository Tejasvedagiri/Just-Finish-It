"""Session lifecycle tests: folder creation, resume, metadata round-trip,
file tracking, and history persistence across manager instances."""

import json


def test_constructor_creates_jfi_session_folder(manager):
    from pathlib import Path

    assert (manager.session_path / "history.jsonl.gz").parent.is_dir()
    assert manager.session_path.name == "demo"
    parent = manager.session_path.parent
    assert parent.name == ".JFI"
    # The on-disk folder is inside the test cwd.
    assert (Path.cwd() / ".JFI" / "demo").is_dir()


def test_constructor_normalises_session_id(make_manager):
    ssm = make_manager("My Demo")
    assert ssm.session_id == "my_demo"
    assert ssm.session_path.parent.name == ".JFI"


def test_resume_flag_reflects_existing_history(make_manager, manager):
    # First construction: fresh session.
    assert not manager.is_resuming

    # Add a message (which persists the pickle), then rebuild on the same id.
    manager.add_message("user", "hello")
    resumed = make_manager(manager.session_id)
    assert resumed.is_resuming
    assert len(resumed.history) == 1


def test_metadata_round_trip(make_manager):
    first = make_manager("meta")
    assert first.metadata == {"implemented_files": []}

    first.track_file("src/a.py")
    first.save_metadata()

    second = make_manager("meta")
    loaded = second.load_metadata()
    assert "src/a.py" in loaded["implemented_files"]
    # metadata.json is plain JSON on disk, next to the history pickle.
    raw = json.loads(second.metadata_path.read_text(encoding="utf-8"))
    assert raw["implemented_files"] == ["src/a.py"]


def test_track_file_dedupes_and_persists(make_manager):
    ssm = make_manager("track")
    ssm.track_file("x.py")
    ssm.track_file("y.py")
    ssm.track_file("x.py")  # duplicate must not be appended again
    assert ssm.metadata["implemented_files"] == ["x.py", "y.py"]

    reloaded = make_manager("track").load_metadata()
    assert reloaded["implemented_files"] == ["x.py", "y.py"]


def test_track_file_empty_path_is_ignored(make_manager):
    ssm = make_manager("empty")
    ssm.track_file("")
    assert ssm.metadata["implemented_files"] == []


def test_get_project_state_summary_lists_tracked_files(make_manager):
    ssm = make_manager("summary")
    assert "No files have been tracked yet." in ssm.get_project_state_summary()

    ssm.track_file("src/b.py")
    summary = ssm.get_project_state_summary()
    assert "- src/b.py" in summary
    assert "CURRENT PROJECT FILES:" in summary


def test_history_persistence_across_instances(make_manager):
    first = make_manager("hist")
    first.add_message("user", "turn one")
    first.append_raw({"role": "assistant", "content": "turn two"})

    second = make_manager("hist")
    assert [m["content"] for m in second.history] == ["turn one", "turn two"]


def test_save_history_appends_instead_of_rewriting(make_manager, monkeypatch):
    """Regression: history.pkl used to be rewritten in full on every message,
    so a turn's save cost grew with the whole session's size. save_history()
    must only ever hand the newly added messages to the append helper."""
    import JFI.session.simple_session_manager as ssm_module

    seen_batches = []
    original = ssm_module._append_jsonl_gz

    def spy(path, messages):
        seen_batches.append(list(messages))
        return original(path, messages)

    monkeypatch.setattr(ssm_module, "_append_jsonl_gz", spy)

    ssm = make_manager("append-only")
    ssm.add_message("user", "one")
    ssm.add_message("user", "two")
    ssm.append_raw({"role": "assistant", "content": "three"})

    assert [b[0]["content"] for b in seen_batches] == ["one", "two", "three"]
    # No call ever saw more than the single new message that triggered it.
    assert all(len(b) == 1 for b in seen_batches)


def test_history_survives_a_truncated_final_write(make_manager):
    """Regression: a crash mid-save used to risk the *entire* history file —
    gzip.GzipFile.read() decodes however many members it needs to satisfy a
    request, and raises without returning anything if that walk reaches a
    truncated final member, even when earlier members are perfectly intact.
    Only the in-flight message should be lost, never anything before it."""
    ssm = make_manager("crashy")
    for i in range(5):
        ssm.add_message("user", f"message {i}")

    size_before_last = ssm.history_path.stat().st_size
    ssm.add_message("user", "this one gets corrupted")
    full_size = ssm.history_path.stat().st_size

    truncated_at = size_before_last + (full_size - size_before_last) // 2
    with open(ssm.history_path, "r+b") as f:
        f.truncate(truncated_at)

    resumed = make_manager("crashy")
    assert [m["content"] for m in resumed.history] == [f"message {i}" for i in range(5)]


def test_legacy_pickle_history_is_migrated_on_load(make_manager):
    """A pre-existing history.pkl (the old full-rewrite format) must still
    load, and gets written out under the new append-only format immediately
    so every save from then on appends instead of resurrecting the pickle."""
    import pickle

    ssm = make_manager("legacy")
    legacy_path = ssm.session_path / "history.pkl"
    with open(legacy_path, "wb") as f:
        pickle.dump([{"role": "user", "content": "old goal"},
                     {"role": "assistant", "content": "old reply"}], f)

    migrated = make_manager("legacy")
    assert migrated.is_resuming
    assert [m["content"] for m in migrated.history] == ["old goal", "old reply"]
    assert migrated.history_path.exists()

    # A save after migration appends cleanly, and a fresh load sees it all.
    migrated.add_message("user", "new turn after migration")
    reloaded = make_manager("legacy")
    assert [m["content"] for m in reloaded.history] == [
        "old goal", "old reply", "new turn after migration"
    ]


def test_add_messages_appends_batch(make_manager):
    ssm = make_manager("batch")
    ssm.add_messages([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    assert len(ssm.history) == 2

    reloaded = make_manager("batch")
    assert [m["content"] for m in reloaded.history] == ["a", "b"]


def test_get_messages_returns_system_plus_history(manager):
    manager.add_message("user", "hello there")
    messages = manager.get_messages("imp")
    assert messages[0]["role"] == "system"
    assert "Implementation Agent" in messages[0]["content"]
    # The user turn is carried through.
    contents = [m.get("content") for m in messages]
    assert "hello there" in contents


def test_compress_history_keeps_plan_agent_instructions(manager):
    from JFI.session.simple_session_manager import _estimate_tokens

    manager.add_message(
        "system",
        f"You are the Plan Agent. Your single task: write a step-by-step plan to "
        f"{manager.plan_path} now, using the mandated '- [ ]' format.",
    )
    for i in range(12):
        manager.add_message("user", f"question number {i}")
        manager.add_message(
            "assistant", ("answer body " * 80) + f"part {i}"
        )

    # A reserve bigger than the context budget pushes the effective cap to its
    # floor (512 tokens), which forces compression of the middle blocks. The
    # manager's default budget is CONTEXT_SIZE * ratio, so use the actual one.
    total = _estimate_tokens(manager.history)
    compressed = manager.compress_history(reserve=total + manager.context_budget() + 1024)

    assert _estimate_tokens(compressed) < total
    system_texts = [m.get("content") or "" for m in compressed if m["role"] == "system"]
    # The plan agent's instructions must survive compression verbatim.
    original = next(
        m["content"] for m in manager.history
        if m["role"] == "system" and "Plan Agent" in str(m.get("content") or "")
    )
    assert any(original in text for text in system_texts)


def test_compress_history_keeps_goal_user_message(manager):
    from JFI.session.simple_session_manager import _estimate_tokens

    goal = "My goal is: add a status bar that shows plan progress."
    manager.add_message("user", goal)
    for i in range(6):
        manager.add_message("assistant", ("filler response body " * 40) + f" {i}")

    budget = _estimate_tokens(manager.history) // 2
    compressed = manager.compress_history(reserve=budget)

    flat = [m.get("content") or "" for m in compressed]
    assert any(goal in text for text in flat), (
        "the goal message must survive compression verbatim"
    )


def test_compress_history_keeps_nudges(manager):
    from JFI.session.simple_session_manager import _estimate_tokens

    manager.add_message("user", "My goal is: do the thing.")
    for i in range(6):
        manager.add_message("assistant", ("filler answer text " * 40) + f" {i}")
    # Match the marker phrase that _is_nudge looks for, verbatim and lowercase.
    nudge = (
        "Continue working. when you are entirely finished with this phase, stop "
        "and say PHASE_COMPLETE."
    )
    manager.add_message("user", nudge)

    budget = _estimate_tokens(manager.history) // 2
    compressed = manager.compress_history(reserve=budget)

    flat = [m.get("content") or "" for m in compressed]
    assert any("entirely finished with this phase" in text for text in flat), (
        "the nudge must survive compression verbatim"
    )
