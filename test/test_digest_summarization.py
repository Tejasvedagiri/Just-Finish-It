"""LLM-based digest summarization (Tier 4).

A feature layered on top of the existing compress_history/_digest
machinery, added so aged-out history stops losing all its reasoning:

_build_digest: Tier 4 now LLM-summarizes newly-aged-out blocks into a
running prose summary (persisted via metadata_store, see
JFI.session.metadata_store) instead of only recording tool/file/command
names — but only once enough new material has piled up
(DIGEST_SUMMARY_MIN_CHARS) or on the very first digest, and only when a
phase's LLM stream is actually wired in via set_llm_streams(). No stream,
too little new material, or a failed call all fall back to the original
cheap metadata note, never crashing compression.

Note: this module used to also cover render_facts_for_auto_load, a
mechanism that auto-injected saved context() facts into every phase's
system message. That was intentionally removed in favor of a pure
pull-based model: context_save/context_lookup are ordinary tool calls the
model decides to use, never auto-injected (see JFI.tool.context_tools).
"""
import json

from JFI.session.simple_session_manager import DIGEST_MARKER, DIGEST_SUMMARY_MIN_CHARS


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
    assert not ssm.metadata.get("digest_summary")


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


def test_digest_summary_prompt_favors_completeness_over_brevity(make_manager):
    """The summarizer's own system prompt used to cap it at 'well under 200
    words' -- too lossy for a long session's worth of aged-out history, per
    live use. It now explicitly trades brevity for completeness instead."""
    ssm = make_manager("digest-completeness-wording")
    ssm.add_message("user", "build the thing")
    _fill_middle(ssm, 12)

    llm = FakeLLMStream(reply="Learned X, decided Y.")
    ssm.set_llm_streams({"imp": llm})

    ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")

    system_prompt = llm.calls[0][0]["content"]
    assert "200 words" not in system_prompt
    assert "Favor completeness" in system_prompt


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
    assert not ssm.metadata.get("digest_summary")
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


# ---------------------------------------------------------------------------
# _build_digest summarizes from PRE-Tier-2/3 content (compress_history's
# pretrim_blocks) -- a fact buried past TOOL_RESULT_HEAD (600 chars) into a
# long tool result must still reach the summarizer, not just the ~600+400
# head/tail scraps Tier 2 leaves for the live model request. See
# compress_history's pretrim_blocks comment for the run.log evidence this
# guards against: a fact established via execute_command getting compressed
# away before the digest ever saw it, forcing the agent to re-run the same
# diagnostic command turns later with no memory of the answer.
# ---------------------------------------------------------------------------

