# Design notes

Why browser-hub is shaped the way it is, with the evidence behind each decision. Dated findings
were measured against `@playwright/mcp@0.0.78` on macOS with Google Chrome, and §8 was re-verified
against 0.0.85. Re-check them before
changing the pinned version (see the last section).

## 1. One Chrome and one hub per profile

A Chrome profile directory can be opened by only one Chrome process (its `SingletonLock`), and one
playwright-mcp server attaches to one CDP endpoint. So a profile = one Chrome + one hub + one port
pair. Profiles can never collide with each other: separate process, separate data directory,
separate hub.

**Chrome is launched by launchd, not by Playwright.** That way it survives hub restarts. It's also
a normal, signed Google Chrome, so extensions like 1Password (which check for a genuine browser)
keep working, and it runs headed, so a person can sign in and watch.

**`--use-mock-keychain --password-store=basic` are permanent.** Cookies in these profiles are
encrypted under Playwright's mock keychain. Launch the same profile without those flags and Chrome
can't decrypt any of them: every site shows you as logged out.

**A Chrome update can do the same.** Up to Chrome 158, cookies in these profiles were stored as
`v10`: AES-128-CBC under a key derived from the mock keychain's fixed password (`mock_password`,
PBKDF2-SHA1, salt `saltysalt`, 1003 rounds), with a SHA-256 of the cookie's host before the value.
Chrome 158.0.8127.37 installed itself on 2026-11-23 at 20:12 and writes `v11`, under a key that
isn't that one. At each profile's first launch under it (one at 00:01, another at 11:35 the
next day) it couldn't read the `v10` cookies and dropped them, every login with them. It logged
only `Failed to decrypt token for service AccountId-…`. `v11` cookies survive restarts and a copy
to another Mac, so it happened once.

Recovered 2026-11-24 from the Time Machine backup taken at 23:10, between the update and the
first restart: `tools/v10-cookies-to-cdp.py` decrypts a backed-up `Default/Cookies` into CDP
cookie parameters, and `tools/add-cookies-via-cdp.mjs` hands them to the running Chrome
(`Storage.setCookies`), which encrypts them in its own current format and skips any cookie it
already has a newer copy of. 1,847 cookies went back into one profile and 540 into another, and Google
was signed in again at once. The lesson: a Chrome restart after an update can lose every login,
and the only copy that can be read afterwards is one taken before that restart.

**So hubctl takes that copy itself** (`lib/cookies.mjs`). A copy of the cookie database is no use,
because it's in the format the new version can't read. The copy is the cookies in plaintext, from
the running Chrome over CDP (`Storage.getCookies`), in `<profile dir>/browser-hub-cookies.json`,
mode 0600. It's no more exposed than the profile's own `v10` cookies were, and it sits in the
directory that is already never committed or synced. Saved when `hubctl down` or the reaper stops
a Chrome (an update installed while Chrome runs only takes effect at the next launch, so the
running one can still read everything), and by the reaper every `COOKIE_SAVE_MINUTES` while it
runs, for crashes and reboots. A cold start compares the saved copy's Chrome version with the
running one (one `curl`). Only when they differ does the helper run: if the jar now holds less
than half of what was saved, it hands back every saved cookie the jar lacks, and Chrome encrypts
them in its new format. A version change with an intact jar is an ordinary update, and a smaller
jar without one is someone signing out: neither restores. A save under a new version that finds
the jar collapsed refuses to overwrite the copy, so a missed restore can still be done by hand
(`hubctl cookies <profile> restore`). A save under a new version otherwise keeps the old copy as
`.prev`. The live suite fakes an update (an emptied jar and a copy stamped with an older version)
and checks both the refusal and the restore.

