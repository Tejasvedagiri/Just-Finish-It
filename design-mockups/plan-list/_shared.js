// Shared mock plan tree + tiny status-dot helper, used identically by all
// six plan-list mockups so they're a fair side-by-side comparison -- same
// data, same theme tokens (lifted from frontend/src/style.css's dark-ocean
// defaults), different LAYOUT/interaction only. Not wired to any real
// session; this is what a moderately deep, realistic JFI plan looks like
// (17 Implement leaves incl. a genuine 3-level branch at 1.2.1/1.2.2, plus
// 2 Testing leaves) -- big enough to actually stress "can I see 1, 1.2,
// 1.2.1 at once", which is the whole point of this review.

const MOCK_TREE = {
  imp: {
    label: "Implement",
    nodes: [
      {
        number: "1", desc: "Scaffold a real Vite project (vanilla JS template) into a throwaway subdirectory, then move everything generated up to the working root.",
        children: [
          { number: "1.1", desc: "Run the Vite vanilla-template scaffolder into .vite-tmp (npm create vite@latest .vite-tmp -- --template vanilla).", status: "done" },
          {
            number: "1.2", desc: "Move the generated Vite project files up from .vite-tmp to the working root.",
            children: [
              { number: "1.2.1", desc: "Move package.json, vite.config.js, index.html, src/, public/, .gitignore up one level.", status: "done" },
              { number: "1.2.2", desc: "Remove the now-empty .vite-tmp directory without clobbering .jfi/, portfolio-dashboard.html, or .env.", status: "current" },
            ],
          },
          { number: "1.3", desc: "Verify the working root state after the move: Vite files present, .jfi/ untouched, portfolio-dashboard.html unchanged.", status: "todo" },
          { number: "1.4", desc: "Confirm .env, .jfi/, and portfolio-dashboard.html are byte-identical to before the scaffold step.", status: "todo" },
        ],
      },
      { number: "2", desc: "Extract the embedded CSS (lines 10-339) into src/styles.css and reference it from index.html. No CSS behavior changes.", status: "todo" },
      { number: "3", desc: "Extract ALL hardcoded data out of the script into src/data.js: holdings, dividends, growth-by-holding, transactions, txnMeta, newsItems, wmExact, genProfiles, and the titles object.", status: "todo" },
      { number: "4", desc: "Extract the theme-toggle concern into src/theme.js: the #themeToggle click handler that flips body[data-theme] and swaps the #knobIcon inline SVG path.", status: "todo" },
      { number: "5", desc: "Extract the global page-level time-range filter into src/timeRange.js: currentRange, customLabel, isCustom, customStartDate/EndDate, the preset multipliers, and applyGlobalRangeToPage.", status: "todo" },
      { number: "6", desc: "Extract the view-switching/router concern into src/router.js: the .nav-item[data-view] click handlers that toggle .view/.active and update #pageTitle/#pageSub.", status: "todo" },
      { number: "7", desc: "Extract the hand-drawn SVG chart primitives into src/charts.js: setChartPath and renderLineChart. Do NOT introduce a charting library.", status: "todo" },
      { number: "8", desc: "Extract the ticker-lookup concern into src/ticker.js: loadTicker, getTickerData, buildGenerated, and the #tickerInput wiring.", status: "todo" },
      { number: "9", desc: "Extract the per-view render functions: renderOutlook, renderTxns, renderNews, updateScalables, updateGrowthBarlist, openSectorDetail.", status: "todo" },
      { number: "10", desc: "Wire the entry point src/main.js: import every extracted module in the right order and call each one's init function once.", status: "todo" },
      {
        number: "11", desc: "Convert portfolio-dashboard.html into the Vite entry index.html: a thin shell referencing the extracted CSS/JS, body markup preserved verbatim.",
        children: [
          { number: "11.1", desc: "Write the new Vite index.html from portfolio-dashboard.html's body markup, verbatim -- no content changes, no new markup.", status: "done" },
          { number: "11.2", desc: "Finish the index.html shell's head: remove the embedded <style>/<script>, add the CSS <link> and the /src/main.js module reference.", status: "current" },
          { number: "11.3", desc: "Verify the thin shell mechanically: no <style>/inline <script> blocks remain, all 10 view section ids present, file parses as well-formed HTML.", status: "todo" },
        ],
      },
    ],
  },
  testing: {
    label: "Testing",
    nodes: [
      { number: "1", desc: "Build verification: npm install then npm run build from the project root. Both must exit 0 and produce a working dist/.", status: "todo" },
      { number: "2", desc: "Browser E2E on the running dev server: start npm run dev in the background, load it via browse_webpage, and mechanically confirm all 10 views, the theme toggle, and the WM/AAPL/MSFT/O-only ticker lookup.", status: "todo" },
    ],
  },
};

function statusGlyph(status) {
  if (status === "done") return { char: "✓", cls: "s-done" };
  if (status === "current") return { char: "●", cls: "s-current" };
  return { char: "○", cls: "s-todo" };
}

// Flattens the tree into count [done, total] -- a branch's own count is the
// sum of its leaf descendants, it never carries a status of its own.
function subtreeCounts(node) {
  if (!node.children) return [node.status === "done" ? 1 : 0, 1];
  let d = 0, t = 0;
  for (const c of node.children) {
    const [cd, ct] = subtreeCounts(c);
    d += cd; t += ct;
  }
  return [d, t];
}

const SHARED_CSS = `
  :root {
    --bg: #141b26; --bg-raised: #1b2431; --bg-card: #1b2431; --bg-panel: #1b2431; --bg-log: #171f2a;
    --line: #3c4a5c; --line-soft: #263140;
    --ink: #c8d6e5; --ink-dim: #96a8bd; --ink-faint: #5f7186;
    --accent: #00afaf; --accent-dim: #0d4a4a;
    --amber: #e0af68; --red: #ff5f5f; --red-dim: #7a2f2f; --blue: #5fafff;
    --font-ui: 'JetBrains Mono', ui-monospace, monospace;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--ink); font-family: var(--font-ui);
    font-size: 13px; line-height: 1.5;
  }
  #app { padding: 22px clamp(16px, 3vw, 40px) 60px; max-width: 1040px; margin: 0 auto; }
  .mockup-head { margin-bottom: 18px; }
  .mockup-head .tag { color: var(--accent); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700; }
  .mockup-head h1 { margin: 4px 0 6px; font-size: 19px; }
  .mockup-head p { margin: 0; color: var(--ink-dim); font-size: 12.5px; max-width: 640px; }
  .back-link { display: inline-block; margin-bottom: 14px; color: var(--ink-faint); font-size: 11.5px; text-decoration: none; }
  .back-link:hover { color: var(--accent); }
  .panel { background: var(--bg-panel); border: 1px solid var(--line); }
  .panel-head { display: flex; align-items: center; justify-content: space-between; padding: 10px 14px; border-bottom: 1px solid var(--line); font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-dim); font-weight: 700; }
  .panel-head .n { color: var(--accent); font-variant-numeric: tabular-nums; }
  .s-done { color: var(--accent); }
  .s-current { color: var(--amber); }
  .s-todo { color: var(--ink-faint); }
  .desc { color: var(--ink-dim); font-size: 12px; }
  code, .mono { font-family: var(--font-ui); }
`;
