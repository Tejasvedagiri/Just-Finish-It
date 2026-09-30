"""run_episode(): one scoped v2 LLM conversation, start to end.

End conditions (laya_plan.md G5, §5.3), recorded as Episode.end_reason:
- "finish":   the role's finish tool (finish / mark_leaf_done) succeeded;
- "budget":   the next request would exceed the episode's token budget --
              the caller treats that as proof the node was too big;
- "turn_cap": MAX_EPISODE_TURNS model turns without finishing;
- "error":    the LLM request failed and the user chose Retry but it was
              still failing when they stopped answering (in practice only
              reached by consoles with no choice menu);
- "stopped":  the user stopped the run, including "Stop" at the LLM-failure
              menu.

LLM failures: a retryable one (a dropped connection, a 5xx, a reply cut off
by the output cap) is retried LLM_RETRY_LIMIT times automatically; then --
or straight away for anything else -- the user is asked "Retry, or stop the
run?". The run never gives up on its own. LOG_LLM_CALL_DEBUG=1 appends every
request/response pair to .jfi/llm_debug.jsonl.

Every message is persisted as a HistoryMessage tagged with the episode id,
and the conversation is only ever these rows -- no other episode's turns.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from JFI.episode.brief import ScopeAnchor, build_kickoff
from JFI.episode.budget import estimate_tokens, max_episode_turns, usage_tokens
from JFI.episode.directives import record_delivered_directive
from JFI.episode.rectify import is_failure, repair_directive
from JFI.episode.tools import EpisodeTools
from JFI.llm import retry
from JFI.manager.abstract_manager import AbstractManager, ResponseTooLongError
from JFI.models import Episode, get_session
from JFI.models._util import utcnow
from JFI.session.history_store import append_history_to_db
from JFI.tool.result_cap import cap_result

# Observed on the stui run: the Architect spent 6 turns reading a 115 KB
# HTML file in 100-250-line chunks, hit the 20k budget and ended with no
# plan at all -- nothing had told it the budget was running out, and every
# chunk it had already digested still sat in the conversation. So: warn once
# at WARN_AT, and from PRUNE_AT on shrink all but the newest KEEP_RECENT
# tool results to a stub (the DB history keeps them in full).
WARN_AT = 0.6
PRUNE_AT = 0.75
KEEP_RECENT = 4
STUB_CHARS = 300


def _prune_old_results(messages: List[Dict[str, Any]]) -> bool:
    tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    changed = False
    for i in tool_indexes[:-KEEP_RECENT]:
        content = messages[i].get("content")
        if isinstance(content, str) and len(content) > STUB_CHARS * 2:
            messages[i] = {**messages[i], "content": content[:STUB_CHARS]
                           + "\n...(older result trimmed to save this conversation's budget; "
                             "call the tool again if you still need it)"}
            changed = True
    return changed


@dataclass
class EpisodeResult:
    episode_id: int
    end_reason: str
    turns: int
    tokens: int
    summary: str = ""
    tools_used: List[str] = field(default_factory=list)
    tools_loaded: List[str] = field(default_factory=list)


def _start_episode(engine, session_id: str, role: str, mode: str, node_id: Optional[int]) -> int:
    with get_session(engine) as db:
        row = Episode(session_id=session_id, node_id=node_id, role=role, mode=mode)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def _end_episode(engine, episode_id: int, result: EpisodeResult) -> None:
    with get_session(engine) as db:
        row = db.get(Episode, episode_id)
        row.ended_at = utcnow()
        row.turns, row.tokens, row.end_reason = result.turns, result.tokens, result.end_reason
        row.tools_used, row.tools_loaded = list(result.tools_used), list(result.tools_loaded)
        db.add(row)
        db.commit()


def run_episode(llm, console, engine, session_id: str, *, role: str, mode: str, anchor: ScopeAnchor,
                system_message: str, tools: EpisodeTools, budget: int,
                max_turns: Optional[int] = None) -> EpisodeResult:
    episode_id = _start_episode(engine, session_id, role, mode, anchor.node_id)
    messages: List[Dict[str, Any]] = []

    def add(message: Dict[str, Any]) -> None:
        messages.append(message)
        append_history_to_db(engine, session_id, [message], episode_id=episode_id)

    add({"role": "system", "content": system_message})
    add({"role": "user", "content": build_kickoff(anchor)})

    finish_tool = anchor.finish.split("(")[0]
    turn_cap = max_turns or max_episode_turns()
    result = EpisodeResult(episode_id, "turn_cap", 0, 0)
    failures: Dict[tuple, int] = {}
    warned = False

    while True:
        if console.should_stop():
            result.end_reason = "stopped"
            break
        console.wait_while_paused()
        for note in console.drain_forced_input():
            # `!text` typed mid-run goes into the episode that's running now
            # and applies from here on. Before this, planner and Dev episodes
            # never read it.
            record_delivered_directive(engine, session_id, note, anchor.node_id, episode_id)
            add({"role": "user", "content": f"USER INTERJECTION (apply this from here on):\n{note}"})
        if result.turns >= turn_cap:
            result.end_reason = "turn_cap"
            break
        schemas = tools.schemas()
        request_estimate = estimate_tokens(messages, schemas)
        if request_estimate > budget * PRUNE_AT and _prune_old_results(messages):
            request_estimate = estimate_tokens(messages, schemas)
        if request_estimate > budget * WARN_AT and not warned:
            warned = True
            add({"role": "user", "content": (
                f"BUDGET: this conversation has used about {request_estimate * 100 // budget}% of its "
                f"{budget}-token budget. Stop exploring: write your output now, from what you already "
                f"know, and call {anchor.finish}. Older tool results will be trimmed from here on.")})
            request_estimate = estimate_tokens(messages, schemas)
        result.tokens = max(result.tokens, request_estimate)
        if request_estimate > budget:
            result.end_reason = "budget"
            break

        console.set_status(tokens=(request_estimate, budget))
        if _show_stream_prompts():
            dump_prompt(console, role, messages)
        parsed = _call_llm(llm, console, messages, schemas, request_estimate, add, role, episode_id)
        if parsed is None:
            result.end_reason = "stopped" if console.should_stop() else "error"
            break
        result.turns += 1
        reported = usage_tokens(parsed.get("usage"))
        if reported is not None:
            result.tokens = max(result.tokens, reported)

        content, tool_calls = parsed.get("content"), parsed.get("tool_calls")
        if content or tool_calls:
            assistant: Dict[str, Any] = {"role": "assistant"}
            if content:
                assistant["content"] = content
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            add(assistant)

        finished = False
        for call in tool_calls or []:
            name = call["function"]["name"]
            text, image_url = _execute(console, tools, call, failures)
            add({"role": "tool", "tool_call_id": call["id"], "name": name, "content": text})
            if image_url:
                add({"role": "user", "content": [
                    {"type": "text", "text": "(image attached -- see the view_image result above)"},
                    {"type": "image_url", "image_url": {"url": image_url}}]})
            if name == finish_tool and not text.startswith("Error"):
                finished = True
                result.summary = _summary_of(call)
        if finished:
            result.end_reason = "finish"
            break
        if not tool_calls:
            add({"role": "user", "content": (
                "AUTO-RECTIFY: take one concrete action with a tool now"
                + (" -- your last turn was only reasoning, and that is lost" if parsed.get("had_reasoning")
                   and not content else "")
                + f". When the node in SCOPE is complete, call {anchor.finish}.")})

    result.tools_used, result.tools_loaded = list(tools.used), list(tools.loaded)
    _end_episode(engine, episode_id, result)
    return result


TOO_LONG_NUDGE = ("Your last reply was cut off: its reasoning ran past the limit before any tool call. Don't plan "
                  "everything at once. Make your next 2-3 tool calls now (look something up, record a decision, "
                  "add a node), then continue step by step; you can keep planning between calls.")


def _call_llm(llm, console, messages, schemas, estimate, add=None, role: str = "", episode_id=None
              ) -> Optional[Dict[str, Any]]:
    attempt = 0
    while True:
        if console.should_stop():
            return None
        try:
            response = llm.send_message(messages, tools=schemas)
            parsed = console.print_agent_response(response, prompt_tokens_estimate=estimate)
            if _log_llm_calls():
                _log_call(role, episode_id, messages, schemas, parsed)
            return parsed
        except Exception as e:
            attempt += 1
            if isinstance(e, ResponseTooLongError):
                # Observed on the stui run: the Architect tried to plan all 18
                # components in its hidden reasoning before its first tool call,
                # hit REASONING_OUTPUT_CAP, and the same request was resent twice
                # -- so it did the same thing twice and the plan ended empty. The
                # retry has to change the request, not repeat it.
                console.display_error(f"LLM reply cut off (attempt {attempt}/{retry.LLM_RETRY_LIMIT + 1}): {e}")
                if add is not None and attempt <= retry.LLM_RETRY_LIMIT:
                    add({"role": "user", "content": TOO_LONG_NUDGE})
            if retry.is_retryable(e) and attempt <= retry.LLM_RETRY_LIMIT and not console.should_stop():
                if not isinstance(e, ResponseTooLongError):
                    console.display_error(f"LLM request failed (attempt {attempt}/{retry.LLM_RETRY_LIMIT + 1}): "
                                          f"{retry.format_error(e)} — retrying in {retry.LLM_RETRY_DELAY_SECONDS:.0f}s...")
                    retry.interruptible_sleep(console, retry.LLM_RETRY_DELAY_SECONDS)
                continue
            # Out of automatic retries (or not a retryable error): the run
            # only stops if the user says so -- a transient server problem
            # used to end the episode on "error" and take the plan with it.
            console.display_error(f"LLM request failed: {retry.format_error(e)}")
            if console.should_stop():
                return None
            choice = console.get_user_choice("LLM request failed. Retry, or stop the run?",
                                             [("r", "Retry now"), ("s", "Stop (progress is saved)")])
            if choice != "r" or console.should_stop():
                console.request_stop()
                return None
            attempt = 0  # a deliberate retry earns a fresh automatic-retry budget


def _show_stream_prompts() -> bool:
    """SHOW_STREAM_PROMPTS=1 (or true/yes/on) in .env: dump the exact
    messages sent to the LLM every turn — see dump_prompt. Read fresh each
    call rather than cached: it's checked once per turn at most, never in a
    hot loop, and a live .env edit (e.g. via Ctrl+N into a fresh process)
    should still take effect without a restart being required."""
    return os.environ.get("SHOW_STREAM_PROMPTS", "").strip().lower() in ("1", "true", "yes", "on")


def dump_prompt(console: AbstractManager, role: str, messages: List[Dict[str, Any]]) -> None:
    """
    SHOW_STREAM_PROMPTS=1: prints every message about to be sent to the LLM
    this turn, in full — deliberately untruncated, unlike
    display_tool_result's 8-line/width-capped preview elsewhere in the
    console. Meant for prompt-engineering and context-compression
    debugging, where seeing exactly what the model is about to see (all of
    it) is the entire point.
    """
    console.display_rule(f"PROMPT SENT — {role.upper()} ({len(messages)} message(s))")
    for i, message in enumerate(messages, 1):
        header = f"[{i}] {message.get('role', '?')}"
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            names = ", ".join(tc.get("function", {}).get("name", "?") for tc in tool_calls)
            header += f"  (tool_calls: {names})"
        if message.get("tool_call_id"):
            header += f"  (tool_call_id: {message['tool_call_id']})"
        console.display_system(header)

        content = message.get("content")
        if isinstance(content, list):
            # Multimodal content (view_image's follow-up user turn): show the
            # text parts in full, note images without dumping raw base64.
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    console.display_system(str(part.get("text") or ""))
                elif part.get("type") == "image_url":
                    console.display_system("(image attached)")
        elif content:
            console.display_system(str(content))
    console.display_rule("END PROMPT")


def _log_llm_calls() -> bool:
    return os.environ.get("LOG_LLM_CALL_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _log_call(role: str, episode_id, messages, tools, parsed) -> None:
    """One JSON line per LLM call in .jfi/llm_debug.jsonl -- a debugging aid,
    so an I/O error here never interrupts the run."""
    record = {"timestamp": datetime.now().isoformat(timespec="seconds"), "role": role, "episode_id": episode_id,
              "request": {"messages": messages, "tools": tools}, "response": parsed}
    try:
        path = Path(os.environ.get("SESSION_PATH", ".")) / ".jfi" / "llm_debug.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _execute(console, tools: EpisodeTools, call: Dict[str, Any], failures: Dict[tuple, int]):
    name, raw_args = call["function"]["name"], call["function"].get("arguments") or "{}"
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as e:
        console.display_tool_call(name)
        return (f"Error: the arguments for {name} were not valid JSON ({e}). Nothing ran. "
                "AUTO-RECTIFY: send smaller arguments."), None
    if not isinstance(args, dict):
        console.display_tool_call(name)
        return f"Error: the arguments for {name} must be a JSON object.", None
    console.display_tool_call(name, args)
    try:
        raw = tools.call(name, args)
    except TypeError as e:
        raw = f"Error: wrong arguments for {name} ({e})."
    except Exception as e:
        raw = f"Error executing tool {name}: {e}"
    text, image_url = raw if isinstance(raw, tuple) else (raw, None)
    # The v2 read tools cap their own results with a precise hint; this backstop
    # is for the shared ones. Observed on the calc run: the reviewer's
    # `findstr "JFI:"` over a 4.7 MB log returned 99,395 characters through
    # execute_command, and the episode ended over budget after one turn.
    text = cap_result(str(text), "Ask for less: a narrower command or pattern, a line range, or one symbol.")
    signature = (name, raw_args)
    if is_failure(text):
        failures[signature] = failures.get(signature, 0) + 1
        text = f"{text}\n\n{repair_directive(name, args, text, failures[signature])}"
        image_url = None
    else:
        failures.pop(signature, None)
    console.display_tool_result(text)
    return text, image_url


def _summary_of(call: Dict[str, Any]) -> str:
    try:
        return str(json.loads(call["function"].get("arguments") or "{}").get("summary", ""))
    except (json.JSONDecodeError, AttributeError):
        return ""


def make_finish(anchor: ScopeAnchor):
    """The `finish` tool for one episode: accepts only the node in SCOPE."""
    def finish(node_id: int, summary: str = "") -> str:
        if anchor.node_id is not None and int(node_id) != anchor.node_id:
            return f"Error: this conversation is about node {anchor.node_id}, not {node_id}."
        return f"Finished node {node_id}."
    return finish
