"""AUTO-RECTIFY coaching for a failed tool call inside an episode: every tool
reports failure with a leading "Error", and the model gets one concrete next
action (the argument to change, the path to check) instead of generic advice
-- generic advice gets ignored and the same call is re-issued."""

from typing import Any, Dict

# How many times the same failing call is coached before the model is told to
# abandon that approach entirely.
FAILURE_RETRY_LIMIT = 3


def is_failure(result: Any) -> bool:
    """Every tool reports failure with a leading 'Error'."""
    return str(result).lstrip().startswith("Error")


def repair_directive(func_name: str, args: Dict[str, Any], result: str, attempt: int) -> str:
    """
    Concrete next action for a failed call, so the model corrects itself instead
    of re-issuing the same call. Generic advice gets ignored; naming the exact
    tool and argument to change does not.
    """
    if attempt >= FAILURE_RETRY_LIMIT:
        return (
            f"AUTO-RECTIFY: this identical {func_name} call has now failed {attempt} times. "
            "Stop repeating it. Either solve the step a different way, or if it genuinely "
            "cannot be done, leave the leaf not done, record the blocker with add_reviewer_note, "
            "and move on to the next pending leaf."
        )

    lowered = str(result).lower()

    if func_name == "replace_in_file":
        if "not found" in lowered:
            return (
                "AUTO-RECTIFY: the file was NOT modified. read_file "
                f"'{args.get('file_path', '')}' and copy the target line exactly as it appears, "
                "including its leading '- ' and indentation, then call replace_in_file again."
            )
        if "appears" in lowered and "times" in lowered:
            return (
                "AUTO-RECTIFY: the file was NOT modified because old_string matched several "
                "places. Extend old_string with the line above or below it so it matches once."
            )

    if func_name in ("read_file", "write_file", "append_to_file", "view_image", "extract_video_frames") \
            and "does not exist" in lowered:
        return (
            "AUTO-RECTIFY: that path is wrong. Run execute_command with "
            "'ls -la .' (or the parent directory) to find the real path, then retry."
        )

    if func_name == "extract_video_frames":
        if "not installed" in lowered:
            return (
                "AUTO-RECTIFY: no video processing capability in this environment (the "
                "'ffmpeg' binary is missing) — retrying will not help. Skip this step, note "
                "the blocker in the plan, and continue without extracted frames."
            )
        if "no distinct frames" in lowered:
            return (
                "AUTO-RECTIFY: retry the same call with a lower threshold (e.g. 0.1) to catch "
                "subtler scene changes."
            )

    if func_name == "execute_command":
        if "timed out after" in lowered:
            current_timeout = args.get("timeout", 300)
            next_timeout = max(int(current_timeout) * 2, 900)
            return (
                f"AUTO-RECTIFY: the command did not fail — it simply needed more than "
                f"{current_timeout}s (no STDERR is shown because nothing went wrong, it just "
                f"wasn't finished yet). This is normal for package installs/downloads and "
                f"builds. Re-run the SAME command again, this time passing "
                f"timeout={next_timeout} to execute_command. Do not add flags or change the "
                "command to work around this — a longer timeout is the fix."
            )
        return (
            "AUTO-RECTIFY: the command failed — read the STDERR above and fix the cause "
            "(missing dependency, wrong path, syntax error) before re-running it. Do not "
            "re-run the identical command unchanged."
        )

    if func_name == "capture_screenshot" and ("no display" in lowered or "not installed" in lowered):
        return (
            "AUTO-RECTIFY: no screenshot capability in this environment (no display, or the "
            "'mss' package is missing) — retrying will not help. Skip this step, note the "
            "blocker in the plan, and continue without a screenshot."
        )

    return (
        f"AUTO-RECTIFY: the {func_name} call failed and nothing was changed. Diagnose the "
        "message above and issue a corrected call."
    )