**Chrome starts with no window and no tab (`--no-startup-window`).** Until 2026-11-24 every profile
opened one startup tab, a "label page" naming the profile, on the belief that Chrome quits when
its last tab closes and takes the CDP endpoint down under every agent. That isn't so on macOS.
Measured 2026-11-24 with a headed Chrome 158 started with `--no-startup-window`: CDP answered
with no windows at all; three rounds of opening a tab in its own window (as the focus guard does)
and closing it each left zero tabs and a live Chrome; a page from the shared context
(`context.newPage()`, the playwright-mcp path) worked with no window open, and so did
`/json/new` (`hubctl open`). Once the focus guard gave every agent tab its own window, the label
tab had become a stray window of its own, so it went. Real startup URLs were tried even earlier
and removed: agents open their own tabs anyway. Logins live in the cookie jar, not in open tabs.

**Native-messaging manifests are linked into every profile.** Chrome looks for a native-messaging
host (the manifest that lets an extension talk to its desktop app) in
`<user-data-dir>/NativeMessagingHosts`, not in the stock
`~/Library/Application Support/Google/Chrome/NativeMessagingHosts` that 1Password writes to. Our
profiles have their own user-data-dirs, so the 1Password extension showed "Connection problem —
make sure the 1Password app is unlocked" (2026-10-13) even with the app unlocked and the browser
genuine: 1Password's BrowserSupport logs never saw Chrome, because Chrome never found the manifest
to launch it. The fix is a symlink per host per profile. `hubctl add`, `regen` and `up` make them
(`link_native_hosts`); `doctor` warns when one is missing. Symlinks, not copies, so a 1Password
update that rewrites the manifest is picked up. The hosts are the `NATIVE_HOSTS` config list. A
real file already sitting in a profile's directory is the user's and is never replaced. Linking
takes effect without a Chrome restart: Chrome reads the manifest when the extension connects.

## 2. Many clients on one browser: `--shared-browser-context`

