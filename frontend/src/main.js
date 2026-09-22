// JFI Fleet frontend -- connects to jfi-master's /view WebSocket and
// renders live session state. No fabricated fields: everything shown here
// comes from AbstractManager.get_status_snapshot() (relayed verbatim by
// every session's SocketReporter) or is derived server-side in
// session_registry.py (the activity feed, first_seen/last_seen, online).
// There is no "model", "cost", or "files touched" in the real payload, so
// unlike the early design mockups this view never shows numbers nobody
// actually reported.

import { THEME_PRESETS, THEME_ORDER, THEME_LABELS, deriveTokens } from "./themes.js";

const PHASES = ["planner", "product_owner", "imp", "testing", "reviewer", "cleanup"];
const PHASE_LABELS = {
  planner: "Planner", product_owner: "Product Owner", imp: "Implement",
  testing: "Testing", reviewer: "Reviewer", cleanup: "Cleanup",
};
const DEFAULT_THEME = "dark-ocean";

const state = { sessions: {}, activity: [], connected: false };
let activeTab = "fleet";
let selectedKey = null;
let sessionSubTab = "overview"; // "overview" | "checklist" -- sub-tabs within the Session tab

// Must match JFI.tool.db_browse.table_registry()'s own key set (Python) --
// this is just the picklist of names for the "Session DB" tab below; the
// actual query logic lives entirely on the session side (see
// socket_reporter.py's _handle_db_query), this side never runs SQL itself.
const DB_TABLES = [
  "ActivityEvent", "BackgroundProcess", "ContextEntry", "DonePhase", "HistoryMessage",
  "ImplementedFile", "Leaf", "LogEvent", "QueuedItem", "SessionNote", "SessionRecord", "UnlockedTool",
];
let dbTable = DB_TABLES[0];
let dbScoped = true;
let dbRows = null;
let dbError = null;
let dbPendingRequestId = null;

const app = document.getElementById("app");

// ------------------------------------------------------------- theming

