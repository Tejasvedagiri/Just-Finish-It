import { defineConfig } from "vite";

// Relative asset paths (not "/assets/...") so the built dist/ works
// whether jfi-master serves it from "/" or, later, behind a reverse-proxy
// path prefix -- no server-side templating involved, master_server.py
// just serves these files as-is.
// `npm run dev` proxies the API calls this app makes (WebSockets to /view
// and /report) to a jfi-master already running somewhere, so the frontend
// can be developed with hot reload without rebuilding on every change.
// Defaults to jfi-master's own default port (8765, see master_server.py's
// DEFAULT_PORT); override with MASTER_DEV_PROXY_TARGET when jfi-master is
// running on a different port, e.g.
//   MASTER_DEV_PROXY_TARGET=ws://localhost:9988 npm run dev
const proxyTarget = process.env.MASTER_DEV_PROXY_TARGET || "ws://localhost:8765";

export default defineConfig({
  base: "./",
  server: {
    proxy: {
      "/view": { target: proxyTarget, ws: true },
      "/report": { target: proxyTarget, ws: true },
    },
  },
});