playwright-mcp's `--shared-browser-context` is the mode built for several MCP clients on one
browser (playwright PR #37463). Each client connection gets its own `Context` with its own current
tab. Without the flag, a second client evicts the first.

What's shared, and why the agent rules exist:

- **The tab list is shared.** Every `Context` subscribes to page create/close events on the one
  browser context, so each client's `browser_tabs list` shows *all* tabs, including other agents'.
  Rule: open your own tab first, and never act on one you didn't open.
- **Indices shift.** Tabs are a plain array, and a close splices it, so when another agent closes a
  tab below yours, your index changes. Measured with three concurrent lanes: one lane watched its
  own tab go 6 → 5 → 4. Content was never mixed up, but a remembered index silently points at
  someone else's tab. Rule: re-list right before every select/close and match by URL/title.
- **The current-tab pointer can move.** When a tab closes, each client's pointer falls back to
  `tabs[min(index, len-1)]`, which can be a tab that client never opened. Rule: if a call reports
  a current tab you don't recognize (often `about:blank`), re-list and re-select your own.
- **Tabs outlive their session.** A disconnected client's tabs stay open, so orphans accumulate.
  The browser is headed, so a person can just close them.
- **JS dialogs block only the tab that raised them.** Focus is emulated per page, and the
  no-throttling flags keep background tabs running at full speed.

## 3. Lanes: one MCP server name per concurrent agent

Tested 2026-07-25. Two subagents that reference the **same** MCP server name share one client, and
so one current-tab pointer, even when each declares the server inline in its own frontmatter. Run
concurrently, they wrecked each other: one agent's `browser_navigate` landed on a tab it never
opened, a snapshot came back showing the other agent's page, and a tab vanished from under its
owner. (Both agents refused to close tabs they couldn't identify, which is the only reason nothing
was destroyed.)

A lane is a copy of an agent that differs only in its server name, pointed at the same hub URL, so
each lane gets its own client. Verified the same day with three lanes running truly concurrently
(three navigate/snapshot/list cycles each, overlapping):

- **Tool isolation was perfect.** Each lane saw only its own `mcp__<server>__*` namespace. Inline
  servers are per-agent and aren't inherited by siblings, which is also why a lane's
  `disallowedTools` must not name other lanes: the base name is a string prefix of every lane's
  name, and a prefix-matching deny would strip the lane's own tools.
- **Content isolation was perfect.** All nine navigations landed where requested, and no lane ever
  saw another's page.
- **Sessions were untouched.** Logged-in state and a cart on a shared store account were intact
  afterwards.

`hubctl lanes` generates them. New agent files load only at the next session.

**Why other profiles' hubs live only in agent frontmatter.** Per the Claude Code subagent docs, an
MCP server defined inline in an agent's `mcpServers` gives its tools to that agent and keeps their
descriptions out of the main conversation's context. Each extra profile costs the main session
nothing, and an identity is used only by delegating to its agent on purpose. That is routing,
not access control: the hubs and CDP ports have no authentication, so any local process, a
session's shell included, can still reach every profile.

## 4. Lifecycle: an always-on hub, a lazy Chrome

The main session's `playwright` MCP server connects when the session starts and doesn't retry.
Originally that meant keeping the default Chrome running around the clock (measured at 1.38 GB
resident, 12 processes, 0.1% CPU). An earlier attempt made it lazy and removed `playwright` from
`~/.claude.json` to compensate. That left sessions with no browser tools except Claude in Chrome,
which the system prompt actively promotes, so agents started driving the user's personal Chrome:
exactly what this project exists to prevent.

It turned out the constraint binds the **hub**, not Chrome (measured 2026-08-25):

1. **A hub with no Chrome** binds its port, completes the MCP `initialize` handshake, and serves
   its full tool list. It doesn't touch CDP until a browser tool is actually called. So the
   ~30 MB hub alone satisfies session start, and Chrome can be lazy.
2. **A tool call on a hub that has never had a browser**, with Chrome down, returns a soft
   `isError` (ECONNREFUSED). The session survives, and the next call works once Chrome is up.
3. **Chrome dying under a live hub is different.** Every client connected to that hub is left
   holding a destroyed context, and every later call fails with `TypeError: Cannot read properties
   of undefined (reading 'once')`, permanently, even after Chrome comes back. (A client on a
   separate MCP session got `HTTP 404 Session not found` instead.) Re-confirmed by the live test
   suite, which kills Chrome under a connected session.
4. **Restarting the hub clears it, transparently.** From a live Claude Code session mid-
   conversation, the very next tool call after a hub restart succeeded, with no `/mcp` reconnect.
   Re-observed 2026-09-24 on a newer Claude Code: the first call after the restart failed with
   "MCP server session expired", the client reconnected by itself within seconds, and the retry
   succeeded. Still no `/mcp` needed; agents are told to simply retry.

Hence the rule in `hubctl up`: **whenever it has to start Chrome, it restarts the hub too.** That
also makes a reap followed by the next cold start invisible to an open session. Recovering by
starting only Chrome leaves the hub broken (finding 3).

Other hubs are lazy as well: a subagent's MCP connection is made at spawn time, after the hook has
already run, so nothing needs them up in advance.

## 5. The hook

`ensure-profile.sh` is a `PreToolUse` hook on two matchers:

- **`Agent`**: a subagent's MCP connections are made at spawn time, before it can run any command.
  A browser agent whose Chrome is cold would start with dead tools and no way to fix that itself,
  so the hook has to boot Chrome before the spawn.
- **`mcp__playwright__.*|mcp__browser-.*`**: the main session's calls (`playwright` is the default
  profile) and every browser agent's own calls. This covers a Chrome that was reaped, quit or
  crashed partway through a conversation.

It's on the path of every browser tool call, so it has to be cheap. It reads the two fields it
needs with a regex on the raw hook JSON (no Python startup), and `hubctl up`'s fast path is two
`nc -z` probes and a `touch`: about 50 ms. It always exits 0: exit 2 from a `PreToolUse` hook
blocks the call, and this hook must never be the reason a browser call fails. A tool from some
unrelated MCP server that happens to be named `browser-*` fails the profile lookup silently
(exit 3). An agent named `browser-<unknown>` gets a stderr message.

Hook configuration is read at session start, so a newly registered hook only affects new sessions.

## 6. The reaper

