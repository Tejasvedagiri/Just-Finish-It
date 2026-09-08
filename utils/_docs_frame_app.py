"""Standalone harness that renders one representative JFI console frame.

Not a live session — never talks to an LLM. Exists so
capture_theme_screenshots.py --docs can capture REAL prompt_toolkit output
(genuine style/color resolution, real bold-glyph rendering, real background
fill of padding rows) exercising every message-role style — user, assistant
tag+body, system, tool call+result, error — instead of just the idle "enter
a session name" screen a full `python -m JFI.runner` run blocks on before
any LLM response ever arrives.

Run directly (used as a subprocess target, not imported):
    THEME=dark-ocean python utils/_docs_frame_app.py
"""
import time

from dotenv import load_dotenv

load_dotenv()

from JFI.manager.pt_console_manager import PromptToolkitConsoleManager  # noqa: E402


def main() -> None:
    console = PromptToolkitConsoleManager()

    console.set_status(
        session="theming-pass",
        phases=["planner", "imp", "testing", "reviewer"],
        phase="reviewer",
        state="streaming",
        plan=(7, 9),
        tokens=(12300, 32768),
        task="3.3 Write docs/Themes.md with preset screenshots",
    )
    console.mark_phase_done("planner")
    console.mark_phase_done("imp")
    console.mark_phase_done("testing")
    # Cumulative token counters are internal render state, not exposed via
    # set_status — set directly so the header's ↓/↑ figures aren't blank.
    console._tokens_read = 98200
    console._tokens_written = 12300

    console.display_rule("Just Finish It — Generic Autonomous Mode 🤖")
    console.display_user("add a light/dark theme switch and document it")

    # Mirrors print_agent_response's own tag-then-body write sequence rather
    # than display_assistant (which folds both into one line/style) — this is
    # exactly what a real streamed reply looks like on screen.
    console._line("class:out.assistant.tag", "🤖 Assistant")
    console._write(
        "class:out.assistant",
        "Done — the THEME env var now picks one of ten presets, and I've\n"
        "captured real screenshots for each. Docs are in `docs/Themes.md`.\n",
    )

    console.display_system("Session log opened at JFI/readme/session-2025.log")
    console.display_error("LLM stream ended without a finish reason — retrying in 5s")

    # The capturing script kills this process once it has read enough frames;
    # sleeping well past that window (rather than returning) keeps the UI up
    # and avoids main()'s own exit path clearing the screen mid-capture.
    console.run(lambda: time.sleep(30))


if __name__ == "__main__":
    main()
