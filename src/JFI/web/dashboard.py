"""Streamlit dashboard for monitoring, answering, and driving a running
`./jfi` session -- the goal prompt, approvals, and new follow-up requests
can all be typed here instead of at the terminal.

Runs as its own process, completely separate from `./jfi` -- there's no
in-memory link between them, so everything here comes from files under the
session's own JFI/<session>/ folder:

- web_status.json / web_answer.json: written/read by JFI.manager.web_bridge,
  only present when the jfi process was started with JFI_WEB_BRIDGE=1 (which
  also auto-launches this dashboard itself, see JFI.web.launcher and
  runner._launch_web_dashboard, unless JFI_WEB_DASHBOARD=0). This is what
  gives "live" state (current phase, streaming/thinking, tokens) and lets
  this page answer a pending prompt (button or free text) or queue a brand-
  new request while idle -- see _render_approval/_render_queue_input.
- plan.md / review.md / run.log: written by jfi regardless of the bridge, so
  progress/history are visible here even for a session that isn't currently
  running with the bridge enabled -- just not live, and not answerable.

Launch with: streamlit run src/JFI/web/dashboard.py
(or the `jfi-web` console script, see JFI.web.launcher)
"""

import json
import os
import re
import time
from pathlib import Path

import streamlit as st

from JFI.manager.web_bridge import ANSWER_FILENAME, STATUS_FILENAME, atomic_write_json
from JFI.session.simple_session_manager import PHASE_SECTION

st.set_page_config(page_title="Just Finish It — Status", page_icon="📋", layout="wide")


def _sessions_root() -> Path:
    """Same rule SimpleSessionManager uses to place a session's JFI/ folder
    (see SESSION_PATH in simple_session_manager.py) -- so this dashboard
    looks in the same place jfi actually wrote to."""
    return Path(os.environ.get("SESSION_PATH", ".")) / "JFI"


def _discover_sessions(root: Path) -> list[Path]:
    if not root.is_dir():
        return []

    def last_activity(p: Path) -> float:
        candidates = [
            f.stat().st_mtime for f in (p / STATUS_FILENAME, p / "history.jsonl.gz", p / "plan.md")
            if f.exists()
        ]
        return max(candidates, default=p.stat().st_mtime)

    return sorted((p for p in root.iterdir() if p.is_dir()), key=last_activity, reverse=True)


# Checkbox-counting regexes kept identical to
# SimpleSessionManager.plan_progress/phase_progress -- a user-skipped
# "- [○]" item counts as resolved alongside "- [x]", same as there.
def _plan_counts(text: str) -> tuple[int, int]:
    done = len(re.findall(r"^[ \t]*[-*][ \t]*\[[xX]\]", text, re.M))
    skipped = len(re.findall(r"^[ \t]*[-*][ \t]*\[○\]", text, re.M))
    todo = len(re.findall(r"^[ \t]*[-*][ \t]*\[[ ]\]", text, re.M))
    return done + skipped, done + skipped + todo


def _phase_counts(text: str, section: str) -> tuple[int, int]:
    done = skipped = todo = 0
    in_section = False
    for line in text.splitlines():
        header = re.match(r"^\s*#{1,6}\s+(.*)", line)
        if header:
            in_section = header.group(1).strip().lower().startswith(section.lower())
            continue
        if not in_section:
            continue
        if re.match(r"^[ \t]*[-*][ \t]*\[[xX]\]", line):
            done += 1
        elif re.match(r"^[ \t]*[-*][ \t]*\[○\]", line):
            skipped += 1
        elif re.match(r"^[ \t]*[-*][ \t]*\[[ ]\]", line):
            todo += 1
    return done + skipped, done + skipped + todo


def _load_status(session_path: Path) -> dict | None:
    path = session_path / STATUS_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _render_approval(session_path: Path, awaiting: dict) -> None:
    """Renders whatever `./jfi` is currently blocked on: a get_user_choice
    menu (buttons, one per option) or a plain get_user_input prompt (the
    goal, or any other free-text question) as a text box -- either way,
    submitting writes web_answer.json for WebBridge._relay_answer to pick
    up and feed into console.submit_external_answer()."""
    st.error(f"⏸️  Action required: {awaiting.get('prompt', '')}")
    options = awaiting.get("options") or []
    if options:
        cols = st.columns(len(options))
        for col, option in zip(cols, options):
            if col.button(option["label"], key=f"answer-{option['key']}", use_container_width=True):
                atomic_write_json(session_path / ANSWER_FILENAME, {"type": "answer", "key": option["key"]})
                st.toast(f"Sent: {option['label']}")
                time.sleep(0.3)
                st.rerun()
    else:
        with st.form(key="answer-form", clear_on_submit=True):
            reply = st.text_area("Your answer", label_visibility="collapsed", height=100)
            if st.form_submit_button("Send") and reply.strip():
                atomic_write_json(session_path / ANSWER_FILENAME, {"type": "answer", "key": reply.strip()})
                st.toast("Sent")
                time.sleep(0.3)
                st.rerun()


