// Runs one extractor against a profile's Chrome over CDP, in a tab of its own.
//   node run-extractor.mjs <playwright-node_modules> <cdp-port> <extractor.js> [arg...]
// Prints one JSON document on stdout. Exit: 0 every item ok, 3 some item partial/not_found,
// 4 some item blocked (login/captcha), 1 the run itself failed. Never adopts or closes a tab it
// didn't open: the shared-browser rules apply to scripts exactly as to agents.
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const [nm, port, exPath, ...args] = process.argv.slice(2);
const { chromium } = createRequire(nm + '/')('playwright-core');
const ex = await import(pathToFileURL(exPath).href);
const t0 = Date.now();
let browser, page, out;
try {
  browser = await chromium.connectOverCDP(`http://localhost:${port}`, { timeout: 15000, noDefaults: true });
  // noDefaults: without it Playwright sets the shared context's download behavior to its own temp
  // dir (deleted when we disconnect) and turns on focus/media emulation for every tab: a download
  // anyone else made during a run would vanish. playwright-mcp's hub connects the same way.
  const ctx = browser.contexts()[0];            // the profile's persistent context: its cookies
  // Our own tab, never an existing one, and never activated: newPage() activates its tab, and on
  // macOS that raises Chrome over whatever the person is typing in (DESIGN §11).
  const tag = `bh-run-${process.pid}-${Date.now()}`;
  const cdp = await browser.newBrowserCDPSession();
  // A window of its own, behind the others: a background tab isn't rendered, and waits that poll
  // on animation frames (Cloudflare's interstitial check, lazy-loaded prices) would never finish.
  await cdp.send('Target.createTarget', { url: `about:blank#${tag}`, newWindow: true, background: true });
  for (let i = 0; i < 300 && !page; i++) {      // up to 15 s for it to attach with its URL
    page = ctx.pages().find(p => p.url().includes(tag));
    if (!page) await new Promise(r => setTimeout(r, 50));
  }
  if (!page) throw new Error('our tab never appeared');
  page.setDefaultTimeout(15000);
  page.setDefaultNavigationTimeout(30000);
  // --canary: the extractor's own known-good ids, to catch a selector that rotted (hubctl run --check).
  const ids = args[0] === '--canary' ? (ex.canary || []) : args;
  const items = [];
  // Polite pacing: a burst of page loads is what bot checks look for. Extractors may set minGapMs.
  const gap = ex.minGapMs ?? 2500;
  for (const [n, id] of ids.entries()) {
    if (n) await new Promise(r => setTimeout(r, gap * (0.75 + Math.random() / 2)));
    const s = Date.now();
    let r;
    try { r = await ex.lookup(page, id); }
    catch (e) { r = { status: 'error', error: String(e.message || e).split('\n')[0] }; }
    if (!r || typeof r !== 'object') r = { status: 'error', error: 'extractor returned nothing' };
    if (r.status === 'ok' && !r.data?.unavailable) {
      // A page's required fields, or each search hit's: a hit with no price is as bad as a page.
      const hits = Array.isArray(r.data?.hits) ? r.data.hits : null;
      const gap = (d, keys, pre = '') => (keys || []).filter(k => d?.[k] == null || d[k] === '').map(k => pre + k);
      const missing = hits ? [...new Set(hits.flatMap(h => gap(h, ex.hitRequired, 'hits.')))] : gap(r.data, ex.required);
      if (missing.length) Object.assign(r, { status: 'partial', missing });
    }
    items.push({ id, ...r, url: r.url || page.url(), ms: Date.now() - s });
    if (r.status === 'blocked') {               // the rest would hit the same wall
      if (n < ids.length - 1) items.push({ id: ids.slice(n + 1).join(' '), skipped: ids.slice(n + 1), status: 'skipped', reason: 'blocked before these' });
      break;
    }
  }
  out = { extractor: ex.name, at: new Date().toISOString(), ms: Date.now() - t0, items };
} catch (e) {
  out = { extractor: ex.name, error: String(e.message || e).split('\n')[0], ms: Date.now() - t0 };
} finally {
  try { await page?.close(); } catch {}
  // For a connectOverCDP browser, close() drops our connection; it doesn't quit Chrome.
  try { await browser?.close(); } catch {}
}
if (args[0] === '--canary') {
  const why = i => i.status === 'ok' ? '' : ` (${i.reason || i.error || (i.missing || []).join(',') || i.status})`;
  process.stdout.write(`${ex.name}: ` + (out.error ? `error: ${out.error}` :
    out.items.map(i => `${i.id} ${i.status}${why(i)} ${i.ms}ms`).join(', ') || 'no canary ids') + '\n');
} else process.stdout.write(JSON.stringify(out, null, 1) + '\n');
const st = (out.items || []).map(i => i.status);
const code = out.error ? 1 : st.includes('blocked') ? 4 : st.some(s => s !== 'ok') ? 3 : 0;
// Exit once stdout has drained: a pipe on macOS is written asynchronously and a bare exit() cuts it
// at 64 KB, while waiting for Node to exit by itself could hang on a leftover Playwright handle.
process.stdout.write('', () => process.exit(code));
