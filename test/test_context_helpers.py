"""Context-window helpers: token estimation, text elision, and history compression."""


# ---------------------------------------------------------------------------
# _estimate_tokens
# ---------------------------------------------------------------------------

def test_estimate_tokens_is_non_negative_int_for_empty_history(manager):
    from JFI.session.simple_session_manager import _estimate_tokens

    tokens = _estimate_tokens([])
    assert isinstance(tokens, int)
    assert tokens >= 0


def test_estimate_tokens_scales_with_message_length(make_manager):
    from JFI.session.simple_session_manager import _estimate_tokens

    short = [{"role": "user", "content": "hi"}]
    long_ = [dict(m, content="x" * 4000) for m in short]

    assert _estimate_tokens(long_) > _estimate_tokens(short)


def test_estimate_tokens_counts_tool_call_payloads(make_manager):
    from JFI.session.simple_session_manager import _estimate_tokens

    base = [{"role": "assistant", "content": ""}]
    with_calls = [dict(base, tool_calls=[{
        "function": {"name": "write_file", "arguments": '{"file_path": "a.py"}' * 20}
    }])]

    assert _estimate_tokens(with_calls) > _estimate_tokens(base)


# ---------------------------------------------------------------------------
# _elide
# ---------------------------------------------------------------------------

def test_elide_returns_short_text_unchanged():
    from JFI.session.simple_session_manager import _elide

    text = "abc"
    assert _elide(text, head=50, tail=50) == text


def test_elide_keeps_head_and_tail_for_long_text():
    from JFI.session.simple_session_manager import _elide

    head_chars = "H" * 200
    tail_chars = "T" * 100
    middle = "M" * 700
    text = head_chars + middle + tail_chars

    result = _elide(text, head=200, tail=100)

    assert result.startswith(head_chars)
    assert result.endswith(tail_chars)
    assert "elided" in result


def test_elide_reports_dropped_character_count():
    from JFI.session.simple_session_manager import _elide

    text = "a" * 300 + "b" * 200
    result = _elide(text, head=100, tail=50)

    dropped = len(text) - 150
    assert f"[{dropped} characters elided]" in result


# ---------------------------------------------------------------------------
# compress_history
# ---------------------------------------------------------------------------

def test_compress_history_is_noop_when_within_budget(manager):
    manager.add_message("user", "goal")
    manager.add_message("assistant", "ok")

    view = manager.compress_history(reserve=0)

    assert view == manager.history


def test_compress_history_shrinks_estimate_but_keeps_transcript_on_disk(make_manager, console):
    from JFI.session.simple_session_manager import _estimate_tokens

    ssm = make_manager("big")
    # A goal block plus many tool-heavy middle blocks and a live tail.
    ssm.add_message("user", "build the thing")
    for i in range(12):
        ssm.append_raw({"role": "assistant", "content": "", "tool_calls": [{
            "function": {"name": "write_file", "arguments": '{"file_path": "f.py"}'}
        }]})
        ssm.append_raw({"role": "tool", "content": "wrote file " + "z" * 6000})
    ssm.add_message("user", "continue")

    original_tokens = _estimate_tokens(ssm.history)
    view = ssm.compress_history(reserve=original_tokens // 2)

    assert _estimate_tokens(view) < original_tokens
    # The newest turn survives intact.
    assert view[-1] == {"role": "user", "content": "continue"}
    # The full transcript is still on disk (compression returns a view only).
    resumed = make_manager("big")
    assert len(resumed.history) == len(ssm.history)


def test_compress_history_trims_oversized_plain_text_content(make_manager):
    """Regression: plain assistant/user text used to be immune to every tier.

    Only `role == "tool"` messages and write_file/append_to_file tool_call
    arguments were ever trimmed, so a big plain-text turn (a verbose
    assistant report, a large pasted goal, the "USER FEEDBACK FOR ITERATION"
    block) could keep a session over budget no matter how many tiers ran.
    """
    ssm = make_manager("plain-text-bulk")
    ssm.add_message("user", "goal: build the thing")
    for i in range(8):
        ssm.append_raw({"role": "assistant", "content": "a" * 30000})
        ssm.add_message("user", "looks good, continue")
    ssm.add_message("user", "final live turn")

    from JFI.session.simple_session_manager import _estimate_tokens

    before = _estimate_tokens(ssm.history)
    view = ssm.compress_history(reserve=100)
    after = _estimate_tokens(view)

    assert after < before
    assert after <= ssm.context_budget() - 100
    # The live tail still survives intact.
    assert view[-1] == {"role": "user", "content": "final live turn"}


def test_compress_history_keeps_tool_call_and_result_together(make_manager):
    ssm = make_manager("pairs")
    ssm.add_message("user", "goal")
    for i in range(10):
        ssm.append_raw({"role": "assistant", "content": "", "tool_calls": [{
            "function": {"name": "execute_command", "arguments": '{"command": "ls"}'}
        }]})
        ssm.append_raw({"role": "tool", "content": "output " + "y" * 500})
    ssm.add_message("user", "carry on")

    view = ssm.compress_history(reserve=1)

    # Every tool_calls message that survives is immediately followed by its
    # result — the runner would otherwise hand OpenAI a broken sequence.
    for index, message in enumerate(view):
        if message.get("tool_calls"):
            assert view[index + 1].get("role") == "tool"
