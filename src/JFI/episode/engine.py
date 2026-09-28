"""run_episode(): one scoped v2 LLM conversation, start to end.

End conditions (laya_plan.md G5, §5.3), recorded as Episode.end_reason:
- "finish":   the role's finish tool (finish / mark_leaf_done) succeeded;
- "budget":   the next request would exceed the episode's token budget --
              the caller treats that as proof the node was too big;
- "turn_cap": MAX_EPISODE_TURNS model turns without finishing;
- "error":    the LLM kept failing after retries;
- "stopped":  the user stopped the run.

Every message is persisted as a HistoryMessage tagged with the episode id,
and the conversation is only ever these rows -- no other episode's turns.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from JFI.episode.brief import ScopeAnchor, build_kickoff
from JFI.episode.budget import estimate_tokens, max_episode_turns, usage_tokens
from JFI.episode.directives import take_directives
from JFI.episode.tools import EpisodeTools
from JFI.models import Episode, get_session
from JFI.models._util import utcnow
from JFI.session.history_store import append_history_to_db

LLM_RETRIES = 2
LLM_RETRY_DELAY_SECONDS = 3.0


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
    directives = take_directives(engine, session_id, anchor.node_id, episode_id)
    messages: List[Dict[str, Any]] = []

    def add(message: Dict[str, Any]) -> None:
        messages.append(message)
        append_history_to_db(engine, session_id, [message], episode_id=episode_id)

    add({"role": "system", "content": system_message})
    add({"role": "user", "content": build_kickoff(anchor, directives)})

    finish_tool = anchor.finish.split("(")[0]
    turn_cap = max_turns or max_episode_turns()
    result = EpisodeResult(episode_id, "turn_cap", 0, 0)
    failures: Dict[tuple, int] = {}

    while True:
        if console.should_stop():
            result.end_reason = "stopped"
            break
        console.wait_while_paused()
        if result.turns >= turn_cap:
            result.end_reason = "turn_cap"
            break
        schemas = tools.schemas()
        request_estimate = estimate_tokens(messages, schemas)
        result.tokens = max(result.tokens, request_estimate)
        if request_estimate > budget:
            result.end_reason = "budget"
            break

        parsed = _call_llm(llm, console, messages, schemas, request_estimate)
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


def _call_llm(llm, console, messages, schemas, estimate) -> Optional[Dict[str, Any]]:
    for attempt in range(LLM_RETRIES + 1):
        if console.should_stop():
            return None
        try:
            response = llm.send_message(messages, tools=schemas)
            return console.print_agent_response(response, prompt_tokens_estimate=estimate)
        except Exception as e:
            console.display_error(f"LLM request failed (attempt {attempt + 1}/{LLM_RETRIES + 1}): {e}")
            if attempt < LLM_RETRIES:
                time.sleep(LLM_RETRY_DELAY_SECONDS)
    return None


def _execute(console, tools: EpisodeTools, call: Dict[str, Any], failures: Dict[tuple, int]):
    from JFI.runner import _is_failure, _repair_directive  # the same AUTO-RECTIFY coaching v1 uses

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
    text = str(text)
    signature = (name, raw_args)
    if _is_failure(text):
        failures[signature] = failures.get(signature, 0) + 1
        text = f"{text}\n\n{_repair_directive(name, args, text, failures[signature])}"
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