function loadTheme() {
  try {
    const saved = localStorage.getItem("jfi-theme");
    return saved && THEME_PRESETS[saved] ? saved : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

function applyTheme(id) {
  const preset = THEME_PRESETS[id] || THEME_PRESETS[DEFAULT_THEME];
  const tokens = deriveTokens(preset);
  const root = document.documentElement.style;
  for (const [prop, value] of Object.entries(tokens)) root.setProperty(prop, value);
  document.documentElement.dataset.theme = id;
  try {
    localStorage.setItem("jfi-theme", id);
  } catch {
    /* private window / blocked storage -- theme just won't persist */
  }
}

applyTheme(loadTheme());

// ------------------------------------------------------------- websocket

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/view`;
}

let viewSocket = null;

function connect() {
  const ws = new WebSocket(wsUrl());
  viewSocket = ws;

  ws.onopen = () => {
    state.connected = true;
    render();
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "snapshot") {
      state.sessions = msg.sessions || {};
      state.activity = msg.activity || [];
    } else if (msg.type === "session_update") {
      state.sessions[msg.key] = msg.entry;
    } else if (msg.type === "activity") {
      state.activity.push(msg.event);
      if (state.activity.length > 200) state.activity.shift();
    } else if (msg.type === "db_result") {
      // Answer to the "Session DB" tab's own query -- request_id guards
      // against a stale response landing after the user already picked a
      // different table (see requestDbRows). Paints #db-rows directly
      // instead of going through the generic render() -> renderSessionDbTab()
      // path: that path now deliberately skips rebuilding anything once
      // the shell already exists for this session (see dbShellFor), so it
      // would never actually show this result on its own.
      if (msg.request_id === dbPendingRequestId) {
        dbRows = msg.rows || null;
        dbError = msg.error || null;
        dbPendingRequestId = null;
      }
      paintDbRows();
      return;
    }
    render();
  };

  ws.onclose = () => {
    state.connected = false;
    if (viewSocket === ws) viewSocket = null;
    render();
    setTimeout(connect, 3000);
  };

  ws.onerror = () => ws.close();
}

// Sends {type:"control", key, action, ...} to master.js, which relays it to
// the named session's own /report socket -- see socket_reporter.py's
// _dispatch_control on the receiving end. `answerKey` (not `key`) carries
// a get_user_choice response like "r"/"s"; `key` here is always the
// TARGET SESSION's identity, never conflated with that.
function sendControl(sessionKey, action, extra = {}) {
  if (!viewSocket || viewSocket.readyState !== WebSocket.OPEN) return;
  viewSocket.send(JSON.stringify({ type: "control", key: sessionKey, action, ...extra }));
}

// ------------------------------------------------------------- helpers

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function relTime(unixSeconds) {
  if (!unixSeconds) return "—";
  const diff = Date.now() / 1000 - unixSeconds;
  if (diff < 5) return "now";
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

function duration(unixSeconds) {
  if (!unixSeconds) return "—";
  const diff = Date.now() / 1000 - unixSeconds;
  const h = Math.floor(diff / 3600);
  const m = Math.floor((diff % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function fmtClock(unixSeconds) {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function sessionEntries() {
  return Object.values(state.sessions).sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0));
}

// ------------------------------------------------------------- render: chrome
//
// The app shell (header/connection-status/theme-select/tab-bar) is built
// ONCE and never innerHTML-replaced again -- only #tab-content is. This
// isn't just tidiness: a live WebSocket message can land at any moment,
// including while the theme <select> is open, and replacing a <select>'s
// own DOM node closes its dropdown out from under whoever just clicked it
// (observed in practice). Data-driven pieces of the shell (connection
// text, online/awaiting counts, active tab, the "Session · <name>" label)
// are updated via direct DOM mutation instead, which never disturbs focus
// or an open picker on an element that wasn't itself replaced.

let shellBuilt = false;
let elConnStatus, elConnText, elConnCount, elThemeSelect, elTabContent;
// `${selectedKey}:${sessionSubTab}` the Session tab's own skeleton (see
// paintSection above) was last built for -- a mismatch means the whole
// skeleton needs rebuilding (a different session or sub-tab was picked),
// a match means only the sections whose data actually changed get touched.
let sessionShellFor = null;

function buildShell() {
  app.innerHTML = `
    <div class="app-head">
      <div class="brand"><span class="dot"></span><h1>jfi fleet</h1><span class="sub">// session app</span></div>
      <div class="head-right">
        <span class="conn-status" id="conn-status"><span class="dot"></span><span id="conn-text"></span></span>
        <span class="conn-status" id="conn-count"></span>
        <select class="theme-select" id="theme-select">
          ${THEME_ORDER.map((id) => `<option value="${id}" ${loadTheme() === id ? "selected" : ""}>${THEME_LABELS[id]}</option>`).join("")}
        </select>
      </div>
    </div>
    <div class="tab-bar">
      <button class="tab-btn" data-tab="fleet">Fleet</button>
      <button class="tab-btn" data-tab="activity">Activity</button>
      <button class="tab-btn" data-tab="session">Session</button>
      <button class="tab-btn" data-tab="session-db">Session DB</button>
    </div>
    <div class="tab-panels" id="tab-content"></div>
    <footer>jfi fleet — live via WebSocket, no filesystem access between master and sessions</footer>
  `;

  elConnStatus = document.getElementById("conn-status");
  elConnText = document.getElementById("conn-text");
  elConnCount = document.getElementById("conn-count");
  elThemeSelect = document.getElementById("theme-select");
  elTabContent = document.getElementById("tab-content");

  elThemeSelect.addEventListener("change", () => applyTheme(elThemeSelect.value));
  app.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      activeTab = btn.dataset.tab;
      render();
    });
  });
}

// ------------------------------------------------- render: section diffing
//
// The Session tab carries several scrollable/stateful panels (the live log,
// the plan checklist) that live INSIDE #tab-content -- so even though the
// app shell above survives every render, a naive "replace #tab-content's
// whole innerHTML on every WebSocket tick" still tore those panels down and
// rebuilt them from scratch every time ANY field on the session changed,
// snapping the reader's scroll position back to the top constantly (a WS
// tick lands every few seconds on an active session). Fix: the Session
// tab's own skeleton (one fixed-id wrapper <div> per panel) is built once
// per (session, sub-tab) pair, and each panel's content is only actually
// repainted when the SPECIFIC data it depends on changes -- a fingerprint
// (JSON.stringify of just that slice) is compared against the last-painted
// one, so a token counter ticking up somewhere else never touches the log
// or checklist DOM node at all. Fleet/Activity stay simple full-replace:
// they're plain list views with nothing scrollable/stateful to protect.
const sectionCache = {};

function paintSection(id, fingerprint, renderHtml, { preserveScrollSelector, afterPaint } = {}) {
  const el = document.getElementById(id);
  if (!el || sectionCache[id] === fingerprint) return;
  sectionCache[id] = fingerprint;

  let savedScroll = null;
  if (preserveScrollSelector) {
    const scrollEl = el.querySelector(preserveScrollSelector);
    // Only a MEANINGFUL scroll (the reader moved away from the top) is worth
    // restoring -- staying at 0 just means "still following the live tail."
    if (scrollEl && scrollEl.scrollTop > 0) savedScroll = scrollEl.scrollTop;
  }

  el.innerHTML = renderHtml();

  if (savedScroll != null) {
    const scrollEl = el.querySelector(preserveScrollSelector);
    if (scrollEl) scrollEl.scrollTop = Math.min(savedScroll, scrollEl.scrollHeight);
  }
  if (afterPaint) afterPaint(el);
}

function render() {
  if (!shellBuilt) {
    buildShell();
    shellBuilt = true;
  }

  const entries = sessionEntries();
  const online = entries.filter((e) => e.online);
  const awaiting = online.filter((e) => e.data && e.data.awaiting);

  elConnStatus.className = `conn-status ${state.connected ? "live" : "down"}`;
  elConnText.textContent = state.connected ? "connected" : "reconnecting…";
  elConnCount.textContent = `${online.length} online · ${awaiting.length} awaiting`;

  app.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === activeTab);
    if (btn.dataset.tab === "session") {
      btn.textContent = selectedKey ? `Session · ${state.sessions[selectedKey]?.data?.session || selectedKey}` : "Session";
    }
  });

  if (activeTab === "session") {
    dbShellFor = null; // leaving the Session DB tab invalidates its shell so returning rebuilds it fresh
    renderSessionTab(entries);
  } else if (activeTab === "session-db") {
    sessionShellFor = null; // leaving the Session tab invalidates its shell so returning rebuilds it fresh
    renderSessionDbTab();
  } else {
    sessionShellFor = null; // leaving the tab invalidates the shell so returning rebuilds it fresh
    dbShellFor = null; // ditto for the Session DB tab
    elTabContent.innerHTML = activeTab === "fleet" ? renderFleetTab(entries) : renderActivityTab();
    elTabContent.querySelectorAll(".card").forEach((card) => {
      card.addEventListener("click", () => {
        selectedKey = card.dataset.key;
        activeTab = "session";
        render();
      });
    });
  }
}

// ------------------------------------------------------------- render: fleet

function phaseDistribution(entries) {
  const online = entries.filter((e) => e.online);
  const counts = {};
  for (const p of PHASES) counts[p] = 0;
  for (const e of online) {
    const phase = e.data?.phase;
    if (phase && counts[phase] !== undefined) counts[phase]++;
  }
  const total = online.length || 1;
  return { counts, total };
}

function renderFleetTab(entries) {
  if (entries.length === 0) {
    return `<p class="empty-note">No sessions have reported yet. Start one with MASTER_WS_URL set to this master's /report URL.</p>`;
  }

  const { counts, total } = phaseDistribution(entries);
  const chart = `
    <div class="fchart">
      <div class="fc-title">Sessions by phase</div>
      <div class="phase-dist">
        ${PHASES.map((p) => (counts[p] ? `<div class="pd-seg ${p}" style="width:${(counts[p] / total) * 100}%">${counts[p]}</div>` : "")).join("")}
      </div>
      <div class="pd-legend">
        ${PHASES.map((p) => `<span><span class="sw" style="background:var(--${p === "planner" ? "amber" : p === "product_owner" ? "purple" : p === "imp" ? "accent" : p === "testing" ? "blue" : p === "reviewer" ? "red" : "ink-faint"})"></span>${PHASE_LABELS[p]}</span>`).join("")}
      </div>
    </div>
  `;

  const online = entries.filter((e) => e.online);
  const offline = entries.filter((e) => !e.online);
  const awaiting = online.filter((e) => e.data?.awaiting);
  const rest = online.filter((e) => !e.data?.awaiting);

  const streaming = online.filter((e) => e.data?.state === "streaming").length;

  const stats = `
    <div class="stat-row">
      <div class="stat"><div class="label">Sessions</div><div class="value">${entries.length}</div></div>
      <div class="stat"><div class="label">Online</div><div class="value accent">${online.length}</div></div>
      <div class="stat"><div class="label">Streaming</div><div class="value">${streaming}</div></div>
      <div class="stat"><div class="label">Awaiting</div><div class="value red">${awaiting.length}</div></div>
      <div class="stat"><div class="label">Offline</div><div class="value">${offline.length}</div></div>
    </div>
  `;

  let body = stats + chart;

  if (awaiting.length) {
    body += `<p class="section-label">needs attention</p><div class="grid">${awaiting.map(renderCard).join("")}</div>`;
  }
  for (const phase of PHASES) {
    const inPhase = rest.filter((e) => e.data?.phase === phase);
    if (inPhase.length) {
      body += `<p class="section-label">${PHASE_LABELS[phase]} — ${inPhase.length}</p><div class="grid">${inPhase.map(renderCard).join("")}</div>`;
    }
  }
  const noPhase = rest.filter((e) => !e.data?.phase || !PHASES.includes(e.data.phase));
  if (noPhase.length) {
    body += `<p class="section-label">other</p><div class="grid">${noPhase.map(renderCard).join("")}</div>`;
  }
  if (offline.length) {
    body += `<p class="section-label">offline</p><div class="grid">${offline.map(renderCard).join("")}</div>`;
  }
  return body;
}