def _render_queue_input(session_path: Path) -> None:
    """Shown instead of _render_approval when nothing is currently awaiting
    an answer -- the dashboard equivalent of typing a line at the terminal's
    idle input: queues a brand-new follow-up request via
    console.submit_external_queue_item(), replayed once the current review
    phase lands (or starts the pipeline back up if it was already idle)."""
    with st.form(key="queue-form", clear_on_submit=True):
        st.caption("Queue a new request (runs after the current review phase, or starts one up if idle):")
        text = st.text_area("Request", label_visibility="collapsed", height=100)
        if st.form_submit_button("Queue") and text.strip():
            atomic_write_json(session_path / ANSWER_FILENAME, {"type": "queue", "text": text.strip()})
            st.toast("Queued")
            time.sleep(0.3)
            st.rerun()


def _render_status(status: dict) -> None:
    c1, c2, c3, c4 = st.columns(4)
    phase_label = (status.get("phase") or "-").title() or "-"
    # Planner's own internal stage (Arc/Lead/Journy/Func/Task -- see
    # runner.PLANNER_STAGES) rides the same "Phase" metric rather than a
    # separate widget, so at a glance this reads "Planner (Journy)" instead
    # of leaving the dashboard indistinguishable across all four passes.
    if status.get("stage"):
        phase_label += f" ({status['stage']})"
    c1.metric("Phase", phase_label)
    c2.metric("State", status.get("state") or "-")
    c3.metric("Iteration", status.get("iteration") or 1)
    tokens = status.get("tokens")
    c4.metric("Context tokens", f"{tokens[0]:,} / {tokens[1]:,}" if tokens else "-")

    plan = status.get("plan")
    if plan and plan[1]:
        done, total = plan
        st.progress(done / total, text=f"Overall plan: {done}/{total}")

    phase_plan = status.get("phase_plan")
    if phase_plan and phase_plan[1]:
        done, total = phase_plan
        st.progress(done / total, text=f"This phase: {done}/{total}")

    if status.get("task"):
        st.caption(f"Current item: {status['task']}")

    st.caption(
        f"Tokens this run — read: {status.get('tokens_read', 0):,} · "
        f"written: {status.get('tokens_written', 0):,} · "
        f"queued input: {status.get('queue_size', 0)}"
    )

    processes = status.get("background_processes")
    if processes:
        with st.expander(f"Background processes ({len(processes)})", expanded=True):
            for p in processes:
                where = " ".join(
                    filter(None, [
                        f"host={p['host']}" if p.get("host") else None,
                        f"port={p['port']}" if p.get("port") else None,
                    ])
                )
                st.caption(
                    f"**{p['handle']}** · `{p['command']}` · pid={p['pid']}"
                    + (f" · {where}" if where else "")
                    + f" · {p['status']} · {p['elapsed']}s"
                )


def main() -> None:
    root = _sessions_root()
    sessions = _discover_sessions(root)

    st.sidebar.title("📋 Just Finish It")
    if not sessions:
        st.sidebar.info(f"No sessions found under {root}/")
        st.info(
            f"No sessions found under `{root}/`. Run `./jfi` from this same directory "
            "(or set SESSION_PATH to match) and enter a session name at its terminal prompt "
            "first -- that one initial prompt still has to happen there, since the session's "
            "own folder (and this page's way of seeing it) doesn't exist until it's answered. "
            "Everything after that -- the goal, approvals, new requests -- can be typed here."
        )
        return

    names = [p.name for p in sessions]
    selected_name = st.sidebar.selectbox("Session", names)
    session_path = root / selected_name
    auto_refresh = st.sidebar.checkbox("Auto-refresh", value=True)
    refresh_seconds = st.sidebar.slider("Refresh every (seconds)", 1, 30, 3, disabled=not auto_refresh)

    st.title(selected_name)

    status = _load_status(session_path)
    if status is None:
        st.warning(
            "No live status for this session yet. Start (or restart) `./jfi` for it with "
            "`JFI_WEB_BRIDGE=1` in the environment to get live phase/state/token updates and "
            "to approve prompts from here. Plan progress below still works without it."
        )
    else:
        awaiting = status.get("awaiting")
        if awaiting:
            _render_approval(session_path, awaiting)
        else:
            _render_queue_input(session_path)
        _render_status(status)

    st.subheader("Plan")
    plan_path = session_path / "plan.md"
    if plan_path.exists():
        plan_text = plan_path.read_text(encoding="utf-8")
        done, total = _plan_counts(plan_text)
        if total:
            st.progress(done / total, text=f"{done}/{total} items ticked")
        for phase, section in PHASE_SECTION.items():
            pdone, ptotal = _phase_counts(plan_text, section)
            if ptotal:
                st.caption(f"{section}: {pdone}/{ptotal}")
        with st.expander("Full plan.md", expanded=status is None):
            st.markdown(plan_text)
    else:
        st.info("No plan.md yet for this session.")

    review_path = session_path / "review.md"
    if review_path.exists():
        st.subheader("⚠️ Latest review report (pending fixes)")
        st.markdown(review_path.read_text(encoding="utf-8"))

    log_path = session_path / "run.log"
    if log_path.exists():
        with st.expander("Recent activity (run.log tail, newest first)"):
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            # Newest line first -- this panel re-renders from scratch on every
            # auto-refresh (see the st.rerun() below), so a chronological tail
            # would put the newest activity at the BOTTOM, forcing a re-scroll
            # every refresh just to see what's new. Reversed, the newest line
            # is always visible at the top without scrolling.
            st.code("\n".join(reversed(lines[-200:])) or "(empty)")

    if auto_refresh:
        time.sleep(refresh_seconds)
        st.rerun()


main()