def test_digest_summarization_sees_tool_result_text_past_the_elision_window(make_manager):
    ssm = make_manager("deep-fact")
    ssm.add_message("user", "build the thing")
    _fill_middle(ssm, 12)
    llm = FakeLLMStream(reply="v1")
    ssm.set_llm_streams({"imp": llm})
    ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")
    assert len(llm.calls) == 1

    # A single tool result long enough that Tier 2's head(600)/tail(400)
    # elision would cut this marker out entirely if it ran before the digest
    # summarizer saw the content -- it sits well past character 600 and well
    # before the last 400 characters.
    marker = "DISCOVERED_FACT_db_path_is_slash_data_slash_stockmcp_db"
    long_result = ("a" * 700) + marker + ("b" * 700)
    ssm.append_raw({"role": "assistant", "content": "", "tool_calls": [{
        "function": {"name": "execute_command", "arguments": json.dumps({"command": "probe db path"})}
    }]})
    ssm.append_raw({"role": "tool", "content": long_result})

    # Push enough additional bulky blocks (and past KEEP_RECENT_BLOCKS more
    # blocks) to both cross DIGEST_SUMMARY_MIN_CHARS and age the marker block
    # out of the protected recent tail, so it actually reaches the summarizer
    # instead of just riding along untouched in the live view.
    for i in range(20):
        ssm.append_raw({"role": "assistant", "content": "", "tool_calls": [{
            "function": {"name": "execute_command", "arguments": json.dumps({"command": f"big {i}"})}
        }]})
        ssm.append_raw({"role": "tool", "content": "y" * (DIGEST_SUMMARY_MIN_CHARS // 10)})
    ssm.add_message("user", "keep going")
    llm.reply = "v2, learned the db path"

    ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")

    assert len(llm.calls) == 2
    sent_text = json.dumps(llm.calls[1])
    assert marker in sent_text


class _MultiChunkLLMStream:
    """Like FakeLLMStream, but replies with several separate streamed
    chunks instead of one -- needed to actually exercise live,
    chunk-by-chunk display rather than a single-chunk reply that would
    look identical whether streamed live or dumped all at once."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = []

    def send_message(self, messages):
        self.calls.append(messages)
        return [FakeChunk(c) for c in self.chunks]


class _StreamingConsole:
    """Fuller console fake than conftest's Console -- separates
    display_stream (live, chunk-by-chunk) from log_stream_result
    (persisted once, not re-rendered) and display_system/display_rule (the
    PROMPT SENT dump), so a test can assert the digest response streams
    live instead of arriving as one post-hoc block, and is never shown
    twice."""

    def __init__(self):
        self.system_messages: list[str] = []
        self.rule_labels: list[str] = []
        self.stream_chunks: list[str] = []
        self.logged: list[tuple[str, str]] = []

    def display_system(self, text):
        self.system_messages.append(str(text))

    def display_rule(self, label=""):
        self.rule_labels.append(label)

    def display_stream(self, text):
        self.stream_chunks.append(text)

    def log_stream_result(self, tag, text):
        self.logged.append((tag, text))


def test_digest_streams_live_under_show_stream_prompts_without_double_printing(monkeypatch):
    """SHOW_STREAM_PROMPTS=1 must show the digest response as it's
    generated (display_stream, chunk by chunk -- the same "live" feel as
    the main turn's own streamed response) rather than only as a single
    block dumped once the whole call returns, and must persist the
    assembled text exactly once (log_stream_result) rather than also
    re-printing it via display_system, which would show it twice on
    screen."""
    from JFI.session.simple_session_manager import SimpleSessionManager

    monkeypatch.setenv("SHOW_STREAM_PROMPTS", "1")
    console = _StreamingConsole()
    ssm = SimpleSessionManager(console, "digest-live-stream")
    ssm.add_message("user", "build the thing")
    _fill_middle(ssm, 12)

    llm = _MultiChunkLLMStream(["Learned ", "X, ", "decided Y."])
    ssm.set_llm_streams({"imp": llm})

    ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")

    # Streamed live, chunk by chunk, in the exact pieces the LLM sent.
    assert console.stream_chunks == ["Learned ", "X, ", "decided Y."]
    # Persisted exactly once, as the fully assembled text.
    assert console.logged == [("DIGEST", "Learned X, decided Y.")]
    # Never re-printed as a second whole-text dump via display_system.
    assert not any("Learned X, decided Y." in m for m in console.system_messages)


class _ChunkWithReasoning:
    def __init__(self, content=None, reasoning_content=None):
        delta = type("Delta", (), {"content": content, "reasoning_content": reasoning_content})()
        self.choices = [type("Choice", (), {"delta": delta})()]


class _ReasoningThenContentLLM:
    """A reasoning model (e.g. local qwen3) that streams its whole
    chain-of-thought via reasoning_content chunks before any real content
    chunk -- the shape that used to show NOTHING on screen for the digest
    call, looking identical to "stuck" even while LM Studio was actively
    generating."""

    def __init__(self, reasoning_chunks=(), content_chunks=()):
        self.reasoning_chunks = list(reasoning_chunks)
        self.content_chunks = list(content_chunks)
        self.calls = []

    def send_message(self, messages):
        self.calls.append(messages)
        return (
            [_ChunkWithReasoning(reasoning_content=r) for r in self.reasoning_chunks]
            + [_ChunkWithReasoning(content=c) for c in self.content_chunks]
        )


def test_digest_default_caps_are_tighter_than_a_normal_turns(monkeypatch):
    """Deliberately NOT the same defaults as STREAM_OUTPUT_CAP/
    REASONING_OUTPUT_CAP (10000/3000) -- this call's own system prompt
    only ever asks for "up to roughly 500 words" of plain summary, so it
    has no legitimate need for anywhere near a real coding turn's budget.
    A tighter default means a runaway reasoning pass on a slow local model
    fails fast into the cheap fallback instead of stalling the whole
    pipeline for a long time on every single digest."""
    from JFI.session.simple_session_manager import SimpleSessionManager

    console = _StreamingConsole()
    ssm = SimpleSessionManager(console, "digest-default-caps")
    ssm.add_message("user", "build the thing")
    _fill_middle(ssm, 12)

    # No content ever arrives -- just enough reasoning to cross the DEFAULT
    # digest reasoning cap (1500 tokens ~= 6000 chars) but nowhere near the
    # main turn's default (3000 tokens ~= 12000 chars).
    llm = _ReasoningThenContentLLM(reasoning_chunks=["x" * 6100])
    ssm.set_llm_streams({"imp": llm})

    view = ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")

    digest = next(m for m in view if DIGEST_MARKER in str(m.get("content", "")))
    assert "Tool calls: execute_command" in digest["content"]  # fell back, didn't hang


def test_digest_streams_reasoning_live_before_content(monkeypatch):
    """A reasoning model's chain-of-thought must stream live too, not just
    the final content -- otherwise a long "thinking" pass before any real
    content looks identical to a hung side-channel call."""
    from JFI.session.simple_session_manager import SimpleSessionManager

    monkeypatch.setenv("SHOW_STREAM_PROMPTS", "1")
    console = _StreamingConsole()
    ssm = SimpleSessionManager(console, "digest-reasoning")
    ssm.add_message("user", "build the thing")
    _fill_middle(ssm, 12)

    llm = _ReasoningThenContentLLM(
        reasoning_chunks=["Thinking ", "it over..."],
        content_chunks=["Learned ", "X."],
    )
    ssm.set_llm_streams({"imp": llm})

    ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")

    # Reasoning chunks streamed live too, in order, ahead of the content.
    assert console.stream_chunks == ["Thinking ", "it over...", "Learned ", "X."]
    assert any("reasoning" in label.lower() for label in console.rule_labels)
    assert console.logged == [("DIGEST", "Learned X.")]


def test_digest_aborts_runaway_reasoning_instead_of_hanging_forever(monkeypatch):
    """No cap at all on this side-channel call used to mean a runaway local
    model (e.g. a reasoning model that never stops "thinking") could block
    compression -- and the whole pipeline -- indefinitely. It must abort
    and fall back to the cheap metadata note instead, same contract as any
    other digest failure."""
    from JFI.session.simple_session_manager import SimpleSessionManager

    monkeypatch.setenv("DIGEST_REASONING_OUTPUT_CAP", "10")  # tiny, to trip quickly
    console = _StreamingConsole()
    ssm = SimpleSessionManager(console, "digest-runaway-reasoning")
    ssm.add_message("user", "build the thing")
    _fill_middle(ssm, 12)

    # Far more reasoning than the 10-token (40-char) cap allows, and no
    # content at all -- a runaway "thinking" model that never answers.
    llm = _ReasoningThenContentLLM(reasoning_chunks=["thinking a very great deal about this "] * 20)
    ssm.set_llm_streams({"imp": llm})

    view = ssm.compress_history(reserve=ssm.context_budget() - 50, phase="imp")

    # Falls back to the cheap metadata note rather than hanging or crashing.
    digest = next(m for m in view if DIGEST_MARKER in str(m.get("content", "")))
    assert "Tool calls: execute_command" in digest["content"]
    assert not ssm.metadata.get("digest_summary")
    assert any("Digest summarization failed" in m for m in console.system_messages)


def test_keep_recent_blocks_raised_for_less_aggressive_recent_trimming():
    """Raised from 6 -> 10 after live use showed genuinely RECENT turns (not
    just history old enough to digest) getting trimmed/elided more often
    than felt right under a tight CONTEXT_SIZE -- see the constant's own
    comment in simple_session_manager.py for the full rationale."""
    from JFI.session.simple_session_manager import KEEP_RECENT_BLOCKS

    assert KEEP_RECENT_BLOCKS >= 10
