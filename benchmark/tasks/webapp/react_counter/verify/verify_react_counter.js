/*
 * Post-hoc behavioral checker for the 'react_counter' webapp task -- copied
 * into the project directory only AFTER the JFI session ends (never visible
 * to the model), then run as `verify.command` from task.json. jsdom loads
 * the actual index.html (executing its real <script> tags, including the
 * CDN-hosted React/ReactDOM UMD builds), then drives it via real
 * dispatchEvent clicks the same way a user would -- not just static
 * structure checks, per this benchmark's push for stronger, more behavioral
 * verification (see benchmark/README.md's Evaluation methodology section).
 * Exits 0 iff every check passes; prints one PASS/FAIL line per check either
 * way.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

async function main() {
  const htmlPath = path.join(process.cwd(), 'index.html');
  const html = fs.readFileSync(htmlPath, 'utf8');

  const checks = [];
  const usesReactCdn = /unpkg\.com\/react(?!-dom)/.test(html) || /unpkg\.com\/react@/.test(html);
  const usesReactDomCdn = /react-dom/.test(html);
  checks.push(['index.html references a React CDN script', usesReactCdn]);
  checks.push(['index.html references a react-dom CDN script', usesReactDomCdn]);

  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    resources: 'usable',
    url: 'http://localhost/',
  });
  const { window } = dom;

  const waitFor = (fn, timeoutMs) => new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      let ok;
      try { ok = fn(); } catch (e) { ok = false; }
      if (ok) return resolve();
      if (Date.now() - start > timeoutMs) return reject(new Error('timeout waiting for condition'));
      setTimeout(tick, 50);
    };
    tick();
  });

  try {
    await waitFor(() => window.document.querySelector('[data-testid="count"]') !== null, 10000);
    checks.push(['count element rendered', true]);
  } catch (e) {
    checks.push(['count element rendered', false]);
  }

  const getCount = () => {
    const el = window.document.querySelector('[data-testid="count"]');
    return el ? el.textContent.trim() : null;
  };
  const click = (selector) => {
    const el = window.document.querySelector(selector);
    if (!el) throw new Error(`missing element ${selector}`);
    el.dispatchEvent(new window.Event('click', { bubbles: true }));
  };

  checks.push(['initial count is 0', getCount() === '0']);

  try {
    click('[data-testid="increment"]');
    await waitFor(() => getCount() === '1', 2000);
    click('[data-testid="increment"]');
    await waitFor(() => getCount() === '2', 2000);
    click('[data-testid="increment"]');
    await waitFor(() => getCount() === '3', 2000);
    checks.push(['three increments -> count is 3', true]);
  } catch (e) {
    checks.push(['three increments -> count is 3', false]);
  }

  try {
    click('[data-testid="reset"]');
    await waitFor(() => getCount() === '0', 2000);
    checks.push(['reset -> count back to 0', true]);
  } catch (e) {
    checks.push(['reset -> count back to 0', false]);
  }

  let passed = 0;
  for (const [name, ok] of checks) {
    console.log(`${ok ? 'PASS' : 'FAIL'}: ${name}`);
    if (ok) passed++;
  }
  console.log(`\n${passed}/${checks.length} checks passed`);
  process.exit(passed === checks.length ? 0 : 1);
}

main().catch((e) => {
  console.error('verify script crashed:', e);
  process.exit(1);
});