`hubctl reap` (launchd, every 15 min) stops a profile's Chrome when all of these hold:

1. its Chrome is up;
2. its last-use stamp is older than `IDLE_MINUTES`. Every `hubctl up` writes the stamp, so the
   hook, scripts and humans all extend the lease the same way, and nobody has to remember to;
3. for a lazy hub only: no client is connected to the hub right now. That protects an agent
   working past the stamp.

The always-on hub gets no connection guard. Every open Claude Code session keeps a connection to
it whether or not it has browsed (15 were measured at once), and once anyone has browsed, the hub
holds its CDP socket open too, so both ports always look busy. The stamp is the only honest
signal. Reaping under an open session is safe because of §4.4.

An earlier version read the hub port with `PlistBuddy -c 'Print :ProgramArguments' | grep -- '--port
N'`. PlistBuddy prints one array element per line, so that never matched, and the connection guard
silently did nothing for weeks. Ports are now read from the generated XML, whose shape `hubctl`
controls, and a live test holds a connection open to a profile's hub and checks that the reaper
spares it.

The reaper also deletes files in `var/output/` older than `OUTPUT_RETENTION_DAYS`, and rotates the
hook's and its own logs. Chrome and hub logs rotate when their job restarts, because rotating a log
under a running writer is unsafe.

## 7. Concurrency and config changes in `hubctl up`

- **A per-profile lock** (`zsystem flock`) serializes cold starts. Spawning three lanes at once
  fires three hooks at once. Unserialized, each would restart the hub under the others. The fast
  path takes no lock. The live suite starts one profile from three processes at once and checks
  that exactly one Chrome results.
- **The hub restart is a recorded debt.** Starting Chrome writes `var/run/hub-restart-owed-<p>`
  before anything else, and only a completed hub restart clears it. The fast path refuses to
  answer "already up" while it exists. Without it, an interrupted cold start (Ctrl-C, a killed
  hook, a Chrome slower than the timeout) would leave both ports answering over a poisoned hub,
  and every later call would take the fast path straight past the fix.
- **Fail fast after a failure.** A profile that failed to start in the last minute fails again at
  once (`var/run/failed-<p>`), instead of making every queued browser call wait out the same
  timeouts in turn.
- **Stale markers.** launchd keeps using a job's old definition until the job is unloaded and
  reloaded, and unloading a running Chrome kills it. So `hubctl regen` reloads only jobs that
  aren't running, restarts the always-on hub at once (safe, per §4.4), and marks a running Chrome
  or lazy hub as stale. `hubctl up` applies the marker at that job's next cold start, when nothing
  is running to disturb. `hubctl reload` applies it immediately, for when that's what you want.

## 8. playwright-mcp behaviors this depends on

Source-verified against the 0.0.78 bundle (`playwright-core/lib/coreBundle.js`) on 2026-08-09,
and re-verified against 0.0.85 on 2026-11-09 (bundle read, plus a probe running 0.0.78 and 0.0.85
side by side on a throwaway Chrome), unless noted otherwise.

- **Screenshot filenames resolve against the client's cwd, not `--output-dir`.**
  `browser_take_screenshot` with a `filename` calls `resolveClientFile → workspaceFile(filename,
  clientWorkspace)`, i.e. `path.resolve(<session cwd>, filename)`, and the server writes with no
  overwrite check. A bare name silently replaced an untracked file in a user's repo. `--output-dir`
  is used only when `filename` is omitted (auto-named `page-<timestamp>.png`). Writes are allowed
  only under those two roots: anything else, `/tmp` included, is rejected with "File access
  denied". Hence the rule: omit `filename`, or give an absolute path under the output dir.
- **`file://` is blocked** by default. `--allow-unrestricted-file-access` lifts it, but the hubs
  don't set it: it would let any agent read any local file through the browser. Serve local files
  over `http://127.0.0.1` instead.
