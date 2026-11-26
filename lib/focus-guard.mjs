// Focus guard: keeps agents' browsing from pulling Chrome to the front.
//
//   node focus-guard.mjs <chrome-cdp-port> -- <hub command...> --cdp-endpoint GUARD
//
// playwright-mcp opens tabs with Target.createTarget (no `background`) and calls Page.bringToFront
// on every browser_tabs select. On macOS each of those activates Chrome's window and takes keyboard
// focus from whatever the person is typing in. This process sits between the hub and Chrome: it
// listens on a free loopback port, starts the hub with GUARD replaced by that port's URL, and
// relays CDP both ways, with three changes: Target.createTarget opens a background window, and
// Page.bringToFront / Target.activateTarget are answered here without reaching Chrome, unless the
// tab is hidden in its window (then it must come forward to render at all). Tabs still
// open, navigate and screenshot normally: focus is emulated per page and background tabs aren't
// throttled (see chrome_plist). `hubctl open` talks to Chrome directly, so tabs a person asks for
// still come to the front.
//
// It connects to Chrome only when the hub does, so a hub with no browser behind it still serves its
// tools (DESIGN §4). The hub is a child: its exit is this process's exit, and launchd's signals
// are passed on, so `hubctl up`'s hub restarts work exactly as before.
import { spawn, execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import http from 'node:http';

const chromePort = +process.argv[2];
const sep = process.argv.indexOf('--');
const hubCmd = process.argv.slice(sep + 1);
if (!chromePort || sep < 0 || !hubCmd.length) {
  console.error('usage: focus-guard.mjs <chrome-cdp-port> -- <hub command...> (GUARD marks the endpoint)');
  process.exit(2);
}

// The WebSocket library playwright-core already bundles, from the hub's own npx package, so there
// is nothing to install. Resolved by asking npx where the hub's package lives.
function wsLib() {
  if (process.env.BH_PW_MODULES)   // tests: a node_modules that has playwright-core
    return wsFrom(process.env.BH_PW_MODULES);
  const npxAt = hubCmd.findIndex(a => /(^|\/)npx$/.test(a));
  const pkg = npxAt >= 0 && hubCmd.slice(npxAt + 1).find(a => !a.startsWith('-'));
  if (!pkg) throw new Error('no npx package in the hub command');
  const bin = execFileSync(hubCmd[npxAt], ['-y', '-p', pkg, '-c', 'echo "${PATH%%:*}"'],
    { cwd: '/', encoding: 'utf8', env: process.env, timeout: 60000 }).trim();
  return wsFrom(bin.replace(/\/\.bin$/, ''));
}
function wsFrom(nodeModules) {
  const u = createRequire(nodeModules + '/')('playwright-core/lib/utilsBundle');
  return { WebSocket: u.ws, WebSocketServer: u.wsServer };
}

const log = (...a) => console.error(new Date().toISOString(), '[focus-guard]', ...a);
const CHROME = `127.0.0.1:${chromePort}`;

function startHub(endpoint) {
  const args = hubCmd.map(a => a === 'GUARD' ? endpoint : a);
  const hub = spawn(args[0], args.slice(1), { stdio: 'inherit' });
  for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP']) process.on(sig, () => hub.kill(sig));
  hub.on('exit', (code, sig) => process.exit(code ?? (sig ? 1 : 0)));
  hub.on('error', e => { log('could not start the hub:', e.message); process.exit(1); });
  return hub;
}

// Fail open: without its WebSocket library the guard can't relay, and a hub that doesn't start is
// far worse than one that can raise Chrome. Run the hub straight at Chrome and say so.
let lib;
try { lib = wsLib(); }
catch (e) {
  log(`no relay (${String(e.message).split('\n')[0]}); hub talks to Chrome directly, focus not guarded`);
  startHub(`http://${CHROME}`);
}
if (lib) guard(lib);

function guard({ WebSocket, WebSocketServer }) {
  // A bug in the relay must not take every session's connection down with it.
  process.on('uncaughtException', e => log('relay error (kept running):', e.stack || e));

  // Only a local client that isn't a web page gets through: the hub. Chrome's own DevTools server
  // refuses a Host that isn't localhost or an IP (DNS rebinding) and a WebSocket handshake with an
  // Origin (Chrome 111+), and a relay that skipped those checks would be the way around them. A
  // Sec-Fetch-Site header is refused too: browsers send it on every request to a loopback address,
  // even a no-cors GET with no Origin, and Node's own clients never do (its fetch sends only
  // Sec-Fetch-Mode).
  const local = req => {
    const h = String(req.headers.host || '').toLowerCase();
    return [`127.0.0.1:${guardPort}`, `localhost:${guardPort}`, `[::1]:${guardPort}`].includes(h) &&
      req.headers.origin === undefined && req.headers['sec-fetch-site'] === undefined;
  };

  const server = http.createServer((req, res) => {
    if (!local(req)) { res.writeHead(403, { connection: 'close' }).end('focus-guard: not a local client\n'); return; }
    // HTTP endpoints (/json/version, /json/list, ...) go straight through. Chrome builds the
    // browser's WebSocket URL from the Host header, which already names us; rewrite to be sure.
    const up = http.request({ host: '127.0.0.1', port: chromePort, path: req.url, method: req.method, headers: req.headers }, r => {
      const chunks = [];
      r.on('data', c => chunks.push(c));
      r.on('error', () => res.destroy());
      r.on('aborted', () => res.destroy());
      r.on('end', () => {
        let body = Buffer.concat(chunks);
        if (req.url.startsWith('/json')) {
          body = Buffer.from(body.toString().replaceAll(`localhost:${chromePort}`, `127.0.0.1:${guardPort}`)
            .replaceAll(CHROME, `127.0.0.1:${guardPort}`));
        }
        res.writeHead(r.statusCode, { ...r.headers, 'content-length': body.length }).end(body);
      });
    });
    up.on('error', e => { if (!res.headersSent) res.writeHead(502).end(String(e.message)); else res.destroy(); });
    req.on('error', () => up.destroy());
    req.pipe(up);
  });

  const MAX = 256 * 1024 * 1024;
  const wss = new WebSocketServer({ noServer: true, perMessageDeflate: false, maxPayload: MAX });
  server.on('upgrade', (req, socket, head) => {
    if (!local(req)) {
      socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
      return;
    }
    wss.handleUpgrade(req, socket, head, client => relay(client, new WebSocket(`ws://${CHROME}${req.url}`, { perMessageDeflate: false, maxPayload: MAX })));
  });

  let guardPort;
  server.listen(0, '127.0.0.1', () => {
    guardPort = server.address().port;
    const hub = startHub(`http://127.0.0.1:${guardPort}`);
    log(`listening on ${guardPort}, relaying to Chrome on ${chromePort}; hub pid ${hub.pid}`);
  });
}

// Ids for the guard's own questions to Chrome, far above anything a client uses.
const OWN_IDS = 2_000_000_000;
let ownId = OWN_IDS;

function relay(client, chrome) {
  const pending = [];
  const asked = new Map();   // our id -> the client's bringToFront, waiting on "is that tab hidden?"
  const toChrome = (m, binary = false) => chrome.readyState === 1 ? chrome.send(m, { binary }) : pending.push([m, binary]);
  const answer = (msg) => client.readyState === 1 &&
    client.send(JSON.stringify(msg.sessionId ? { id: msg.id, result: {}, sessionId: msg.sessionId } : { id: msg.id, result: {} }));

  // ws hands frames over as Buffers and sends a Buffer as a binary frame unless told otherwise;
  // Chrome's DevTools socket drops the connection on a binary frame. Keep each frame's type.
  chrome.on('open', () => { for (const [m, binary] of pending.splice(0)) chrome.send(m, { binary }); });
  chrome.on('message', (data, isBinary) => {
    // Replies to the guard's own questions never reach the hub, even late ones (a dialog can hold
    // Runtime.evaluate past the 2 s fallback): Playwright asserts on a reply id it didn't send.
    if (!isBinary && data.length < 4096) {
      const m = data.toString('utf8', 0, 40).match(/^\{"id":(\d{10,})/);
      if (m && +m[1] >= OWN_IDS) {
        const orig = asked.get(+m[1]);
        if (!orig) return;                       // answered by the fallback already
        asked.delete(+m[1]);
        let hidden = false;
        try { hidden = JSON.parse(data.toString()).result?.result?.value === 'hidden'; } catch {}
        // A tab that's hidden in its window (a link opened a second tab in front of it) has to be
        // brought forward, or nothing in it renders and clicks time out. That can raise Chrome; it
        // is the one case where the guard lets it.
        if (hidden) toChrome(JSON.stringify(orig)); else answer(orig);
        return;
      }
    }
    if (client.readyState === 1) client.send(data, { binary: isBinary });
  });
  chrome.on('close', () => client.close());
  chrome.on('error', e => { log('chrome:', e.message); client.close(); });
  client.on('close', () => chrome.close());
  client.on('error', () => chrome.close());
  client.on('message', (data, isBinary) => {
    let out = data;
    if (!isBinary) {
      out = data.toString();
      try {
        const msg = JSON.parse(out);
        if (msg.method === 'Page.bringToFront' && msg.sessionId) {
          const id = ownId++;
          asked.set(id, msg);
          // Never leave the client waiting: no answer within 2 s counts as "visible".
          setTimeout(() => { if (asked.delete(id)) answer(msg); }, 2000).unref();
          toChrome(JSON.stringify({ id, sessionId: msg.sessionId, method: 'Runtime.evaluate',
            params: { expression: 'document.visibilityState', returnByValue: true } }));
          return;
        }
        if (msg.method === 'Page.bringToFront' || msg.method === 'Target.activateTarget') { answer(msg); return; }
        if (msg.method === 'Target.createTarget') {
          // Each agent tab gets a window of its own, opened behind the others. A background *tab*
          // isn't rendered, so Playwright's clicks (which wait for an animation frame) time out
          // in it; the active tab of a background window keeps rendering, because the profiles
          // run with --disable-backgrounding-occluded-windows.
          msg.params = { ...msg.params, newWindow: true, background: true };
          out = JSON.stringify(msg);
        }
      } catch { /* not JSON: pass it through untouched */ }
    }
    toChrome(out, isBinary);
  });
}
