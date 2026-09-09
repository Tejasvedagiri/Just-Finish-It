"""SHOW_STREAM_PROMPTS=1: dumps every message sent to the LLM each turn, in
full — deliberately untruncated, unlike display_tool_result's 8-line/width-
capped preview elsewhere in the console."""

import pytest

from JFI.runner import _show_stream_prompts, dump_prompt


class _RecordingConsole:
    def __init__(self):
        self.rules: list[str] = []
        self.lines: list[str] = []

    def display_rule(self, label=""):
        self.rules.append(label)

    def display_system(self, text):
        self.lines.append(text)


@pytest.mark.parametrize("value, expected", [
    (None, False),
    ("", False),
    ("0", False),
    ("false", False),
    ("False", False),
    ("no", False),
    ("1", True),
    ("true", True),
    ("True", True),
    ("yes", True),
    ("on", True),
])
def test_show_stream_prompts_parses_truthy_values(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("SHOW_STREAM_PROMPTS", raising=False)
    else:
        monkeypatch.setenv("SHOW_STREAM_PROMPTS", value)
    assert _show_stream_prompts() is expected


def test_dump_prompt_never_truncates_long_content():
    console = _RecordingConsole()
    big = "x" * 5000
    dump_prompt(console, "imp", [{"role": "system", "content": big}])
    assert big in console.lines


def test_dump_prompt_shows_role_and_tool_call_names():
    console = _RecordingConsole()
    messages = [
        {"role": "user", "content": "Do the thing."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "1", "function": {"name": "write_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "1", "name": "write_file", "content": "Success"},
    ]
    dump_prompt(console, "imp", messages)

    joined = "\n".join(console.lines)
    assert "user" in joined
    assert "tool_calls: write_file" in joined
    assert "tool_call_id: 1" in joined
    assert "Do the thing." in joined
    assert "Success" in joined


def test_dump_prompt_shows_multimodal_text_without_dumping_image_data():
    console = _RecordingConsole()
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "see image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }]
    dump_prompt(console, "imp", messages)

    joined = "\n".join(console.lines)
    assert "see image" in joined
    assert "(image attached)" in joined
    assert "base64" not in joined  # never dump the raw payload


def test_dump_prompt_brackets_output_with_start_and_end_rules():
    console = _RecordingConsole()
    dump_prompt(console, "reviewer", [{"role": "user", "content": "hi"}])
    # phase_display_name("reviewer") -> "Review", so the header reads "REVIEW".
    assert any("PROMPT SENT" in r and "REVIEW" in r for r in console.rules)
    assert console.rules[-1] == "END PROMPT"
