// Recovery tool (DESIGN §1). pw-modules: the dir from var/run/pw-modules-*; run `hubctl up <profile>` first.
// node tools/add-cookies-via-cdp.mjs <pw-modules> <cdp-port> <cookies.json> <rollback-out.json>
// Adds cookies through CDP so Chrome encrypts them itself. Skips any (domain,name,path) Chrome
// already has: those are newer than the backup. Saves the cookies it found first, for rollback.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import { writePrivate } from '../lib/cookies.mjs';
const [nm, port, inFile, rollback] = process.argv.slice(2);
const { chromium } = createRequire(nm + '/')('playwright-core');
const browser = await chromium.connectOverCDP(`http://localhost:${port}`, { timeout: 15000, noDefaults: true });
try {
  const cdp = await browser.newBrowserCDPSession();
  const { cookies: before } = await cdp.send('Storage.getCookies');
  writePrivate(rollback, JSON.stringify(before));   // live cookies: 0600 even over an existing file
  const have = new Set(before.map(c => `${c.domain}\t${c.name}\t${c.path}`));
  const want = JSON.parse(fs.readFileSync(inFile, 'utf8')).filter(c => !have.has(`${c.domain}\t${c.name}\t${c.path}`));
  let ok = 0, failed = [];
  for (let i = 0; i < want.length; i += 100) {
    const chunk = want.slice(i, i + 100);
    try { await cdp.send('Storage.setCookies', { cookies: chunk }); ok += chunk.length; }
    catch {  // one bad cookie fails the batch: retry singly to find it
      for (const c of chunk) {
        try { await cdp.send('Storage.setCookies', { cookies: [c] }); ok++; }
        catch (e) { failed.push(`${c.domain} ${c.name}: ${String(e.message).slice(0, 80)}`); }
      }
    }
  }
  const { cookies: after } = await cdp.send('Storage.getCookies');
  console.log(JSON.stringify({ before: before.length, kept_newer: have.size, sent: want.length, ok, failed: failed.length, after: after.length }));
  if (failed.length) console.log(failed.slice(0, 15).join('\n'));
} finally { await browser.close(); }
