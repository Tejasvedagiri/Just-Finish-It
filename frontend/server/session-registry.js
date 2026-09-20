// In-memory fleet state for the master server (master.js) -- plain,
// dependency-free logic so it's unit-testable without spinning up a real
// WebSocket server. Faithful port of src/JFI/web/session_registry.py (the
// Python master's original implementation) -- keep the two in sync if the
// protocol ever changes; this one is authoritative now that the master
// itself runs here, not in Python.
//
// A session never hands the master a filesystem path: every session is
// identified purely by the opaque `key` its own reporter chose (hostname +
// session id -- see JFI.manager.socket_reporter's _session_key on the
// Python side), and every status update is the same plain object
// AbstractManager.get_status_snapshot() already produces. The master has
// no filesystem access to a reporting session's machine anyway once that
// session is on a different host, so nothing here ever reads or writes a
// path that belongs to a session.

export const MAX_ACTIVITY_EVENTS = 200;

function now() {
  return Date.now() / 1000;
}

/**
 * Compares one session's previous status snapshot (`oldData`, null on its
 * first-ever report) against its new one and returns zero or more
 * human-readable activity events -- the same kind of thing a person
 * watching run.log would notice, derived here once centrally instead of
 * by every viewer independently.
 */
export function deriveEvents(key, repo, oldData, newData) {
  const events = [];
  const emit = (severity, text) => events.push({ ts: now(), key, repo, severity, text });

  if (oldData == null) {
    emit("info", `${key} connected`);
    return events;
  }

  const oldPhase = oldData.phase;
  const newPhase = newData.phase;
  if (newPhase && newPhase !== oldPhase) {
    emit("info", `phase ${oldPhase || "—"} → ${newPhase}`);
  }

  const oldAwaiting = oldData.awaiting;
  const newAwaiting = newData.awaiting;
  if (newAwaiting && !oldAwaiting) {
    const prompt = (newAwaiting && typeof newAwaiting === "object" ? String(newAwaiting.prompt || "") : "").slice(0, 120);
    emit("bad", prompt ? `awaiting input — ${prompt}` : "awaiting input");
  } else if (oldAwaiting && !newAwaiting) {
    emit("good", "input received, resuming");
  }

  const oldPlan = oldData.plan;
  const newPlan = newData.plan;
  if (Array.isArray(oldPlan) && Array.isArray(newPlan) && oldPlan.length === 2 && newPlan.length === 2) {
    const [oldDone] = oldPlan;
    const [newDone, newTotal] = newPlan;
    if (newDone > oldDone) {
      emit("good", `ticked ${newDone}/${newTotal}`);
    }
  }

  const oldState = oldData.state;
  const newState = newData.state;
  if (newState !== oldState && newState && String(newState).includes("idle") && !String(oldState || "").includes("complete")) {
    if (Array.isArray(newPlan) && newPlan.length === 2 && newPlan[1] && newPlan[0] === newPlan[1]) {
      emit("good", `pipeline complete — ${newPlan[0]}/${newPlan[1]} ticked`);
    }
  }

  return events;
}

/**
 * Holds the latest known status for every reporting session plus a
 * bounded activity feed derived from each update. One instance lives for
 * the whole lifetime of the master process.
 */
export class SessionRegistry {
  constructor() {
    this._sessions = new Map();
    this._activity = [];
  }

  /** Records a fresh status snapshot from `key` and returns the activity
   * events this update produced (already appended to the bounded feed). */
  update(key, repo, data) {
    const previous = this._sessions.get(key);
    const oldData = previous ? previous.data : null;
    const events = deriveEvents(key, repo, oldData, data);
    const firstSeen = previous ? previous.first_seen : now();
    this._sessions.set(key, {
      key,
      repo,
      data,
      online: true,
      first_seen: firstSeen,
      last_seen: now(),
    });
    this._record(events);
    return events;
  }

  markOffline(key) {
    const entry = this._sessions.get(key);
    if (!entry || !entry.online) return [];
    entry.online = false;
    entry.last_seen = now();
    const events = [{ ts: now(), key, repo: entry.repo || "", severity: "warn", text: `${key} disconnected` }];
    this._record(events);
    return events;
  }

  _record(events) {
    this._activity.push(...events);
    const overflow = this._activity.length - MAX_ACTIVITY_EVENTS;
    if (overflow > 0) this._activity.splice(0, overflow);
  }

  get(key) {
    return this._sessions.get(key) ?? null;
  }

  /** Full current state, for a viewer that just connected. */
  snapshot() {
    const sessions = {};
    for (const [key, entry] of this._sessions) sessions[key] = entry;
    return {
      sessions,
      activity: this._activity.slice(-MAX_ACTIVITY_EVENTS),
    };
  }
}