- **`--timeout-action` defaults to 5000 ms**, and it's passed straight into `locator.hover()`,
  `click()` and so on. Every slow-but-valid action on a heavy or animated page failed at 5 s.
  Hubs here run with 15000 (`ACTION_TIMEOUT_MS`). Navigation keeps its own 60 s default.
- **A failed `browser_tabs action=new`** (say, to a blocked URL) still leaves an `about:blank` tab
  behind, because the page is created before navigation and not unwound. It's harmless; leave it.
- **§4's findings 1-4** (tools with no browser, the `'once'` failure, restart repairs it).
- **Open security issues upstream:** password field contents can appear in accessibility
  snapshots (microsoft/playwright-mcp#1566), and page content is a prompt-injection vector the
  model reads as part of the tree (#1479). The agent rules cover both.

- **Hub flags since 0.0.85.** Hubs run with `--snapshot-mode none` (`SNAPSHOT_MODE`): clicks,
  typing and navigation no longer write a full page snapshot to disk, and they skip the post-action
  ARIA capture. Over 60 days (2026-09 to 2026-11) about 3,000 action results carried an automatic
  snapshot link, and agents opened one of those files 187 times. Agents now ask for refs with
  `browser_find` or `browser_snapshot`. `--file-paths absolute` gives saved-file links a leading
  `/`, so they resolve from any session's cwd. `--no-webmcp` keeps tools that a page registers through WebMCP out of the agents' hands.
  Pages are data, not tool definitions. These flags crash older versions, so `hub_plist` adds them
  only when `pw_mcp_at_least 0.0.85`. A `0.0.78` pin still gets a working hub. A version spec the
  gate can't place (a range, a tag other than `latest`) counts as old. `--idle-timeout` isn't
  set: its one-hour default applies only to a browser playwright-mcp launched itself, never to
  one attached over `--cdp-endpoint`.
- **When the tab list is printed.** A response includes `### Open tabs` when snapshots are on, or
  when any tab's header changed since *this client* last saw it. The header covers title, URL,
  console counts and whether it's this client's current tab. In `full` mode that put the whole
  tab list in about 10,600 responses over 60 days, roughly 0.9M tokens. In `none` mode it appears
  only on a change. The signal the rules depend on survives: when an agent's own tab is closed
  under it and its pointer falls back to someone else's tab, the next response prints the list
  with the new `(current)` tab. The probe confirmed this on both versions. A keyboard action
  in that state lands on the other tab silently, in every version, which is why rule 2 says to
  re-list before acting.
- **A screenshot with a `filename` isn't sent to the model.** `browser_take_screenshot` calls
  `registerImageResult` only when `filename` is omitted (0.0.78 and 0.0.85). So an evidence
  capture saved to an absolute path costs no image tokens. Over the same 60 days, 928 screenshots
  cost about 1.3M image tokens, and 141 of them were `fullPage`.
- **`browser_close` is refused** in a shared context since 0.0.81 ("The browser context is shared
  between clients and cannot be closed"). In 0.0.78 it closed a page. The rule against calling it
  stays, but a slip is now harmless.
- **Every action waits 500 ms for triggered work to settle.** This was hard-coded in 0.0.78, and
  0.0.85 makes it `--timeout-settle`. At 100 ms a click took 0.13 s instead of 0.55 s on the probe
  page. It's left at the default, because that wait is what keeps clicks reliable on slow pages.

When upgrading `PW_MCP`, re-verify every item above. The live tests cover §4, basic browsing and
the hub flags above. The screenshot-path and `file://` behaviors need a manual check.

## 9. Tried and rejected

- **A separate Chrome per unattended script, with no hub and no agent**, so interactive agents
  couldn't disturb a daemon's session. Retired after it was shown that own-tab discipline is
  enough. A raw-CDP script and a live hub on the same Chrome coexisted cleanly, even through native
  `confirm()` dialogs: 10/10 were accepted by the script, and the hub session's tab list and
  current tab were untouched. What sharing requires is that scripts open, use and close their own
  tab, and never adopt an existing tab by URL.
- **Marking a profile's `exit_type` as Normal on shutdown**, on the theory that launchd's SIGTERM
  reads as a crash and makes Chrome restore old tabs. The first half is true; the second isn't.
  Booting from a deliberately "crashed" state restored nothing but the configured startup URLs.
- **Assuming inline `mcpServers` gives each subagent its own connection.** Disproved (§3); lanes
  exist because of it.
- **Several cloned profiles kept in sync by a SessionStart script.** This was the original design,
  and it caused drift and cookie collisions. One profile per identity, shared through one hub,
  replaced it.

## 10. Scripted lookups: `hubctl run`

Measured 2026-11-09/11. Over 60 days, 24 of the 78 purchasing subagent runs still on disk
were plain price/stock re-checks of known products: about 120 lookups, a median of 6 browser calls
each, and about $0.15 of Sonnet per lookup. On the live profile, one agent re-checking six
products took 61 s, 20 tool calls and 82k tokens. The same six took `hubctl run` 9 s and none, with
identical prices, sellers and stock.

- **Its own tab, always.** The runner opens a new page in the profile's existing context (that is
  where the cookies are), and closes it in a `finally`. It never lists, adopts or closes another
  tab. The live suite checks the tab list before and after.
- **Its tab opens in the background.** `context.newPage()` activates the new tab, and on macOS
  headed Chrome then raises its window and takes keyboard focus from whatever the person is typing
  in. The runner uses CDP `Target.createTarget` with `background: true` instead; sampled every
  100 ms through a run, the frontmost app never changed (2026-11-11).
- **It connects with `noDefaults: true`**, as playwright-mcp's hub does. Without it Playwright
  points the shared context's downloads at a temp dir it deletes on disconnect, and turns on focus
  and media emulation for every tab.
- **It stamps the profile like `hubctl up`** (it calls it), before and after the run, so the
  reaper never stops Chrome under a lookup. It doesn't go through the hub at all: a script on CDP
  shares nothing with MCP clients' current-tab pointers.
- **playwright-core comes from the npx cache of the pinned playwright-mcp.** Nothing new to
  install, its version moves with `PW_MCP`, and extractors get the same `page` API agents use in
  `browser_run_code_unsafe`. The resolved path is cached per version in `var/run/`.
- **Three outcomes, as exit codes.** 0: complete. 3: a `required` field was empty or the item
  wasn't found, so the caller falls back to an agent for those items only. 4: a login or captcha
  wall, so the caller reports it rather than probing. A batch stops at the first wall.
- **A broken extractor can be wrong, not just empty.** Newegg preselects a "Buy Used" option on
  some listings, and the first version read $159 used for a $219 new item. Every agent run got it
  right. So extractors key on the labeled option (Buy New, the buy box's own condition), report
  the alternative separately (`usedPrice`, `condition`), and always return the final URL.


## 11. Agents don't take keyboard focus (best effort)

Measured 2026-11-11 on macOS, sampling the frontmost process every 100 ms. Three things raised a
headed agent Chrome over whatever the person was typing in:

1. **New tabs.** playwright-mcp's `browser_tabs new` (and `context.newPage()` generally) sends
   `Target.createTarget` without `background`, which activates the tab and its window: Chrome was
   frontmost for 12 of 14 samples through one agent's new/navigate/select/close.
2. **Selecting a tab.** playwright-mcp calls `page.bringToFront()` on every `browser_tabs select`.
3. **Cold starts.** launchd starting a headed Chrome makes it the active app, and it kept focus:
   78 of 80 samples over 8 s.

`lib/focus-guard.mjs` (on unless `FOCUS_GUARD=0`) fixes the first two for every agent. The hub's
launchd job runs the guard, which picks a free loopback port, starts playwright-mcp with
`--cdp-endpoint` pointing at itself, and relays CDP with three changes:
`Target.createTarget` opens a window of its own behind the others (`newWindow`, `background`), and
`Page.bringToFront` and `Target.activateTarget` are answered without reaching Chrome. Through the
guard, the same agent session left focus alone on every sample, and clicks, screenshots and tab
selection all worked.

- **A window per tab, not a background tab.** A background tab isn't rendered, so Playwright's
  click, which waits for an animation frame, timed out at 15 s in one (the live suite caught it).
  The active tab of a window that sits behind others keeps rendering, because the profiles run
  with `--disable-backgrounding-occluded-windows`.
- **It keeps §4's lifecycle.** The guard connects to Chrome only when the hub does, so a hub with
  no browser still serves its tools. The hub is the guard's child: its exit is the guard's exit,
  and launchd's signals are passed on, so a `kickstart -k` replaces the whole tree. When Chrome
  dies the relay closes the hub's socket, and the hub is poisoned exactly as before, until
  `hubctl up` restarts it.
- **Text stays text.** The relay gets frames as Buffers, and `ws` sends a Buffer as a binary
  frame unless told otherwise. Chrome's DevTools socket drops the connection on a binary frame.
- **A hidden tab still comes forward.** A link with `target=_blank` opens a second tab inside an
  agent's window, in front of the agent's own. Swallowing every bring-to-front would leave the
  original hidden, unrendered and unclickable. So before answering `Page.bringToFront` itself, the
  guard asks that tab for `document.visibilityState`, and forwards the call when it's `hidden`.
  That is the one case where an agent can still raise Chrome. No answer within 2 s counts as
  visible, so a call never hangs.
- **It fails open.** The relay needs the WebSocket library inside the hub's own playwright-core.
  If it can't load it, the guard logs that and starts the hub pointed straight at Chrome. Focus
  isn't guarded then, but the hub starts. A relay error is logged and doesn't end the process.
- **Known gaps.** A page's own `window.open` with window features (OAuth and payment popups) makes
  Chrome open and show a window without any client asking, so the guard never sees it. And after
  a cold start, focus goes back only when the frontmost app wasn't Chrome: the person's own
  Chrome has the same bundle id, and it can't be told apart reliably.
- **People still get windows.** `hubctl open` talks to Chrome directly (`/json/new`), so a tab
  someone asked for comes to the front. `hubctl run` creates its own tab as a background window, for the same rendering reason.

Cold starts are handled in `hubctl up`. It notes the frontmost app (`lsappinfo`, which needs no
permissions, unlike AppleScript) before launching Chrome. For the next 10 s, each time that
profile's Chrome process (matched by pid, since the person's own Chrome has the same bundle id) is
frontmost, it hands focus back with `open -b`. Chrome activates more than once while it starts,
so one hand-back wasn't enough. What remains is about 0.2 s. `hubctl open` skips this; it wants
the window.

## 12. Profiles on another Mac

Built 2026-11-24 so one Mac's sessions (a desktop, where every session runs) could drive the same
profiles on a laptop, whose screen is where someone can watch or take over.

- **The hub runs on the Mac that runs the Chrome.** Chrome's CDP port listens on loopback only,
  the focus guard has to sit next to Chrome, and `hubctl up`'s whole repair logic (§4, §7) is
  about that Mac's launchd. So the other Mac runs an ordinary browser-hub, and this Mac reaches
  its hub through a tunnel. A remote profile is nothing but that tunnel here.
- **ssh, not a network-bound hub.** The alternative was `--host <tailnet address>` on the remote
  hub. That serves an unauthenticated, logged-in browser to every device on the tailnet, which
  can include machines shared in from other people's tailnets. An ssh tunnel is authenticated,
  needs nothing exposed beyond Remote Login, and the same connection carries the remote `hubctl`
  commands.
- **The tunnel keeps the port.** playwright-mcp's HTTP server checks the Host header against
  `localhost:<its port>`, `127.0.0.1:<its port>` and `[::1]:<its port>` (DNS-rebinding
  protection, read in the 0.0.85 bundle: `startMcpHttpServer`), and answers anything else with a
  403. A tunnel from local 18932 to remote 8932 sends `Host: localhost:18932`. The ways around it
  were `--allowed-hosts '*'`, which drops the protection; a Host-rewriting proxy, which is one more
  process in the path; or the same port number on both ends. Hence each Mac has its own port range,
  and `add --remote` refuses a port in use here. `next_ports` skips remote profiles' ports
  without counting them toward the maximum, so a local `add` never wanders into the other range.
- **The tunnel plist is the record.** The ssh destination, the profile's name over there, the
  remote `hubctl` command and the port live in the tunnel job's `EnvironmentVariables`, the same
  way a local profile's ports live in its plists. There's still no registry to drift.
- **The tunnel is the ControlMaster.** One `ssh -N -M -L` per remote profile, lazy like a Chrome.
  Every other ssh for that profile (`up`, `run`, `open`, `info`, the rsync of a copy) rides its
  socket: 40 ms a command, measured desktop to laptop over the LAN. The hook's call costs
  ~100 ms in all. Whether the tunnel is up is asked of the master (`ssh -O check`, local), not by
  probing the forwarded port, which would open a channel to the other Mac on every hook call.
  `ServerAliveInterval 5` × 3 notices a sleeping laptop in ~15 s instead of
  hanging. A socket left by a dead ssh is removed before the next start, or the new ssh would
  silently refuse to be the master.
- **`up` delegates.** `hubctl up <remote-profile>` makes sure the tunnel is up and runs `hubctl up
  <profile>` on the other Mac. That side does the cold start, the owed hub restart, the lock and
  the stamp its own reaper reads, exactly as it would for one of its own sessions. Its reaper's
  connection guard also sees the tunnel's forwarded connections. This side's reaper only closes an
  idle tunnel. A remote failure never exits 3, because the hook reads 3 as "not a browser-hub
  profile" and would swallow it. While the tunnel is down, a failure fails fast for a minute (a
  sleeping laptop), like a local profile that failed to start. With it up, every call tries again:
  the other Mac's own `up` fails fast for its own failures.
- **Agents are copies.** A remote profile's agent is cloned from this Mac's agent for the profile
  of the same name, because that agent already describes the accounts. The clone changes the
  name, the MCP server name and URL, and the profile its `hubctl` commands name. Its description
  is replaced with one that keeps ordinary work on this Mac: the orchestrator routes by
  description, and a second agent with the same description would get half the work sent to a
  laptop. It also gets a section on what differs over there: files the browser saves land on the
  other Mac, and uploads must already be there.
- **Copies are one-way and held.** `push`/`pull` is rsync over the tunnel's master. Two cookie
  jars can't be merged safely, so each run makes one side the same as the other. Chrome must be
  down on both sides, because SQLite databases (Cookies, Login Data) copy torn while Chrome has
  them open. Both sides are held for the copy: here by the profile's `up` lock, and there by
  running the remote rsync under `hubctl held <profile>`, which takes that Mac's lock and refuses
  if its Chrome is up. The copy being replaced is kept as an APFS clone (`cp -c`), which is
  instant and free until the copies diverge, named `.before-copy-<time>`; the newest two are kept,
  so a second push by mistake doesn't destroy the only copy of what was there first. "Down" means
  the CDP port is closed and launchd says the process is gone: Chrome closes the port early in
  shutdown and writes its databases after. Excluded: lock files and `DevToolsActivePort` (host-
  and port-specific), caches, crash and metrics files, `OptGuideOnDeviceModel` (~4 GB), and the
  `NativeMessagingHosts` links, which each Mac makes for itself. rsync never deletes an excluded
  path at the destination, so each side keeps its own caches. Copying works at all because of
  `--use-mock-keychain --password-store=basic` (§1): the cookies aren't sealed to either Mac's
  Keychain (first done by hand 2026-07-29, 6.3 GB → 91 MB).
- **Open risk: two live copies of one session.** Sites that rotate session tokens (Google's
  `__Secure-*PSIDTS`, and Device Bound Session Credentials, whose `Device Bound Sessions` store
  is in every profile) may sign one copy out when the other refreshes. The fix is to sign in again
  and push. Check after each first copy that Google is still signed in on both Macs.

