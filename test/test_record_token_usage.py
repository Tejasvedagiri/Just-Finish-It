"""record_token_usage: lets usage from outside a phase's own turn (ask_llm's
side call) still count toward the header's cumulative ↓/↑ totals, instead
of silently not showing up there."""

from JFI.manager.pt_console_manager import PromptToolkitConsoleManager


def test_record_token_usage_adds_to_the_header_counters():
    manager = PromptToolkitConsoleManager()
    manager.record_token_usage(prompt_tokens=10, completion_tokens=5)
    assert manager._tokens_read == 10
    assert manager._tokens_written == 5


def test_record_token_usage_accumulates_across_calls():
    manager = PromptToolkitConsoleManager()
    manager.record_token_usage(prompt_tokens=10, completion_tokens=5)
    manager.record_token_usage(prompt_tokens=3, completion_tokens=2)
    assert manager._tokens_read == 13
    assert manager._tokens_written == 7


def test_record_token_usage_appears_in_the_header_fragments():
    manager = PromptToolkitConsoleManager()
    manager.record_token_usage(prompt_tokens=1000, completion_tokens=2000)
    header = "".join(text for _, text in manager._header_fragments())
    assert "1.0k" in header  # _fmt_tokens(1000)
    assert "2.0k" in header  # _fmt_tokens(2000)


def test_default_arguments_are_a_no_op():
    manager = PromptToolkitConsoleManager()
    manager.record_token_usage()
    assert manager._tokens_read == 0
    assert manager._tokens_written == 0
