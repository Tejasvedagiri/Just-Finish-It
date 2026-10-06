"""Streamlit dashboard for monitoring, answering, and driving a running
`./jfi` session -- the goal prompt, approvals, and new follow-up requests
can all be typed here instead of at the terminal.

Runs as its own process, completely separate from `./jfi` -- there's no
in-memory link between them, so most of what this page shows comes from
the shared DB (`.jfi/JFI.db`), genuinely scoped to whichever session_id is
selected in the sidebar below (every row there is keyed by it -- see
JFI.models.db), plus a couple of live-only files under the flat `.jfi/`
folder a running session itself writes to (see SimpleSessionManager.
__init__'s own note on why it's flat: only ONE JFI session can run
against a project at a time):

- web_status.json / web_answer.json: written/read by JFI.manager.web_bridge,
  only present when the jfi process was started with JFI_WEB_BRIDGE=1 (which
  also auto-launches this dashboard itself, see JFI.web.launcher and
  runner._launch_web_dashboard, unless JFI_WEB_DASHBOARD=0). This is what
  gives "live" state (current phase, streaming/thinking, tokens) and lets
  this page answer a pending prompt (button or free text) or queue a brand-
  new request while idle -- see _render_approval/_render_queue_input. Only
  ever describes whichever session is CURRENTLY running (status["session"]
  names it) -- the sidebar's session picker exists to browse a DIFFERENT
  session's past plan/log/review from the DB, not to interact with it live.
- The plan, the review report (SessionNote, kind="review_report" -- see
  JFI.tool.note_tools), and the recent-activity log are all DB-backed and
  read directly via get_engine, not from a file, the same as the running
  jfi process itself.

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
from JFI.models import get_engine
from JFI.tool.note_tools import get_note, REVIEW_REPORT
from JFI.tool.plan_db_tools import (
    note_points, phase_progress_db, plan_judge_rows, plan_progress_db, render_plan_markdown,
)

st.set_page_config(page_title="Just Finish It — Status", page_icon="📋", layout="wide")


def _project_root() -> Path:
    """Same SESSION_PATH SimpleSessionManager resolves its own
    `self._project_root` from (see simple_session_manager.py's __init__) --
    the one shared `.jfi/JFI.db` lives here. get_engine must be called with
    this, never with a session_path -- passing `.jfi/` itself silently points
    at a different (and normally empty) JFI.db one level too deep, instead of
    the real one every actual jfi session writes to."""
    return Path(os.environ.get("SESSION_PATH", "."))


def _list_session_ids(engine) -> list[str]:
    """Every session_id ever run against this project, most recently
    CREATED first (SessionRecord.updated_at is set once at insert and
    never bumped again by anything in this codebase, so ordering by it
    would look like recency but silently just be creation order in
    disguise -- ordering by created_at directly is at least honest about
    what it actually reflects). `.jfi/` is flat now (see module docstring)
    -- there is no per-session folder left to scan, so the DB's own
    SessionRecord table is the only place a list of sessions can come
    from (same query export_db._list_session_ids uses)."""
    from sqlmodel import select

    from JFI.models import SessionRecord, get_session

    with get_session(engine) as db:
        rows = list(db.exec(select(SessionRecord).order_by(SessionRecord.created_at.desc())))
    return [row.session_id for row in rows]


def _recent_log_events(engine, session_id: str, limit: int = 200) -> list:
    """The last `limit` LogEvent rows for this session, oldest first --
    full cutover replacement for tailing a run.log file (see
    JFI.models.log_event's own docstring for why every tag lands there
    now, not just SYSTEM/RULE)."""
    from sqlmodel import select

    from JFI.models import LogEvent, get_session

    with get_session(engine) as db:
        rows = list(
            db.exec(
                select(LogEvent)
                .where(LogEvent.session_id == session_id)
                .order_by(LogEvent.seq.desc())
                .limit(limit)
            )
        )
    return list(reversed(rows))


def _load_status(jfi_dir: Path) -> dict | None:
    path = jfi_dir / STATUS_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _render_approval(jfi_dir: Path, awaiting: dict) -> None:
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
            if col.button(option["label"], key=f"answer-{option['key']}", width="stretch"):
                atomic_write_json(jfi_dir / ANSWER_FILENAME, {"type": "answer", "key": option["key"]})
                st.toast(f"Sent: {option['label']}")
                time.sleep(0.3)
                st.rerun()
    else:
        with st.form(key="answer-form", clear_on_submit=True):
            reply = st.text_area("Your answer", label_visibility="collapsed", height=100)
            if st.form_submit_button("Send") and reply.strip():
                atomic_write_json(jfi_dir / ANSWER_FILENAME, {"type": "answer", "key": reply.strip()})
                st.toast("Sent")
                time.sleep(0.3)
                st.rerun()


def _render_queue_input(jfi_dir: Path) -> None:
    """Shown instead of _render_approval when nothing is currently awaiting
    an answer -- the dashboard equivalent of typing a line at the terminal's
    idle input: queues a brand-new follow-up request via
    console.submit_external_queue_item(), replayed once the current review
    phase lands (or starts the pipeline back up if it was already idle)."""
    with st.form(key="queue-form", clear_on_submit=True):
        st.caption("Queue a new request (runs after the current review phase, or starts one up if idle):")
        text = st.text_area("Request", label_visibility="collapsed", height=100)
        if st.form_submit_button("Queue") and text.strip():
            atomic_write_json(jfi_dir / ANSWER_FILENAME, {"type": "queue", "text": text.strip()})
            st.toast("Queued")
            time.sleep(0.3)
            st.rerun()


def _render_pause_control(jfi_dir: Path, status: dict) -> None:
    """Pause / Resume, the dashboard's Ctrl+P. Writes the target state (not a
    toggle) for WebBridge._relay_answer -> console.submit_external_pause(), so
    the session holds cleanly before its next model turn; the turn already in
    flight finishes first."""
    paused = bool(status.get("is_paused"))
    left, right = st.columns([3, 1])
    with left:
        if paused:
            st.warning("⏸️  Paused: the session is holding before its next model turn.")
        else:
            st.caption("Running. Pause holds the session before its next model turn.")
    with right:
        label = "▶️ Resume" if paused else "⏸️ Pause"
        if st.button(label, key="pause-toggle", width="stretch"):
            atomic_write_json(jfi_dir / ANSWER_FILENAME, {"type": "pause", "paused": not paused})
            st.toast("Resuming" if paused else "Pausing after the current turn")
            time.sleep(0.3)
            st.rerun()


def _render_status(status: dict) -> None:
    c1, c2, c3, c4 = st.columns(4)
    phase_label = (status.get("phase") or "-").title() or "-"
    # Planner's own internal stage (Arc/Lead/Dev/Tickets/Task -- see
    # runner.PLANNER_ARC_STAGE/PLANNER_NODE_STAGES) rides the same "Phase"
    # metric rather than a separate widget, so at a glance this reads
    # "Planner (Dev)" instead of leaving the dashboard indistinguishable
    # across all four passes.
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
    _render_parallel(status.get("parallel"))

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


def _render_parallel(parallel: dict | None) -> None:
    """PARALLEL_LLM: the planner's side-by-side episodes. "Current item"
    names only whichever of them set it last."""
    running = (parallel or {}).get("running") or []
    if not running:
        return
    role = str(parallel.get("role") or "").title()
    with st.expander(f"{role} episodes in parallel: {len(running)} of {parallel.get('workers')}", expanded=True):
        if parallel.get("why"):
            st.caption(parallel["why"])
        now = time.time()
        for r in running:
            st.caption(f"**node {r['node_id']}** · {r['task']} · running {int(now - r['started_at'])}s")


def _render_node_detail(judge_rows: list) -> None:
    """One node's notes (what to implement and how) and references (where the
    context is), which the Task | Judge table can only show truncated."""
    options = {f"{row['#']} {row['Task'].lstrip('· ')}": row for row in judge_rows}
    choice = st.selectbox("Node", list(options), key="node_detail")
    row = options[choice]
    st.markdown(f"**{row['#']}** · {row['Level']} · {row['Final'] or 'unjudged'} · {row['Status'] or 'parent'}")
    st.markdown(row["Task"].lstrip("· "))
    if row["Notes"]:
        with st.container(border=True):
            st.markdown("**Notes**\n\n" + _notes_markdown(note_points(row["Notes"])))
    else:
        st.caption("No notes on this node.")
    if row["References"]:
        st.markdown("**References**\n" + "\n".join(f"- `{ref.strip()}`" for ref in row["References"].split(", ")))


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()#+\-.!|~^$])")


def _notes_markdown(points: list) -> str:
    """Notes as a bullet list; numbered steps nest under the point before
    them (usually "Steps:"). Notes are code-heavy ("+ - * /", "a ** b"), so
    everything is escaped rather than read as Markdown."""
    lines, nested = [], False
    for label, text in points:
        text = _MARKDOWN_SPECIAL.sub(r"\\\1", text)
        if label:
            lines.append(f"{'    ' if nested else ''}- **{label}.** {text}")
        else:
            nested = text.endswith(":")
            lines.append(f"- {text}")
    return "\n".join(lines)


def _render_runbook_and_design(engine, session_id: str) -> None:
    """The Architect's runbook (how to set up, run and test the project; ✓ =
    a command that has actually run) and design (stack, contracts,
    conventions, outline): what every later role works from."""
    from JFI.tool.plan_db_tools import plan_runbook_and_design

    runbook, design = plan_runbook_and_design(engine, session_id)
    if runbook:
        with st.expander(f"Runbook ({len(runbook)})"):
            st.dataframe(runbook, width="stretch", hide_index=True)
    if design:
        with st.expander(f"Design ({len(design)})"):
            st.dataframe(design, width="stretch", hide_index=True)


def _render_evidence(engine, session_id: str) -> None:
    """The ground truth the build is checked against (docs/old_new.md): every
    case in evidences/, how it was captured, and how its compare task went --
    with Accept, Edit and Re-capture. An edit or re-capture changes the
    evidence, so a running session compares that case again by itself."""
    from JFI.planner.nodes import load_nodes
    from JFI.tool.evidence_tools import (
        EVIDENCE_DIR, evidence_path, list_cases, mark_reviewed, read_evidence, recapture, save_edited_text,
        sync_evidence_names,
    )

    root = _project_root()
    sync_evidence_names(engine, session_id, root)
    cases = list_cases(root)
    if not cases:
        return
    compare_status = {c: n.status.value for n in load_nodes(engine, session_id)
                      if n.kind == "compare" for c in (n.cases or [])}
    rows = []
    for case in cases:
        evidence = read_evidence(root, case)
        reviewed = (evidence.spec if evidence.visual else evidence.header).get("reviewed", "")
        rows.append({"file": evidence.path.name, "case": case,
                     "kind": ("overview" if evidence.reference_only else "visual" if evidence.visual
                              else f"{len(evidence.items)} input(s)"),
                     "source": ("⚠ " if evidence.unverified else "") + evidence.source,
                     "reviewed": reviewed, "compare task": compare_status.get(case, "")})
    unverified = sum(r["source"].startswith("⚠") for r in rows)
    title = f"Evidence ({len(cases)})" + (f" — {unverified} not verified" if unverified else "")
    with st.expander(title, expanded=bool(unverified)):
        st.caption(f"The ground truth in `{EVIDENCE_DIR}/`, each file named after its task (1 = an Architect "
                   "component's overview, 1.2 = a Lead's file, 1.2.3 = Dev's comparison). ⚠ = written by the model, "
                   "not captured from a command, page or file: check those first. Turn Auto-refresh off while "
                   "editing.")
        st.dataframe(rows, width="stretch", hide_index=True)
        case = st.selectbox("Case", cases, key="evidence_case")
        evidence = read_evidence(root, case)
        edited = None
        if evidence.visual:
            st.image(str(evidence.path), caption=f"{EVIDENCE_DIR}/{evidence.path.name} -- {evidence.source}")
            compared = evidence_path(root, case, ".compare.png")
            if compared.is_file():
                st.image(str(compared), caption=f"{compared.name}: Dev's last comparison -- original | new | "
                                                f"differences in red")
        else:
            edited = st.text_area(f"{EVIDENCE_DIR}/{evidence.path.name}", evidence.path.read_text(encoding="utf-8"),
                                  height=260, key=f"evidence_text_{case}")
            result = evidence_path(root, case, ".result.txt")
            if result.is_file():
                st.caption(f"{result.name}: Dev's last comparison")
                st.code(result.read_text(encoding="utf-8"))
        accept, save, again = st.columns(3)
        message = None
        if accept.button("Accept", key=f"evidence_accept_{case}"):
            message = mark_reviewed(root, case)
        if edited is not None and save.button("Save edit", key=f"evidence_save_{case}"):
            message = save_edited_text(root, case, edited)
        if again.button("Re-capture", key=f"evidence_recapture_{case}"):
            message = recapture(engine, session_id, root, case)
        if message:
            (st.error if message.startswith("Error") else st.success)(message)


def _render_db_browser_tab(engine, selected_session_id: str) -> None:
    """Raw table view of .jfi/JFI.db -- a plain, mechanical browser (pick a
    table, see its rows) rather than a purpose-built view of any one of
    them; the Plan/log/review sections in the Session tab already cover
    the "make sense of this" job for the tables that need it. Shares its
    table registry/query logic with the fleet dashboard's own "Session DB"
    tab (see JFI.tool.db_browse) -- the two can never drift on which
    tables exist or how session-scoping works."""
    from JFI.tool.db_browse import query_table, table_registry

    table_name = st.selectbox("Table", sorted(table_registry()), key="db_browser_table")
    scoped = st.checkbox(f"Only rows for '{selected_session_id}'", value=True, key="db_browser_scoped")

    rows = query_table(engine, table_name, selected_session_id if scoped else None)

    if not rows:
        st.info("No rows.")
        return
    st.caption(f"{len(rows)} row(s)")
    st.dataframe(rows, width="stretch", hide_index=True)


def main() -> None:
    jfi_dir = _project_root() / ".jfi"
    db_engine = get_engine(_project_root())
    session_ids = _list_session_ids(db_engine)

    st.sidebar.title("📋 Just Finish It")
    if not session_ids:
        st.sidebar.info(f"No sessions found in {jfi_dir}/JFI.db")
        st.info(
            f"No sessions found in `{jfi_dir}/JFI.db`. Run `./jfi` from this same directory "
            "(or set SESSION_PATH to match) and enter a session name at its terminal prompt "
            "first -- that one initial prompt still has to happen there, since the session "
            "doesn't exist in the database (and this page's way of seeing it) until it's "
            "answered. Everything after that -- the goal, approvals, new requests -- can be "
            "typed here."
        )
        return

    selected_name = st.sidebar.selectbox("Session", session_ids)
    auto_refresh = st.sidebar.checkbox("Auto-refresh", value=True)
    refresh_seconds = st.sidebar.slider("Refresh every (seconds)", 1, 60, 20, disabled=not auto_refresh)

    st.title(selected_name)

    session_tab, db_tab = st.tabs(["Session", "Database"])

    with db_tab:
        _render_db_browser_tab(db_engine, selected_name)

    with session_tab:
        _render_session_tab(db_engine, jfi_dir, selected_name)

    if auto_refresh:
        time.sleep(refresh_seconds)
        st.rerun()


def _render_session_tab(db_engine, jfi_dir: Path, selected_name: str) -> None:
    # `.jfi/` is flat (only one JFI session can run per project at a time --
    # see SimpleSessionManager.__init__), so web_status.json/the answer
    # relay only ever describe whichever session is CURRENTLY live, not
    # necessarily the one selected above. Everything DB-backed below
    # (plan, review report, log) stays genuinely scoped to the selected
    # session_id regardless, since every row there is keyed by it.
    status = _load_status(jfi_dir)
    is_live = status is not None and status.get("session") == selected_name
    if status is None:
        st.warning(
            "No live JFI session running right now. Start `./jfi` with "
            "`JFI_WEB_BRIDGE=1` in the environment to get live phase/state/token updates and "
            "to approve prompts from here. Plan progress below still works without it."
        )
    elif not is_live:
        st.info(
            f"A different session ('{status.get('session')}') is the one currently running -- "
            "live status/approvals below apply to it, not to '" + selected_name + "'. "
            "Plan/log below still show the selected session's own data from the database."
        )
        _render_status(status)
    else:
        _render_pause_control(jfi_dir, status)
        awaiting = status.get("awaiting")
        if awaiting:
            _render_approval(jfi_dir, awaiting)
        else:
            _render_queue_input(jfi_dir)
        _render_status(status)

    st.subheader("Plan")
    session_id = selected_name
    judge_rows = plan_judge_rows(db_engine, session_id)
    if judge_rows:
        done, total = plan_progress_db(db_engine, session_id)
        if total:
            st.progress(done / total, text=f"{done}/{total} leaves done")
        pdone, ptotal = phase_progress_db(db_engine, session_id, "imp")
        if ptotal:
            st.caption(f"Implementation: {pdone}/{ptotal}")
        with st.expander("Full plan", expanded=status is None):
            st.markdown(render_plan_markdown(db_engine, session_id))
            st.markdown("**Task | Judge**")
            st.dataframe(judge_rows, width="stretch", hide_index=True)
        _render_node_detail(judge_rows)
        _render_runbook_and_design(db_engine, session_id)
        _render_evidence(db_engine, session_id)
    else:
        st.info("No plan yet for this session.")

    review_report = get_note(db_engine, session_id, REVIEW_REPORT)
    if review_report:
        st.subheader("⚠️ Latest review report (pending fixes)")
        st.markdown(review_report)

    log_events = _recent_log_events(db_engine, session_id, limit=200)
    if log_events:
        with st.expander("Recent activity (log tail, newest first)"):
            # Newest line first -- this panel re-renders from scratch on every
            # auto-refresh (see main()'s own st.rerun()), so a chronological
            # tail would put the newest activity at the BOTTOM, forcing a
            # re-scroll every refresh just to see what's new. Reversed, the
            # newest line is always visible at the top without scrolling.
            lines = [
                f"[{event.created_at.strftime('%H:%M:%S')}] {event.tag.upper()}: {line}"
                for event in log_events
                for line in event.text.splitlines()
            ]
            st.code("\n".join(reversed(lines)) or "(empty)")


main()
