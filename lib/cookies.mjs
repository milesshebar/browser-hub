// lib/cookies.mjs — keep a profile's logins recoverable across Chrome updates (DESIGN §1).
//
//   node cookies.mjs <pw-modules> <cdp-port> <profile-dir> save
//   node cookies.mjs <pw-modules> <cdp-port> <profile-dir> restore [--if-updated] [--from <file>]
//
// A Chrome update can change how cookies are encrypted, and the first launch under the new version
// drops every cookie it can't read: Chrome 158 did it to every hub profile (2026-11-23). A copy
// written by Chrome itself is no help, because the new version can't read that either. So `save`
// asks the running Chrome for its cookies over CDP (plaintext) and writes them into the profile's
// own directory, and `restore` hands them back to a running Chrome, which encrypts them in whatever
// format it now uses. hubctl saves when it stops a Chrome and hourly from the reaper, and restores
// at a cold start that finds a new Chrome version and a collapsed cookie jar.
//
// The file holds live session secrets. It is mode 0600, inside the profile directory (which is
// never committed or synced), and no more exposed than the profile's own cookies: those are
// encrypted under the mock keychain's publicly known key.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { randomBytes } from 'node:crypto';

export const FILE = 'browser-hub-cookies.json';

// What `save` does with the export already there. A Chrome that has just updated and dropped its
// jar must never overwrite the one export that can bring the jar back.
export function saveAction(prev, version, count) {
  if (!prev || prev.chrome === version) return 'write';
  if (count < prev.count / 2) return 'refuse';  // collapsed under a new version: keep the old export
  return 'rotate';                               // new version, jar intact: keep the old one as .prev
}

// A cold start restores only when Chrome's version changed AND the jar lost more than half of
// what was saved. A version change alone is an ordinary update; a smaller jar alone is someone
// signing out.
export function shouldRestore(saved, version, count) {
  return !!saved && saved.chrome !== version && count < saved.count / 2;
}

const key = c => `${c.domain}\t${c.name}\t${c.path}\t${JSON.stringify(c.partitionKey ?? null)}`;

// Storage.getCookies output -> Storage.setCookies params: every saved cookie the browser doesn't
// have now (what it has is newer) and that hasn't expired.
export function toRestore(saved, current, nowSec) {
  const have = new Set(current.map(key));
  const out = [];
  for (const c of saved) {
    if (have.has(key(c)) || c.partitionKeyOpaque) continue;
    const persistent = !c.session && c.expires > 0;
    if (persistent && c.expires <= nowSec) continue;
    const p = { name: c.name, value: c.value, domain: c.domain, path: c.path, secure: !!c.secure,
                httpOnly: !!c.httpOnly };
    for (const k of ['sameSite', 'priority', 'sourceScheme', 'sourcePort', 'partitionKey'])
      if (c[k] !== undefined && c[k] !== null) p[k] = c[k];
    if (persistent) p.expires = c.expires;
    out.push(p);
  }
  return out;
}

export const productVersion = product => product.slice(product.indexOf('/') + 1);  // "HeadlessChrome/158.0…"

function readExport(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }
}

// Write live session secrets so that only the owner can read them, whatever is already at `file`.
// {mode: 0o600} applies only when a file is created, so writing over an existing 0644 file (or a
// leftover temp file, or a symlink planted at a predictable name) would keep it readable. Instead:
// a temp file that must not exist yet ('wx'), created 0600, then renamed over the destination.
export function writePrivate(file, text) {
  const tmp = `${file}.tmp-${process.pid}-${randomBytes(6).toString('hex')}`;
  try {
    fs.writeFileSync(tmp, text, { mode: 0o600, flag: 'wx' });
    fs.renameSync(tmp, file);
  } catch (e) {
    try { fs.unlinkSync(tmp); } catch {}
    throw e;
  }
}

function writeExport(file, data) {
  // "chrome" first: hubctl reads the version from the head of the file without parsing it.
  writePrivate(file, JSON.stringify({ chrome: data.chrome, saved: data.saved, count: data.count,
                                     cookies: data.cookies }));
}

async function main(argv) {
  const [nm, port, dir, mode, ...rest] = argv;
  const flag = n => rest.includes(n);
  const from = rest.includes('--from') ? rest[rest.indexOf('--from') + 1] : path.join(dir, FILE);
  const file = path.join(dir, FILE);
  const { chromium } = createRequire(nm + '/')('playwright-core');
  // noDefaults: as in run-extractor.mjs, don't touch the shared context's downloads or emulation.
  const browser = await chromium.connectOverCDP(`http://localhost:${port}`, { timeout: 8000, noDefaults: true });
  try {
    const cdp = await browser.newBrowserCDPSession();
    const version = productVersion((await cdp.send('Browser.getVersion')).product);
    const { cookies } = await cdp.send('Storage.getCookies');  // also makes Chrome load (and prune) its jar
    if (mode === 'save') {
      const prev = readExport(file);
      const action = saveAction(prev, version, cookies.length);
      if (action === 'refuse') {
        console.log(JSON.stringify({ action, chrome: version, count: cookies.length, kept: prev.chrome, keptCount: prev.count }));
        return 5;
      }
      if (action === 'rotate') fs.renameSync(file, `${file}.prev`);
      writeExport(file, { chrome: version, saved: Math.floor(Date.now() / 1000), count: cookies.length, cookies });
      console.log(JSON.stringify({ action, chrome: version, count: cookies.length }));
      return 0;
    }
    if (mode === 'restore') {
      const saved = readExport(from);
      if (!saved) {
        if (flag('--if-updated')) { console.log(JSON.stringify({ action: 'none', why: 'no export' })); return 0; }
        console.error(`no saved cookies in ${from}`);
        return 1;
      }
      if (flag('--if-updated') && !shouldRestore(saved, version, cookies.length)) {
        console.log(JSON.stringify({ action: 'none', chrome: version, count: cookies.length, saved: saved.chrome, savedCount: saved.count }));
        return 0;
      }
      const want = toRestore(saved.cookies, cookies, Date.now() / 1000);
      let ok = 0;
      const failed = [];
      for (let i = 0; i < want.length; i += 100) {
        const chunk = want.slice(i, i + 100);
        try { await cdp.send('Storage.setCookies', { cookies: chunk }); ok += chunk.length; }
        catch {  // one bad cookie fails its whole batch: find it
          for (const c of chunk) {
            try { await cdp.send('Storage.setCookies', { cookies: [c] }); ok++; }
            catch (e) { failed.push(`${c.domain} ${c.name}: ${String(e.message).slice(0, 80)}`); }
          }
        }
      }
      const after = (await cdp.send('Storage.getCookies')).cookies.length;
      console.log(JSON.stringify({ action: 'restored', from: saved.chrome, chrome: version, before: cookies.length,
                                   ok, failed: failed.length, after }));
      if (failed.length) console.error(failed.slice(0, 10).join('\n'));
      return failed.length ? 3 : 0;
    }
    console.error(`unknown mode ${mode}`);
    return 2;
  } finally {
    await browser.close();  // a connectOverCDP browser: drops our connection, never quits Chrome
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).then(code => process.exit(code), e => {
    console.error(String(e?.message || e).split('\n')[0]);
    process.exit(1);
  });
}
