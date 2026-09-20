// The socket "master" -- a single Node process that:
//
// 1. Serves the built fleet frontend (../dist/, built via `npm run build`
//    in this project) as plain static HTTP.
// 2. Accepts WebSocket connections from JFI sessions (Python clients)
//    reporting their live status (path /report) -- see
//    src/JFI/manager/socket_reporter.py, enabled on a session via
//    MASTER_WS_URL. The wire protocol is plain WebSocket + JSON, so the
//    Python reporter needs no changes to talk to this Node master instead
//    of the earlier Python one -- it never assumed a language on the
//    other end.
// 3. Accepts WebSocket connections from browser viewers (path /view) and
//    streams them the live fleet state: a full snapshot on connect, then
//    a message per update.
//
// All three share ONE port (MASTER_PORT, default 8765): plain HTTP
// requests and the WebSocket upgrade are told apart per-request path, so
// the frontend never has to guess a second port -- it opens
// `ws://<the page's own host:port>/view`.
//
// Meant to run on a different machine than the sessions reporting to it --
// "master" and "session" talk only over this one socket, never a shared
// filesystem. Ships with NO authentication (a deliberate choice for a
// first version: run it only on a network you already trust, e.g. behind
// a VPN or a firewalled LAN -- anyone who can reach this port can post
// fake session data or watch real session details).

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WebSocketServer } from "ws";
import { SessionRegistry } from "./session-registry.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_PORT = 8765;
const FRONTEND_DIST = process.env.MASTER_FRONTEND_DIST || path.join(__dirname, "..", "dist");

const MIME = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
  ".ico": "image/x-icon", ".woff2": "font/woff2",
};

function staticResponse(reqPath) {
  if (!fs.existsSync(FRONTEND_DIST)) {
    return { status: 200, contentType: "text/plain", body: Buffer.from(
      "jfi-master is running, but the frontend hasn't been built yet.\n" +
      "cd frontend && npm install && npm run build"
    ) };
  }
  const rel = path.normalize(reqPath).replace(/^(\.\.[/\\])+/, "").replace(/^\/+/, "") || "index.html";
  let candidate = path.resolve(FRONTEND_DIST, rel);
  if (!candidate.startsWith(path.resolve(FRONTEND_DIST)) || !fs.existsSync(candidate) || fs.statSync(candidate).isDirectory()) {
    candidate = path.join(FRONTEND_DIST, "index.html"); // SPA fallback
  }
  const body = fs.readFileSync(candidate);
  const contentType = MIME[path.extname(candidate)] || "application/octet-stream";
  return { status: 200, contentType, body };
}

const registry = new SessionRegistry();
const viewers = new Set();
// key -> the session's own live /report socket, so a viewer's control
// message (pause/resume/queue/answer/stop) can be relayed to the right
// session -- see handleView's "control" branch and
// src/JFI/manager/socket_reporter.py's _dispatch_control on the receiving
// end. Only ever holds ONE socket per key (a session that reconnects
// replaces its own old entry); a key with no live socket here means that
// session isn't currently reachable for control, only for the last status
// it reported before going offline.
const sessionSockets = new Map();

function broadcast(message) {
  if (viewers.size === 0) return;
  const payload = JSON.stringify(message);
  for (const ws of viewers) {
    if (ws.readyState === ws.OPEN) {
      try {
        ws.send(payload);
      } catch {
        viewers.delete(ws);
      }
    }
  }
}

function handleReport(ws) {
  let key = null;
  let repo = "";

  ws.on("message", (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      return;
    }
    if (msg.type === "hello") {
      key = String(msg.key || "");
      repo = String(msg.repo || "");
      sessionSockets.set(key, ws);
      return;
    }
    if (msg.type === "status" && key) {
      const data = msg.data || {};
      const events = registry.update(key, repo, data);
      const entry = registry.get(key);
      broadcast({ type: "session_update", key, entry });
      for (const event of events) broadcast({ type: "activity", event });
    }
  });

  ws.on("close", () => {
    if (!key) return;
    if (sessionSockets.get(key) === ws) sessionSockets.delete(key);
    const events = registry.markOffline(key);
    const entry = registry.get(key);
    if (entry) broadcast({ type: "session_update", key, entry });
    for (const event of events) broadcast({ type: "activity", event });
  });
}

function handleView(ws) {
  viewers.add(ws);
  ws.send(JSON.stringify({ type: "snapshot", ...registry.snapshot() }));

  // Pause/resume/queue/answer/stop from this viewer, relayed to the named
  // session's own /report socket -- see socket_reporter.py's
  // _dispatch_control on the receiving end. A session that isn't currently
  // connected (offline, or never opted into MASTER_WS_URL) just has
  // nothing to relay to; this never throws for that case.
  ws.on("message", (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      return;
    }
    if (msg.type !== "control" || !msg.key) return;
    const target = sessionSockets.get(String(msg.key));
    if (!target || target.readyState !== target.OPEN) return;
    target.send(JSON.stringify({ type: "control", action: msg.action, text: msg.text, key: msg.answerKey }));
  });

  ws.on("close", () => viewers.delete(ws));
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const { status, contentType, body } = staticResponse(url.pathname);
  res.writeHead(status, { "Content-Type": contentType, "Content-Length": body.length });
  res.end(body);
});

const wss = new WebSocketServer({ noServer: true });

server.on("upgrade", (req, socket, head) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  if (url.pathname !== "/report" && url.pathname !== "/view") {
    socket.destroy();
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => {
    if (url.pathname === "/report") handleReport(ws);
    else handleView(ws);
  });
});

const port = Number(process.env.MASTER_PORT) || DEFAULT_PORT;
const host = process.env.MASTER_HOST || "0.0.0.0";
server.listen(port, host, () => {
  console.log(`jfi-master (Node) listening on http://${host}:${port} (WebSocket: /report, /view)`);
});
