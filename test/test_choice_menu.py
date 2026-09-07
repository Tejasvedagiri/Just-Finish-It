"""get_user_choice: the arrow-key-movable option menu pinned to the status
bar (PromptToolkitConsoleManager) and the free-text fallback it degrades to
for managers that don't override it (AbstractManager's default).

No Application is started for the PT tests — same headless pattern as
test_pt_line_count.py / test_display_tool_call.py: only the manager's public
write API and its render/key-handling methods are exercised directly.
"""

from JFI.manager.abstract_manager import AbstractManager
from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

OPTIONS = [
    ("y", "Yes, run once"),
    ("s", "Yes, save & run"),
    ("a", "Yes, for the rest of this session"),
    ("n", "No, don't run"),
]


def _new_manager() -> PromptToolkitConsoleManager:
    return PromptToolkitConsoleManager(title="choice-menu")


class TestAbstractManagerDefaultFallback:

    class _Console(AbstractManager):
        def __init__(self, answers):
            self.answers = list(answers)
            self.system_messages: list = []

        def safe_get_user_input(self, prompt_label="You", **kwargs):
            return self.answers.pop(0)

        def display_system(self, text):
            self.system_messages.append(text)

        def display_assistant(self, *a, **k): pass
        def display_user(self, *a, **k): pass
        def get_user_input(self, *a, **k): pass
        def print_agent_response(self, *a, **k): pass

    def test_matches_by_key(self):
        console = self._Console(answers=["n"])
        assert console.get_user_choice("Run it?", OPTIONS) == "n"

    def test_matches_by_full_label_case_insensitive(self):
        console = self._Console(answers=["YES, RUN ONCE"])
        assert console.get_user_choice("Run it?", OPTIONS) == "y"

    def test_reprompts_on_unmatched_answer(self):
        console = self._Console(answers=["nah", "a"])
        assert console.get_user_choice("Run it?", OPTIONS) == "a"
        assert any("Please answer" in m for m in console.system_messages)


class TestPromptToolkitChoiceState:

    def test_get_user_choice_returns_queued_answer(self):
        console = _new_manager()
        console._answers.put("s")  # pre-queued: get_user_choice must not block
        assert console.get_user_choice("Run it?", OPTIONS) == "s"
        # Choice state is torn down once answered.
        assert console._choice_options is None
        assert console._awaiting is None

    def test_move_choice_wraps_forward_and_backward(self):
        console = _new_manager()
        console._choice_options = OPTIONS
        console._choice_index = 0

        console._move_choice(-1)
        assert console._choice_index == len(OPTIONS) - 1

        console._move_choice(1)
        assert console._choice_index == 0

    def test_move_choice_noop_without_active_choice(self):
        console = _new_manager()
        console._choice_options = None
        console._move_choice(1)
        assert console._choice_index == 0

    def test_match_choice_key_by_key_or_label(self):
        console = _new_manager()
        console._choice_options = OPTIONS
        assert console._match_choice_key("A") == "a"
        assert console._match_choice_key("no, don't run") == "n"
        assert console._match_choice_key("nonsense") is None

    def test_confirm_choice_uses_typed_text_when_it_matches(self):
        console = _new_manager()
        console._choice_options = OPTIONS
        console._choice_index = 0  # highlighted is "y", but typed text wins
        console._input_buffer.text = "n"

        console._confirm_choice()

        assert console._answers.get_nowait() == "n"
        assert console._input_buffer.text == ""

    def test_confirm_choice_falls_back_to_highlighted_index(self):
        console = _new_manager()
        console._choice_options = OPTIONS
        console._choice_index = 2  # "a"
        console._input_buffer.text = ""

        console._confirm_choice()

        assert console._answers.get_nowait() == "a"

    def test_status_fragments_render_all_labels_with_selection_highlighted(self):
        console = _new_manager()
        console._choice_options = OPTIONS
        console._choice_index = 1

        frags = console._status_fragments()
        text = "".join(t for _, t in frags)
        for _key, label in OPTIONS:
            assert label in text

        selected_style = next(
            style for style, t in frags if OPTIONS[1][1] in t
        )
        unselected_style = next(
            style for style, t in frags if OPTIONS[0][1] in t
        )
        assert selected_style != unselected_style