function renderCard(entry) {
  const d = entry.data || {};
  const awaiting = d.awaiting;
  const phases = d.phases || PHASES;
  const doneSet = new Set(d.done_phases || []);
  const isWait = !!awaiting;
  const plan = d.plan; // [done, total] or null
  const phasePlan = d.phase_plan;
  const tokens = d.tokens; // [used, budget] or null

  return `
    <div class="card ${isWait ? "alert" : ""} ${!entry.online ? "offline" : ""}" data-key="${esc(entry.key)}">
      ${isWait ? `<div class="alert-banner">⛔ ${esc(awaiting.prompt || "awaiting input")}</div>` : ""}
      <div class="card-head">
        <div>
          <div class="session-name">${esc(d.session || entry.key)}</div>
          <div class="repo-path">${esc(entry.repo || "")}</div>
        </div>
        <span class="badge ${entry.online ? esc((d.state || "").split(" ")[0]) : "offline"}">${entry.online ? esc(d.state || "—") : "offline"}</span>
      </div>
      <div class="stepper">
        ${phases.map((p) => `<div class="step ${doneSet.has(p) ? "done" : p === d.phase ? `current ${isWait ? "wait" : ""}` : ""}"></div>`).join("")}
      </div>
      <div class="step-labels">
        ${phases.map((p) => `<span class="${p === d.phase ? `on ${isWait ? "wait" : ""}` : ""}">${p}</span>`).join("")}
      </div>
      <div class="task-line">${d.task ? `<span class="tag">[${esc(d.stage || d.phase || "")}]</span> ${esc(d.task)}` : "—"}</div>
      <div class="bars">
        ${phasePlan ? `<div class="bar-row"><span class="bl">phase</span><div class="bar-track"><div class="bar-fill" style="width:${(phasePlan[0] / (phasePlan[1] || 1)) * 100}%"></div></div><span class="bv">${phasePlan[0]}/${phasePlan[1]}</span></div>` : ""}
        ${plan ? `<div class="bar-row"><span class="bl">total</span><div class="bar-track"><div class="bar-fill" style="width:${(plan[0] / (plan[1] || 1)) * 100}%"></div></div><span class="bv">${plan[0]}/${plan[1]}</span></div>` : ""}
        ${tokens ? `<div class="bar-row"><span class="bl">tokens</span><div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, (tokens[0] / (tokens[1] || 1)) * 100)}%"></div></div><span class="bv">${tokens[0].toLocaleString()}</span></div>` : ""}
      </div>
      <div class="card-foot"><span>iter ${d.iteration ?? "—"}${(d.background_processes || []).length ? ` · ${d.background_processes.length} bg proc` : ""}</span><span>${relTime(entry.last_seen)}</span></div>
    </div>
  `;
}

// ------------------------------------------------------------- render: activity

function renderActivityTab() {
  if (state.activity.length === 0) {
    return `<p class="empty-note">No activity yet — events appear here as sessions report phase changes, ticks, and prompts.</p>`;
  }
  const sevClass = { good: "sev-good", warn: "sev-warn", bad: "sev-bad", info: "" };
  const items = [...state.activity].reverse();
  return `
    <div class="feed">
      ${items
        .map(
          (ev) => `
        <div class="ev ${sevClass[ev.severity] || ""}">
          <div class="ev-row">
            <span class="ev-time">${fmtClock(ev.ts)}</span>
            <span class="ev-sess">${esc(ev.key)}</span>
            <span class="ev-text">${esc(ev.text)}</span>
          </div>
        </div>`
        )
        .join("")}
    </div>
  `;
}

// ------------------------------------------------------------- render: session db
//
// A plain, mechanical table browser for the selected session's own
// .jfi/JFI.db -- pick a table, optionally scope to just this session's own
// rows, Refresh. No purpose-built rendering of any one table; the Session
// tab's own checklist/log panels already cover the "make sense of this"
// job for the tables that need it. The query itself never runs here --
// see requestDbRows/sendControl and socket_reporter.py's _handle_db_query
// on the other end.

// `dbShellFor` mirrors `sessionShellFor`'s own pattern exactly (see
// paintSection's comment on the Session tab, and buildShell's own comment
// on the theme <select> for the original instance of this failure): the
// shell -- the <select>/checkbox/button controls -- is built ONCE per
// session and never touched again by an ordinary re-render. Every
// "session_update"/"activity" WebSocket message calls render(), which used
// to call renderSessionDbTab() and replace the WHOLE tab's innerHTML EVERY
// time (about once a second while a session is actively reporting) --
// destroying and recreating the <select> out from under anyone trying to
// click it -- the exact same failure the theme <select> already hit once.
let dbShellFor = null; // selectedKey the Session DB tab's shell was last built for

function renderSessionDbTab() {
  const entry = selectedKey && state.sessions[selectedKey];
  if (!entry) {
    dbShellFor = null;
    elTabContent.innerHTML = `<p class="empty-note">Select a session from the Fleet tab first, then come back here to browse its database.</p>`;
    return;
  }

  if (dbShellFor !== selectedKey) {
    elTabContent.innerHTML = `
      <div class="panel">
        <div class="panel-head">Session DB · ${esc(entry.data?.session || entry.key)}</div>
        <div class="panel-body">
          <div class="db-controls">
            <select id="db-table-select">
              ${DB_TABLES.map((t) => `<option value="${esc(t)}" ${t === dbTable ? "selected" : ""}>${esc(t)}</option>`).join("")}
            </select>
            <label class="db-scope-label"><input type="checkbox" id="db-scoped-checkbox" ${dbScoped ? "checked" : ""}/> only this session's rows</label>
            <button class="opt-btn" id="db-refresh-btn">Refresh</button>
          </div>
          <div id="db-rows"></div>
        </div>
      </div>
    `;
    dbShellFor = selectedKey;

    document.getElementById("db-table-select").addEventListener("change", (e) => {
      dbTable = e.target.value;
      dbRows = null;
      dbError = null;
      paintDbRows();
    });
    document.getElementById("db-scoped-checkbox").addEventListener("change", (e) => {
      dbScoped = e.target.checked;
    });
    document.getElementById("db-refresh-btn").addEventListener("click", () => requestDbRows(entry.key));

    paintDbRows();
  }

  // Cheap property mutation, not a node replacement -- safe to run on
  // every render() tick without disturbing an open <select>.
  const refreshBtn = document.getElementById("db-refresh-btn");
  if (refreshBtn) refreshBtn.disabled = !entry.online;
}

function paintDbRows() {
  const el = document.getElementById("db-rows");
  if (!el) return;
  const entry = selectedKey && state.sessions[selectedKey];
  if (dbRows === null) {
    el.innerHTML = `<p class="empty-note">${entry?.online ? "Pick a table and click Refresh." : "This session is offline — no live connection to query its database."}</p>`;
  } else if (dbError) {
    el.innerHTML = `<p class="empty-note">Error: ${esc(dbError)}</p>`;
  } else if (dbRows.length === 0) {
    el.innerHTML = `<p class="empty-note">No rows.</p>`;
  } else {
    el.innerHTML = renderDbTable(dbRows);
  }
}

function renderDbTable(rows) {
  const columns = Object.keys(rows[0]);
  return `
    <div class="db-table-wrap">
      <table class="db-table">
        <thead><tr>${columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
        <tbody>
          ${rows.map((row) => `<tr>${columns.map((c) => `<td>${esc(formatCell(row[c]))}</td>`).join("")}</tr>`).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function formatCell(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function requestDbRows(sessionKey) {
  dbPendingRequestId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  sendControl(sessionKey, "db_query", { table: dbTable, scoped: dbScoped, request_id: dbPendingRequestId });
}

// ------------------------------------------------------------- render: session detail
//
// renderSessionTab builds the skeleton (see paintSection's own docstring
// above) once per (session, sub-tab) pair, then repaints only the sections
// whose own data changed on every subsequent call for the SAME session.

function renderSessionTab(entries) {
  const entry = selectedKey && state.sessions[selectedKey];
  const shellFor = `${selectedKey}:${sessionSubTab}`;

  if (!entry) {
    if (sessionShellFor !== null) {
      elTabContent.innerHTML = `<p class="empty-note">Select a session from the Fleet tab to see its detail here.</p>`;
      sessionShellFor = null;
    }
    return;
  }

  if (sessionShellFor !== shellFor) {
    elTabContent.innerHTML = `
      <div id="sd-head"></div>
      <div id="sd-controls"></div>
      <div id="sd-subtabs"></div>
      ${sessionSubTab === "checklist"
        ? `<div id="sd-checklist"></div>`
        : `
          <div id="sd-timeline"></div>
          <div id="sd-progress"></div>
          <div id="sd-awaiting"></div>
          <div id="sd-current-task"></div>
          <div id="sd-token-detail"></div>
          <div id="sd-heatmap"></div>
          <div id="sd-processes"></div>
          <div id="sd-log"></div>
        `}
    `;
    sessionShellFor = shellFor;
    for (const k of Object.keys(sectionCache)) delete sectionCache[k];
  }

  const d = entry.data || {};
  const plan = d.plan;
  const phasePlan = d.phase_plan;
  const tokens = d.tokens;

  const sessionKey = entry.key;

  paintSection(
    "sd-controls",
    JSON.stringify([d.is_paused, d.queued_items, entry.online]),
    () => renderControlsPanel(d, entry.online),
    {
      afterPaint: (el) => {
        const pauseBtn = el.querySelector("[data-ctl='pause-toggle']");
        if (pauseBtn) {
          pauseBtn.addEventListener("click", () => sendControl(sessionKey, d.is_paused ? "resume" : "pause"));
        }
        const stopBtn = el.querySelector("[data-ctl='stop']");
        if (stopBtn) {
          stopBtn.addEventListener("click", () => {
            if (confirm(`Stop "${d.session || sessionKey}"? Progress is saved; it can be resumed later.`)) {
              sendControl(sessionKey, "stop");
            }
          });
        }
        const form = el.querySelector(".ctl-queue-form");
        if (form) {
          form.addEventListener("submit", (ev) => {
            ev.preventDefault();
            const input = form.querySelector(".ctl-queue-input");
            const text = input.value.trim();
            if (!text) return;
            sendControl(sessionKey, "queue", { text });
            input.value = "";
          });
        }
      },
    }
  );

  paintSection(
    "sd-head",
    JSON.stringify([d.session, entry.key, entry.repo, entry.online, entry.first_seen, tokens, d.iteration, d.queue_size]),
    () => `
      <div class="sd-head">
        <div>
          <h2>${esc(d.session || entry.key)}</h2>
          <div class="path">${esc(entry.repo || "")} · ${entry.online ? "online" : `offline (last seen ${relTime(entry.last_seen)})`}</div>
        </div>
        <div class="sd-stats">
          <div class="hstat"><div class="l">Watched</div><div class="v">${duration(entry.first_seen)}</div></div>
          <div class="hstat"><div class="l">Tokens</div><div class="v amber">${tokens ? `${tokens[0].toLocaleString()}/${tokens[1].toLocaleString()}` : "—"}</div></div>
          <div class="hstat"><div class="l">Iteration</div><div class="v">${d.iteration ?? "—"}</div></div>
          <div class="hstat"><div class="l">Queued</div><div class="v">${d.queue_size ?? "—"}</div></div>
        </div>
      </div>
    `
  );

  paintSection(
    "sd-subtabs",
    JSON.stringify([sessionSubTab, plan]),
    () => `
      <div class="subtab-bar">
        <button class="subtab-btn ${sessionSubTab === "overview" ? "active" : ""}" data-subtab="overview">Overview</button>
        <button class="subtab-btn ${sessionSubTab === "checklist" ? "active" : ""}" data-subtab="checklist">Plan checklist${plan ? ` · ${plan[0]}/${plan[1]}` : ""}</button>
      </div>
    `,
    {
      afterPaint: (el) => {
        el.querySelectorAll(".subtab-btn").forEach((btn) => {
          btn.addEventListener("click", () => {
            sessionSubTab = btn.dataset.subtab;
            render();
          });
        });
      },
    }
  );

  if (sessionSubTab === "checklist") {
    paintSection(
      "sd-checklist",
      JSON.stringify([d.plan_markdown, d.task, d.task_started_at, d.task_history, d.phase, checklistPath]),
      () => renderPlanChecklist(d.plan_markdown, d.task, d.task_started_at, d.task_history, d.phase),
      {
        preserveScrollSelector: ".pc-cards-wrap",
        afterPaint: (el) => {
          el.querySelectorAll("[data-pc-depth]").forEach((btn) => {
            btn.addEventListener("click", () => {
              const depth = Number(btn.dataset.pcDepth);
              checklistPath[depth] = btn.dataset.pcValue;
              checklistPath.length = depth + 1;
              render();
            });
          });
          el.querySelectorAll("[data-pc-crumb]").forEach((btn) => {
            btn.addEventListener("click", () => {
              checklistPath.length = Number(btn.dataset.pcCrumb) + 1;
              render();
            });
          });
        },
      }
    );
    return;
  }

  const phases = d.phases || PHASES;
  const doneSet = new Set(d.done_phases || []);

  paintSection(
    "sd-timeline",
    JSON.stringify([phases, d.done_phases, d.phase, d.stage, d.state]),
    () => `
      <div class="timeline">
        ${phases
          .map((p) => {
            const isDone = doneSet.has(p);
            const isCurrent = p === d.phase;
            return `
            <div class="tl-phase ${isDone ? "done" : isCurrent ? "current" : ""}">
              <div class="n">${PHASE_LABELS[p] || p}</div>
              <div class="t">${isDone ? "complete" : isCurrent ? esc(d.stage ? `${d.stage} · ${d.state}` : d.state || "in progress") : "pending"}</div>
            </div>`;
          })
          .join("")}
      </div>
    `
  );

  paintSection(
    "sd-progress",
    JSON.stringify([phasePlan, plan, tokens]),
    () => `
      <div class="bars sd-progress">
        ${phasePlan ? `<div class="bar-row"><span class="bl">phase</span><div class="bar-track"><div class="bar-fill" style="width:${(phasePlan[0] / (phasePlan[1] || 1)) * 100}%"></div></div><span class="bv">${phasePlan[0]}/${phasePlan[1]}</span></div>` : ""}
        ${plan ? `<div class="bar-row"><span class="bl">total</span><div class="bar-track"><div class="bar-fill" style="width:${(plan[0] / (plan[1] || 1)) * 100}%"></div></div><span class="bv">${plan[0]}/${plan[1]}</span></div>` : ""}
        ${tokens ? `<div class="bar-row"><span class="bl">context</span><div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, (tokens[0] / (tokens[1] || 1)) * 100)}%"></div></div><span class="bv">${tokens[0].toLocaleString()}/${tokens[1].toLocaleString()}</span></div>` : ""}
      </div>
    `
  );

  paintSection(
    "sd-awaiting",
    JSON.stringify(d.awaiting || null),
    () =>
      d.awaiting
        ? `
      <div class="panel approval">
        <div class="panel-head"><span>⛔ Awaiting input</span><span class="n">answer from here or the session's own terminal</span></div>
        <div class="panel-body">
          <p style="margin:0 0 10px;color:var(--ink-dim);font-size:12px;">${esc(d.awaiting.prompt || "")}</p>
          ${(d.awaiting.options || []).length ? `<div class="approval-opts">${d.awaiting.options.map((o) => `<button class="opt-btn" data-answer-key="${esc(o.key)}">${esc(o.label)}</button>`).join("")}</div><p style="margin:10px 0 0;color:var(--ink-faint);font-size:10.5px;">Whichever answers first wins — the terminal isn't locked out by answering here.</p>` : ""}
        </div>
      </div>`
        : "",
    {
      afterPaint: (el) => {
        el.querySelectorAll("[data-answer-key]").forEach((btn) => {
          btn.addEventListener("click", () => sendControl(sessionKey, "answer", { answerKey: btn.dataset.answerKey }));
        });
      },
    }
  );

  paintSection(
    "sd-current-task",
    JSON.stringify([d.phase, d.task, d.task_started_at]),
    () => `
      <div class="panel">
        <div class="panel-head"><span>Current task</span><span class="n">${esc(d.phase || "")}</span></div>
        <div class="panel-body" style="font-size:12.5px;color:var(--ink-dim);">
          ${d.task ? esc(d.task) : "—"}
          ${d.task && d.task_started_at
            ? `<div class="ct-timing"><span>Started ${fmtClock(d.task_started_at)}</span><span class="ct-running">Running ${fmtDuration(Date.now() / 1000 - d.task_started_at)}</span></div>`
            : ""}
        </div>
      </div>
    `
  );

  paintSection(
    "sd-token-detail",
    JSON.stringify([d.tokens_read, d.tokens_written]),
    () => `
      <div class="panel">
        <div class="panel-head"><span>Token detail</span><span class="n">this session</span></div>
        <div class="panel-body" style="font-size:12px;color:var(--ink-dim);display:flex;gap:24px;flex-wrap:wrap;">
          <span>read: ${(d.tokens_read ?? 0).toLocaleString()}</span>
          <span>written: ${(d.tokens_written ?? 0).toLocaleString()}</span>
        </div>
      </div>
    `
  );

  paintSection("sd-heatmap", JSON.stringify(d.task_history), () => renderTaskHeatmap(d.task_history));
  paintSection("sd-processes", JSON.stringify(d.background_processes), () => renderProcessPanel(d.background_processes));
  paintSection("sd-log", JSON.stringify(d.log_tail), () => renderLogPanel(d.log_tail), { preserveScrollSelector: ".log-view" });
}

// Pause/resume/queue/stop -- sent as {type:"control", key, action, ...}
// over this browser's own /view socket; master.js relays it to the named
// session's /report socket, which dispatches it into the exact same
// AbstractManager methods a local keypress already drives (see
// socket_reporter.py's _dispatch_control). Offline sessions have no live
// socket to relay to, so every control here is disabled while `online` is
// false rather than silently doing nothing on click.
function renderControlsPanel(d, online) {
  const queued = d.queued_items || [];
  const disabled = online ? "" : "disabled";
  return `
    <div class="panel controls-panel">
      <div class="panel-body ctl-row">
        <button class="ctl-btn ${d.is_paused ? "ctl-active" : ""}" data-ctl="pause-toggle" ${disabled}>
          ${d.is_paused ? "▶ Resume" : "⏸ Pause"}
        </button>
        <button class="ctl-btn ctl-danger" data-ctl="stop" ${disabled}>⏹ Stop</button>
        <form class="ctl-queue-form">
          <input type="text" class="ctl-queue-input" placeholder="Queue a follow-up…" ${disabled}>
          <button type="submit" class="ctl-btn" ${disabled}>Queue</button>
        </form>
        ${!online ? `<span class="ctl-offline-note">offline — no live connection to control</span>` : ""}
      </div>
      ${queued.length ? `
        <div class="panel-body ctl-queued">
          ${queued.map((text, i) => `<div class="ctl-queued-item"><span class="ctl-queued-num">#${i + 1}</span>${esc(text)}</div>`).join("")}
        </div>` : ""}
    </div>
  `;
}

function fmtDuration(seconds) {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return s ? `${m}m ${s}s` : `${m}m`;
}

// Which checklist SECTION a line falls under, keyed to match record_task_
// tokens' own `phase` values (imp/testing) -- see PHASE_SECTION in
// simple_session_manager.py, the backend's own source of truth for this
// exact mapping.
const CHECKLIST_SECTION_PHASE = { "## implementation": "imp", "## testing": "testing" };

// Parses EVERY bulleted line in plan.md -- both checkbox leaves ("- [ ] 1.1
// ...") and plain parent bullets with no checkbox ("- 1. ..."). Per the
// planner's own format rules, only leaves ever carry a checkbox; a parent
// stays a plain bullet for its whole life -- so a parent's own label/done-
// state has to come from aggregating its children, not from itself. Depth
// comes from each item's OWN number's dot-count (e.g. "5.6.1" -> depth 2,
// same rule tool/plan_renumber.py uses), not raw indentation, which can
// drift from the intended nesting.
function parsePlanLines(markdown) {
  if (!markdown) return [];
  // A top-level PARENT bullet is written "- 1. Description" (number, then a
  // literal period, then the text) -- that trailing period must be dropped,
  // not captured as part of the number, or "1." can never match as an
  // ancestor prefix of a real leaf like "1.1.1" (observed: every single
  // parent number came through with a trailing dot, so NOTHING nested and
  // the whole tree flattened into dozens of bogus top-level roots).
  const bulletRe = /^\s*-\s(?:\[( |x|X)\]\s+)?(\d+(?:\.\d+)*)\.?\s+(.*)$/;
  const items = [];
  let sectionPhase = null;
  for (const raw of markdown.split("\n")) {
    const heading = raw.trim().toLowerCase();
    if (heading in CHECKLIST_SECTION_PHASE) {
      sectionPhase = CHECKLIST_SECTION_PHASE[heading];
      continue;
    }
    const m = raw.match(bulletRe);
    if (!m || !sectionPhase) continue;
    const [, mark, number, desc] = m;
    const depth = (number.match(/\./g) || []).length;
    items.push({
      number, desc: desc.trim(), depth, phase: sectionPhase,
      isLeaf: mark !== undefined, done: mark ? mark.toLowerCase() === "x" : false,
    });
  }
  return items;
}

// Rebuilds the tree structure from parsePlanLines' flat, depth-tagged list --
// document order already puts a parent immediately before its own children,
// so a running "last node seen at each depth" stack is enough to attach
// each item under its real parent, per phase ("## Implementation" and
// "## Testing" are SEPARATELY-numbered trees, so the same number can
// legitimately exist in both -- kept as two independent root sets).
function buildPlanTree(markdown) {
  const roots = {}; // phase -> { number: node }
  const stack = []; // stack[d] = last node seen at depth d, for the CURRENT phase
  for (const item of parsePlanLines(markdown)) {
    if (item.phase !== stack.phase) {
      stack.length = 0;
      stack.phase = item.phase;
    }
    const node = { ...item, children: {} };
    // Nearest ancestor already on the stack whose OWN number is a real
    // dot-prefix of this item's number -- not just "whatever happens to
    // sit at depth-1". A leaf numbered deeper than its nearest WRITTEN
    // ancestor (e.g. "1.1.1" directly under a "1" bullet, with no separate
    // "1.1" bullet ever written -- a real shape, not just malformed input)
    // still nests correctly instead of becoming a bogus extra root that
    // silently mixes into a shallower level's own leaf list.
    let parent = null;
    for (let d = item.depth - 1; d >= 0; d--) {
      const candidate = stack[d];
      if (candidate && item.number.startsWith(candidate.number + ".")) {
        parent = candidate;
        break;
      }
    }
    if (parent) {
      parent.children[item.number] = node;
    } else {
      (roots[item.phase] ||= {})[item.number] = node;
    }
    stack[item.depth] = node;
    stack.length = item.depth + 1;
  }
  return roots;
}

function sortByNumber(nodes) {
  return nodes.slice().sort((a, b) => {
    const pa = a.number.split(".").map(Number);
    const pb = b.number.split(".").map(Number);
    for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
      const d = (pa[i] || 0) - (pb[i] || 0);
      if (d) return d;
    }
    return 0;
  });
}

// A parent's own done/total is never stored on it directly (it has no
// checkbox) -- always the sum over its leaf descendants.
function subtreeCounts(node) {
  const kids = Object.values(node.children);
  if (!kids.length) return [node.done ? 1 : 0, 1];
  let done = 0;
  let total = 0;
  for (const kid of kids) {
    const [d, t] = subtreeCounts(kid);
    done += d;
    total += t;
  }
  return [done, total];
}

// A ring meter, not a numeral: the circumference of r=15.9155 on a 36x36
// viewBox is exactly 100 units, so a percentage can drive stroke-dasharray
// directly with no separate scaling math.
function ringChart(pct, label, sub, extraClass = "") {
  const clamped = Math.max(0, Math.min(100, pct));
  return `
    <div class="pc-ring-card ${extraClass}">
      <svg class="pc-ring" viewBox="0 0 36 36">
        <circle class="pc-ring-track" cx="18" cy="18" r="15.9155" />
        <circle class="pc-ring-fill" cx="18" cy="18" r="15.9155"
          stroke-dasharray="${clamped} ${100 - clamped}" stroke-dashoffset="25" />
        <text x="18" y="20.5" class="pc-ring-pct">${Math.round(clamped)}%</text>
      </svg>
      <div class="pc-ring-labels">
        <div class="pc-ring-label">${esc(label)}</div>
        <div class="pc-ring-sub">${esc(sub)}</div>
      </div>
    </div>
  `;
}

function truncate(text, n) {
  return text.length > n ? text.slice(0, n - 1).trimEnd() + "…" : text;
}

// Which drill-down level is currently selected, as a path of number
// strings with the phase key first, e.g. ["imp", "2", "2.1"] -- module
// state (not per-session) is fine since renderSessionTab resets it
// whenever a different session/sub-tab is selected (see sectionCache's own
// reset alongside it), the same lifecycle sessionSubTab itself already has.
let checklistPath = [];

// One drill-down level's tab row: branches (drill further, shown with a
// done/total badge) and leaf siblings (already atomic, shown with their
// own status icon instead) rendered together, in that order -- both use
// the SAME data-pc-depth/data-pc-value attributes the click handler
// already generically handles, so selecting a leaf tab needs no separate
// code path from selecting a branch tab.
function renderLevelTabRow(branches, leaves, selectedNumber, dataDepth) {
  const branchTabs = branches.map((n) => {
    const [d, t] = subtreeCounts(n);
    return `<button class="pc-tab ${n.number === selectedNumber ? "active" : ""}" data-pc-depth="${dataDepth}" data-pc-value="${esc(n.number)}">${esc(n.number)} ${esc(truncate(n.desc, 28))} <span class="pc-tab-badge">${d}/${t}</span></button>`;
  });
  const leafTabs = leaves.map(
    (n) =>
      `<button class="pc-tab pc-tab-leaf ${n.number === selectedNumber ? "active" : ""}" data-pc-depth="${dataDepth}" data-pc-value="${esc(n.number)}">${n.done ? "✓" : "○"} ${esc(n.number)} ${esc(truncate(n.desc, 28))}</button>`
  );
  return `<div class="pc-tab-row">${branchTabs.join("")}${leafTabs.join("")}</div>`;
}

function renderLeafCard(node, currentPhase, currentNumber, currentTaskStartedAt, historyByKey) {
  const isCurrent = !node.done && currentPhase === node.phase && currentNumber && node.number === currentNumber;
  let timing = "";
  if (isCurrent && currentTaskStartedAt) {
    timing = `
      <span class="pc-time pc-time-running">● ${fmtDuration(Date.now() / 1000 - currentTaskStartedAt)}</span>
      <span class="pc-time-range">since ${fmtClock(currentTaskStartedAt)}</span>
    `;
  } else {
    const h = historyByKey[`${node.phase || ""}:${node.number}`];
    if (h && h.started_at && h.ended_at) {
      timing = `
        <span class="pc-time">${fmtDuration(h.ended_at - h.started_at)}</span>
        <span class="pc-time-range">${fmtClock(h.started_at)} → ${fmtClock(h.ended_at)}</span>
      `;
    }
  }
  return `
    <div class="pc-card ${node.done ? "pc-done" : ""} ${isCurrent ? "pc-current" : ""}">
      <span class="pc-status">${node.done ? "✓" : isCurrent ? "↻" : "○"}</span>
      <span class="pc-num">${esc(node.number)}</span>
      <span class="pc-desc">${esc(node.desc)}</span>
      <span class="pc-time-slot">${timing}</span>
    </div>`;
}

function renderPlanChecklist(planMarkdown, currentTask, currentTaskStartedAt, taskHistory, currentPhase) {
  const tree = buildPlanTree(planMarkdown);
  const phaseKeys = Object.keys(tree);
  if (!phaseKeys.length) {
    return `<p class="empty-note">No plan.md checklist reported yet.</p>`;
  }
  if (!phaseKeys.includes(checklistPath[0])) checklistPath = [phaseKeys[0]];

  const currentNumber = currentTask ? (currentTask.match(/^\S+/) || [""])[0] : "";
  // Most recent finished entry per (phase, leading number) -- see
  // buildPlanTree's own docstring for why phase has to be part of the key,
  // not just the number.
  const historyByKey = {};
  for (const h of taskHistory || []) {
    const num = (h.task.match(/^\S+/) || [h.task])[0];
    historyByKey[`${h.phase || ""}:${num}`] = h;
  }

  // Dashboard strip: one ring per phase section plus an overall one,
  // glanceable before drilling into a single leaf. A parent has no
  // checkbox of its own, so every count here is summed over leaf
  // descendants (subtreeCounts), never read off the parent directly.
  const phaseCounts = {};
  let overallDone = 0;
  let overallTotal = 0;
  for (const p of phaseKeys) {
    let d = 0;
    let t = 0;
    for (const root of Object.values(tree[p])) {
      const [nd, nt] = subtreeCounts(root);
      d += nd;
      t += nt;
    }
    phaseCounts[p] = [d, t];
    overallDone += d;
    overallTotal += t;
  }
  const rings =
    ringChart((overallDone / overallTotal) * 100, "Overall", `${overallDone}/${overallTotal}`, "pc-ring-overall") +
    phaseKeys
      .map((p) => ringChart((phaseCounts[p][0] / phaseCounts[p][1]) * 100, PHASE_LABELS[p] || p, `${phaseCounts[p][0]}/${phaseCounts[p][1]}`))
      .join("");

  // Phase tabs -- the outermost drill-down level ("## Implementation" vs
  // "## Testing" are separate trees, so this is where that split becomes
  // visible/navigable instead of silently mixed into one list).
  const phaseTabs = phaseKeys
    .map((p) => {
      const [d, t] = phaseCounts[p];
      return `<button class="pc-tab ${checklistPath[0] === p ? "active" : ""}" data-pc-depth="0" data-pc-value="${esc(p)}">${esc(PHASE_LABELS[p] || p)} <span class="pc-tab-badge">${d}/${t}</span></button>`;
    })
    .join("");

  // Walk the rest of the path through the tree: one tab row per level,
  // covering whichever siblings are branches (drill further) AND whichever
  // siblings at that SAME level are already leaves -- a level can
  // genuinely be a mix of both (e.g. "3.2" is a leaf sibling of branches
  // "3.1"/"3.3"). The leaf CARDS shown below, though, belong to exactly
  // ONE level: the one the walk actually stops at (either a natural dead
  // end with no branches, or a leaf-tab the user explicitly selected) --
  // never accumulated across every level visited on the way there.
  // Regression this replaces: leaf siblings used to be collected from
  // EVERY level walked through into one merged, sorted list -- number-
  // sorted, so not visibly out of order, but structurally wrong: a
  // top-level leaf like "4" (a sibling of section "3" itself) ended up
  // mixed into "3.3"'s own child list with no indication it belonged at a
  // completely different level, and "3.2" (a sibling of "3.3", not a
  // descendant) appeared there too, while being un-openable on its own --
  // the walk always auto-descends into a default branch, so a shallower
  // leaf sibling had no other way to surface at all. Fixed by making a
  // leaf sibling a SELECTABLE TAB at its own level (same data-pc-depth/
  // data-pc-value the click handler already generically supports for
  // branches) instead of silently hoisting it into a deeper level's card
  // list.
  let levelMap = tree[checklistPath[0]];
  let tabRows = "";
  let finalLeaves = [];
  let depth = 0;
  let lastWrittenIndex = 0;
  const breadcrumb = [`<button class="pc-crumb" data-pc-crumb="0">${esc(PHASE_LABELS[checklistPath[0]] || checklistPath[0])}</button>`];

  while (levelMap) {
    const nodes = sortByNumber(Object.values(levelMap));
    const branches = nodes.filter((n) => Object.keys(n.children).length > 0);
    const leaves = nodes.filter((n) => Object.keys(n.children).length === 0);

    // An explicitly-selected leaf sibling stops the walk HERE, showing
    // this level's own leaves below -- otherwise a leaf sibling of a
    // branch would be permanently unreachable, since the walk always
    // auto-descends into a default branch when nothing else is selected.
    const selectedLeaf = leaves.find((n) => n.number === checklistPath[depth + 1]);
    const stopHere = !!selectedLeaf || !branches.length;
    const selected =
      selectedLeaf ||
      branches.find((n) => n.number === checklistPath[depth + 1]) ||
      branches[0] ||
      leaves[0];
    if (!selected) break;

    checklistPath[depth + 1] = selected.number;
    lastWrittenIndex = depth + 1;

    tabRows += renderLevelTabRow(branches, leaves, selected.number, depth + 1);
    breadcrumb.push(`<button class="pc-crumb" data-pc-crumb="${depth + 1}">${esc(selected.number)} ${esc(truncate(selected.desc, 20))}</button>`);

    if (stopHere) {
      finalLeaves = leaves;
      break;
    }

    levelMap = selected.children;
    depth += 1;
  }
  checklistPath.length = lastWrittenIndex + 1; // drop any stale deeper segments from a shorter path

  const leafCards = sortByNumber(finalLeaves)
    .map((n) => renderLeafCard(n, currentPhase, currentNumber, currentTaskStartedAt, historyByKey))
    .join("");

  return `
    <div class="panel">
      <div class="panel-head"><span>Plan checklist</span><span class="n">${overallDone}/${overallTotal} done</span></div>
      <div class="panel-body pc-rings">${rings}</div>
      <div class="pc-tab-row pc-tab-row-phase">${phaseTabs}</div>
      <div class="pc-breadcrumb">${breadcrumb.join('<span class="pc-crumb-sep">/</span>')}</div>
      ${tabRows}
      <div class="pc-cards-wrap">${leafCards || '<p class="empty-note">Nothing directly under this section yet.</p>'}</div>
    </div>
  `;
}

// A sequential (magnitude) heatmap: one hue (the theme's own --accent),
// light -> dark, normalized against the largest cost in view -- per the
// dataviz method, never a rainbow, and identity (which task) is carried by
// the native tooltip + the "click a cell to see the leaf" affordance, not
// by an always-on label (a number on every cell would just be noise at
// 100+ leaves, which is the whole reason this is a grid, not a list).
//
// Grouped into one sub-section per phase (Implement/Testing/...), never one
// mixed grid: "## Implementation" and "## Testing" are SEPARATELY-numbered
// trees in plan.md, so the same leaf number (e.g. "3.2") can legitimately
// exist in both -- a flat grid would show two same-numbered cells with no
// way to tell which section either belongs to. Entries recorded before this
// grouping existed carry no `phase` at all and fall into "Other" rather
// than being dropped.
function renderTaskHeatmap(taskHistory) {
  if (!taskHistory || !taskHistory.length) return "";
  const maxTokens = Math.max(...taskHistory.map((t) => t.tokens), 1);

  const cellHtml = (t) => {
    const ratio = t.tokens / maxTokens;
    // Sequential ramp: mix from a faint tint of --accent (low cost) up to
    // the full --accent (highest cost in view) -- CSS color-mix keeps
    // this correct across every theme without a second hardcoded scale.
    const bg = `color-mix(in oklab, var(--accent) ${8 + ratio * 82}%, var(--bg-panel))`;
    // The task's own leaf number (e.g. "5.2.1.3") is the leading token
    // of its title -- shown as the cell's identity label. The label
    // itself always wears a text token (--ink), never the swatch color
    // (dataviz: "text wears text tokens, never the series color"), so a
    // swatch+label chip stays legible across the whole ramp instead of
    // risking low-contrast text over a bright fill.
    const number = (t.task.match(/^\S+/) || [t.task])[0];
    return `<div class="heat-cell" title="${esc(t.task)} — ${t.tokens.toLocaleString()} tok"><span class="heat-swatch" style="background:${bg}"></span><span class="heat-label">${esc(number)}</span></div>`;
  };

  const byPhase = {};
  for (const t of taskHistory) {
    const key = t.phase || "other";
    (byPhase[key] ||= []).push(t);
  }
  const order = [...PHASES, "other"].filter((p) => byPhase[p]?.length);
  const sections = order
    .map((phase) => {
      const items = byPhase[phase];
      const label = phase === "other" ? "Other" : PHASE_LABELS[phase] || phase;
      const subtotal = items.reduce((sum, t) => sum + t.tokens, 0);
      return `
        <div class="heat-group">
          <div class="heat-group-label">${esc(label)} <span class="n">${items.length} leaves · ${subtotal.toLocaleString()} tok</span></div>
          <div class="heatmap">${items.map(cellHtml).join("")}</div>
        </div>
      `;
    })
    .join("");

  const totalTokens = taskHistory.reduce((sum, t) => sum + t.tokens, 0);
  return `
    <div class="panel">
      <div class="panel-head"><span>Tasks vs tokens</span><span class="n">${taskHistory.length} leaves · ${totalTokens.toLocaleString()} tok total</span></div>
      <div class="panel-body">
        ${sections}
        <div class="heat-scale">
          <span>low</span>
          <div class="heat-scale-grad"></div>
          <span>high (${maxTokens.toLocaleString()} tok)</span>
        </div>
        <p class="heat-note">Context-history cost per leaf while it was current, grouped by phase since Implementation and Testing leaves are numbered as separate trees in plan.md — hover a cell for the task and exact count. Doesn't include the shared system-prompt/tool-schema overhead resent every turn, so this shows relative cost between leaves, not each leaf's true total spend.</p>
      </div>
    </div>
  `;
}

function renderLogPanel(logTail) {
  if (!logTail || !logTail.length) return "";
  const tagClass = (line) => {
    const m = line.match(/\] (\w+):/);
    return m ? m[1] : "";
  };
  const rows = logTail
    .slice()
    .reverse()
    .map((line) => `<div class="log-ln log-${tagClass(line)}">${esc(line)}</div>`)
    .join("");
  return `
    <div class="panel">
      <div class="panel-head"><span>Live log</span><span class="n">last ${logTail.length} lines · newest first</span></div>
      <div class="log-view">${rows}</div>
    </div>
  `;
}

function renderProcessPanel(processes) {
  if (!processes || !processes.length) return "";
  return `
    <div class="panel">
      <div class="panel-head"><span>Background processes</span><span class="n">${processes.length}</span></div>
      <div class="panel-body proc-list">
        ${processes.map(renderProcessRow).join("")}
      </div>
    </div>
  `;
}

function renderProcessRow(p) {
  const where = [p.host && `host=${p.host}`, p.port && `port=${p.port}`].filter(Boolean).join(" ");
  const running = p.status === "running";
  return `
    <div class="proc-row ${running ? "" : "proc-exited"}">
      <span class="proc-handle">${esc(p.handle)}</span>
      <span class="proc-cmd">${esc(p.command)}</span>
      <span class="proc-meta">pid=${p.pid}${where ? " " + esc(where) : ""}</span>
      <span class="proc-status ${running ? "running" : ""}">${esc(p.status)}</span>
      <span class="proc-elapsed">${p.elapsed}s</span>
    </div>
  `;
}

render();
connect();
