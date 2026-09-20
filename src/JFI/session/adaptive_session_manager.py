from typing import Optional

from JFI.session.simple_session_manager import PLAN_FORMAT_RULES, SimpleSessionManager
from JFI.session.task_rules import CORE_PLAN_RULES, TYPE_ADDENDA, detect_task_type

# Written verbatim by get_phase_trigger's planner branch (see
# simple_session_manager.get_phase_trigger) -- the one place this exact
# prefix appears, which is what makes it a reliable marker for "this is
# the goal", not just any user message.
_GOAL_PREFIX = "My goal is:"


class AdaptiveSessionManager(SimpleSessionManager):
    """
    SimpleSessionManager that swaps in task-type-specific planning rules
    (python/javascript/story -- see task_rules.py) instead of one generic
    rule block for every goal, based on a cheap keyword read of the
    session's own goal text. Storage, history, plan-parsing, locking,
    context budgeting -- everything else -- is identical to
    SimpleSessionManager; only which PLAN FILE FORMAT rules get sent to
    the model changes. See task_rules.py's module docstring for the two
    concrete problems this fixes (context cost, missing architecture
    guidance) and the real run that motivated it.

    The task type is detected once (from the first "My goal is: ..."
    message in history -- present for both a fresh session, right after
    the goal prompt is answered, and a resumed one, already persisted from
    before) and cached for the rest of the session; every phase's system
    message uses the same rules, so the plan's own vocabulary stays
    consistent across planner/imp/testing/reviewer turns.
    """

    def __init__(self, console, session_id: str):
        super().__init__(console, session_id)
        self._task_type_cache: Optional[str] = None

    def _goal_text(self) -> str:
        for message in self.history:
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str) and content.startswith(_GOAL_PREFIX):
                # "My goal is: <goal>\n\nWrite the step-by-step plan..." --
                # take everything up to the blank line that separates the
                # goal from the instruction sentence after it.
                return content[len(_GOAL_PREFIX):].split("\n\n", 1)[0].strip()
        return ""

    def task_type(self) -> str:
        """"python", "javascript", "story", or "generic" -- computed once
        from the goal and cached. Public (unlike _plan_format_rules) since
        it's a natural thing for a caller/log/debug tool to want to know
        without reaching into the rules text itself."""
        if self._task_type_cache is None:
            self._task_type_cache = detect_task_type(self._goal_text())
        return self._task_type_cache

    def _plan_format_rules(self) -> str:
        addendum = TYPE_ADDENDA.get(self.task_type())
        if addendum is None:
            # Unmatched goal: fall back to the existing, already-validated
            # generic rules rather than guessing at a new block with no
            # real signal behind it.
            return PLAN_FORMAT_RULES
        return CORE_PLAN_RULES + addendum
