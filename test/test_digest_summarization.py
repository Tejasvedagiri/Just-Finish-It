"""LLM-based digest summarization (Tier 4) and context-cache auto-loading.

Two features layered on top of the existing compress_history/_digest
machinery, added so aged-out history stops losing all its reasoning and
saved facts stop depending on the model remembering to call context_lookup:

1. _build_digest: Tier 4 now LLM-summarizes newly-aged-out blocks into a
   running prose summary (persisted in metadata.json) instead of only
   recording tool/file/command names — but only once enough new material has
   piled up (DIGEST_SUMMARY_MIN_CHARS) or on the very first digest, and only
   when a phase's LLM stream is actually wired in via set_llm_streams(). No
   stream, too little new material, or a failed call all fall back to the
   original cheap metadata note, never crashing compression.

2. render_facts_for_auto_load / _phase_system_message: whatever's been
   context_save()'d is now folded into every phase's system message
   automatically, since that's rebuilt fresh every request and never
   touched by compress_history — a fact no longer depends on the model
   proactively calling context_lookup at the right moment.
"""
import json

from JFI.session.simple_session_manager import DIGEST_MARKER, DIGEST_SUMMARY_MIN_CHARS
from JFI.tool.context_tools import render_facts_for_auto_load


class FakeChunk:
    def __init__(self, content):
        self.choices = [type("Choice", (), {"delta": type("Delta", (), {"content": content})()})]


class FakeLLMStream:
    """Minimal BaseLLMStream stand-in: records every call, replies with a
    fixed string (as one streamed chunk), or raises if configured to."""

    def __init__(self, reply="condensed summary", error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def send_message(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return [FakeChunk(self.reply)]


def _big_tool_block(i):
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "function": {"name": "execute_command", "arguments": json.dumps({"command": f"cmd {i}"})}
        }]},
        {"role": "tool", "content": f"output {i} " + "z" * 400},
    ]


def _fill_middle(ssm, n):
    """Enough blocks between the goal and the live tail to force Tier 4."""
    for i in range(n):
        for msg in _big_tool_block(i):
            ssm.append_raw(msg)
    ssm.add_message("user", "still going")


# ---------------------------------------------------------------------------
# render_facts_for_auto_load
# ---------------------------------------------------------------------------

def test_render_facts_for_auto_load_empty_cache_returns_empty_string(tmp_path):
    cache_path = str(tmp_path / "context.json")
    assert render_facts_for_auto_load(cache_path) == ""


def test_render_facts_for_auto_load_formats_saved_facts(tmp_path):
    from JFI.tool.context_tools import context_save

    cache_path = str(tmp_path / "context.json")
    context_save("server_pid", "12345", cache_path)
    context_save("api_base", "http://127.0.0.1:3334", cache_path)

    rendered = render_facts_for_auto_load(cache_path)

    assert "server_pid: 12345" in rendered
    assert "api_base: http://127.0.0.1:3334" in rendered


def test_render_facts_for_auto_load_caps_an_oversized_value(tmp_path):
    from JFI.tool.context_tools import context_save

    cache_path = str(tmp_path / "context.json")
    context_save("huge", "x" * 5000, cache_path)

    rendered = render_facts_for_auto_load(cache_path)

    assert "[truncated]" in rendered
    assert len(rendered) < 2000


def test_render_facts_for_auto_load_excludes_internal_keys(tmp_path):
    from JFI.tool.context_tools import save_context_cache

    cache_path = str(tmp_path / "context.json")
    save_context_cache({"approved-cmd": "rm -rf /tmp/x", "real_fact": "keep me"}, cache_path)

    rendered = render_facts_for_auto_load(cache_path)

    assert "real_fact" in rendered
    assert "approved-cmd" not in rendered
    assert "rm -rf" not in rendered


# ---------------------------------------------------------------------------
# _phase_system_message auto-loads saved facts
# ---------------------------------------------------------------------------

def test_system_message_includes_saved_context_facts(manager):
    manager.add_message("user", "goal")
    from JFI.tool.context_tools import context_save

    context_save("db_schema", "users(id, email)", manager.context_cache_path)

    system_msg = manager.get_messages("imp")[0]["content"]

    assert "SAVED CONTEXT" in system_msg
    assert "db_schema: users(id, email)" in system_msg


def test_system_message_omits_saved_context_section_when_cache_empty(manager):
    manager.add_message("user", "goal")

    system_msg = manager.get_messages("imp")[0]["content"]

    assert "SAVED CONTEXT" not in system_msg


# ---------------------------------------------------------------------------
# _build_digest: no LLM stream wired in -> identical cheap-digest behavior
# ---------------------------------------------------------------------------

def test_build_digest_falls_back_to_metadata_note_without_llm_streams(make_manager):
    ssm = make_manager("no-llm")
    ssm.add_message("user", "build the thing")
    _fill_middle(ssm, 12)

    view = ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")

    digest = next(m for m in view if DIGEST_MARKER in str(m.get("content", "")))
    assert "Tool calls: execute_command" in digest["content"]
    # No LLM was ever wired in, so nothing should have been persisted.
    assert "digest_summary" not in ssm.metadata


