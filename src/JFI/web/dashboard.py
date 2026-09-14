"""Streamlit dashboard for monitoring (and approving) a running `./jfi`
session.

Runs as its own process, completely separate from `./jfi` -- there's no
in-memory link between them, so everything here comes from files under the
session's own JFI/<session>/ folder:

- web_status.json / web_answer.json: written/read by JFI.manager.web_bridge,
  only present when the jfi process was started with JFI_WEB_BRIDGE=1. This
  is what gives "live" state (current phase, streaming/thinking, tokens) and
  lets a button click here answer a pending approval.
- plan.md / review.md / run.log: written by jfi regardless of the bridge, so
  progress/history are visible here even for a session that isn't currently
  running with the bridge enabled -- just not live.

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
    st.error(f"⏸️  Action required: {awaiting.get('prompt', '')}")
    options = awaiting.get("options") or []
    cols = st.columns(len(options)) if options else []
    for col, option in zip(cols, options):
        if col.button(option["label"], key=f"answer-{option['key']}", use_container_width=True):
            atomic_write_json(session_path / ANSWER_FILENAME, {"key": option["key"]})
            st.toast(f"Sent: {option['label']}")
            time.sleep(0.3)
            st.rerun()


def _render_status(status: dict) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Phase", (status.get("phase") or "-").title() or "-")
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


def main() -> None:
    root = _sessions_root()
    sessions = _discover_sessions(root)

    st.sidebar.title("📋 Just Finish It")
    if not sessions:
        st.sidebar.info(f"No sessions found under {root}/")
        st.info(
            f"No sessions found under `{root}/`. Run `./jfi` from this same directory "
            "(or set SESSION_PATH to match) and start a session first."
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
        with st.expander("Recent activity (run.log tail)"):
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            st.code("\n".join(lines[-200:]) or "(empty)")

    if auto_refresh:
        time.sleep(refresh_seconds)
        st.rerun()


main()