# ---------------------------------------------------------------------------
# _build_digest: LLM stream wired in
# ---------------------------------------------------------------------------

def test_build_digest_uses_llm_summary_on_first_digest_regardless_of_size(make_manager):
    """The very first digest of a session gets a real summary even if the
    aged-out batch is small — otherwise a short session never benefits."""
    ssm = make_manager("first-digest")
    ssm.add_message("user", "build the thing")
    _fill_middle(ssm, 12)

    llm = FakeLLMStream(reply="Learned X, decided Y.")
    ssm.set_llm_streams({"imp": llm})

    view = ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")

    digest = next(m for m in view if DIGEST_MARKER in str(m.get("content", "")))
    assert "Learned X, decided Y." in digest["content"]
    assert len(llm.calls) == 1
    assert ssm.metadata["digest_summary"] == "Learned X, decided Y."
    assert ssm.metadata["digest_block_count"] > 0


def test_build_digest_persists_across_a_resumed_session(make_manager):
    ssm = make_manager("resume-me")
    ssm.add_message("user", "build the thing")
    _fill_middle(ssm, 12)
    llm = FakeLLMStream(reply="First summary.")
    ssm.set_llm_streams({"imp": llm})
    ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")

    resumed = make_manager("resume-me")

    assert resumed.metadata["digest_summary"] == "First summary."
    assert resumed.metadata["digest_block_count"] == ssm.metadata["digest_block_count"]


def test_build_digest_skips_llm_call_below_threshold_once_a_summary_exists(make_manager):
    """After an initial summary exists, a small newly-aged-out batch (well
    under DIGEST_SUMMARY_MIN_CHARS) should NOT trigger another LLM call —
    it should ride along as an unsummarized note until enough piles up."""
    ssm = make_manager("throttled")
    ssm.add_message("user", "build the thing")
    _fill_middle(ssm, 12)
    llm = FakeLLMStream(reply="Initial summary.")
    ssm.set_llm_streams({"imp": llm})
    ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")
    assert len(llm.calls) == 1

    # One more small turn ages out one more (tiny) block.
    ssm.append_raw({"role": "assistant", "content": "", "tool_calls": [{
        "function": {"name": "read_file", "arguments": '{"file_path": "x.py"}'}
    }]})
    ssm.append_raw({"role": "tool", "content": "tiny"})
    ssm.add_message("user", "keep going")

    view = ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")

    # No second LLM call yet — still under the summarization threshold.
    assert len(llm.calls) == 1
    digest = next(m for m in view if DIGEST_MARKER in str(m.get("content", "")))
    assert "Initial summary." in digest["content"]
    assert "not yet condensed" in digest["content"].lower()


def test_build_digest_summarizes_once_enough_new_material_accumulates(make_manager):
    ssm = make_manager("accumulate")
    ssm.add_message("user", "build the thing")
    _fill_middle(ssm, 12)
    llm = FakeLLMStream(reply="v1")
    ssm.set_llm_streams({"imp": llm})
    ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")
    assert len(llm.calls) == 1

    # Push enough additional bulky blocks to cross DIGEST_SUMMARY_MIN_CHARS.
    for i in range(20):
        ssm.append_raw({"role": "assistant", "content": "", "tool_calls": [{
            "function": {"name": "execute_command", "arguments": json.dumps({"command": f"big {i}"})}
        }]})
        ssm.append_raw({"role": "tool", "content": "y" * (DIGEST_SUMMARY_MIN_CHARS // 10)})
    ssm.add_message("user", "final")
    llm.reply = "v2, folded in the new stuff"

    view = ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")

    assert len(llm.calls) == 2
    # The prompt for the second call carries the prior summary forward.
    second_call_text = json.dumps(llm.calls[1])
    assert "v1" in second_call_text
    digest = next(m for m in view if DIGEST_MARKER in str(m.get("content", "")))
    assert "v2, folded in the new stuff" in digest["content"]
    assert ssm.metadata["digest_summary"] == "v2, folded in the new stuff"


def test_build_digest_falls_back_when_llm_call_raises(make_manager):
    ssm = make_manager("llm-fails")
    ssm.add_message("user", "build the thing")
    _fill_middle(ssm, 12)
    llm = FakeLLMStream(error=RuntimeError("connection reset"))
    ssm.set_llm_streams({"imp": llm})

    view = ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")

    # Never crashes; falls back to the cheap metadata note.
    digest = next(m for m in view if DIGEST_MARKER in str(m.get("content", "")))
    assert "Tool calls: execute_command" in digest["content"]
    assert "digest_summary" not in ssm.metadata
    assert any("Digest summarization failed" in msg for msg in ssm.console.system_messages)


def test_set_llm_streams_is_optional(make_manager):
    """A SessionManager that never has set_llm_streams called on it (e.g.
    an older caller, or a future implementation that skips it) must behave
    exactly like before this feature existed — no crash, cheap digest only."""
    ssm = make_manager("never-wired")
    ssm.add_message("user", "build the thing")
    _fill_middle(ssm, 12)

    view = ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")

    digest = next(m for m in view if DIGEST_MARKER in str(m.get("content", "")))
    assert "Tool calls:" in digest["content"]
