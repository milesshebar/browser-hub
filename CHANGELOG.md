# Changelog

## 1.1.0 — 2026-11-25

**Upgrading.** Pull, then run `./install.sh` (or at least `hubctl regen`). That rewrites every
launchd job, but it does not restart what is already running: a running tunnel keeps its old
forward, and a hub keeps its old focus guard (the guard's code isn't in the plist, so `regen`
sees nothing to change). Until each restarts, the security fixes below don't apply to it. So:
`hubctl reload default` (this closes the default Chrome's tabs), `hubctl reload <profile>` for
every other profile `hubctl list` shows as up, and `hubctl reload <remote-profile>` for each
remote profile, plus the same on the other Mac. Profiles that are down pick everything up at
their next start. Then `hubctl doctor` should show no permission problems.

**New**
- Logins survive Chrome updates. `hubctl` saves each profile's cookies from the running Chrome
  when it stops it and hourly, and when a cold start finds a new Chrome version and an emptied
  jar, it puts them back. `hubctl cookies <profile> save|restore` does it by hand. Chrome 158 had
  silently dropped every login (DESIGN §1).
- Remote profiles: drive a profile whose Chrome runs on another Mac.
  `hubctl add <name> --remote <ssh-host>:<profile>` adds an ssh tunnel to the other Mac's hub and
  a `browser-<name>` agent, copied from this Mac's agent for the same profile. `up`, `down`,
  `open`, `run`, `lanes`, `doctor` and the reaper all handle it. `hubctl push` / `pull` copy a
  profile's data between the Macs (one way, both Chromes held down, the two newest replaced
  copies kept as APFS clones). `hubctl info` prints a profile's facts as `key=value`. See `docs/DESIGN.md` §12.
- `hubctl run <profile> <extractor> [id…]`: scripted lookups on known pages, with no model in
  the loop. Extractors live in `extractors/` (Amazon, Newegg, B&H, Micro Center).
  Each opens its own tab over CDP, checks for a login or captcha wall, extracts, and closes its
  tab. It uses the playwright-core that the pinned playwright-mcp already ships. New tabs open in
  the background, so a lookup never raises Chrome's window or takes keyboard focus.
  Search-style results (B&H `q:`) come back as `data.hits`. `hubctl run <profile> --check` runs
  each extractor against its own known-good ids. See `docs/DESIGN.md` §10.
- Agents don't take keyboard focus (best effort). Each hub reaches Chrome through `lib/focus-guard.mjs`, which
  opens every agent tab in a window of its own behind yours and swallows bring-to-front calls;
  cold starts hand focus back. `FOCUS_GUARD=0` turns it off. See `docs/DESIGN.md` §11.
- README "Trust model" section and `SECURITY.md`: what the unauthenticated loopback ports, the
  mock keychain and the plaintext cookie save mean, stated plainly.

**Security**
- The focus guard refuses any request that isn't from a local, non-browser client: Host must be
  `localhost`, `127.0.0.1` or `[::1]` at its port, and a request with `Origin` or
  `Sec-Fetch-Site` gets a 403. Before, it passed any Host and Origin through to Chrome, which
  bypassed Chrome's own DNS-rebinding and Origin checks.
- Remote-profile tunnels bind their forward to `localhost` with `GatewayPorts=no`. Before, a
  `GatewayPorts yes` in an ssh config served the unauthenticated hub to the network.
  `hubctl regen` rewrites existing tunnels.
- Profile directories are created 0700 whatever the umask, tightened before each Chrome start,
  and checked by `hubctl doctor`. Cookie saves and the recovery tools' output are written as new
  0600 files, never over an existing readable one.
- The agent rules serve local files with `http.server --bind 127.0.0.1 --directory <folder>`.

**Changed**
- Chrome starts with no window and no tab (`--no-startup-window`). The per-profile label page
  is gone: on macOS a headed Chrome keeps running with no windows (`docs/DESIGN.md` §1).
- playwright-mcp pinned at 0.0.85 (was 0.0.78). Re-verified against `docs/DESIGN.md` §8, and the
  live suite passes on it.
- Hubs run with `--snapshot-mode none` (`SNAPSHOT_MODE` in the config; `full` restores the old
  behavior). Clicks and navigations no longer write a page snapshot, and they no longer repeat the
  open-tab list unless a tab changed. Over the past 60 days that list alone ran to roughly 0.9M
  tokens.
- On 0.0.85 and later, hubs also get `--file-paths absolute` and `--no-webmcp`.
  A config still pinned to an older version gets none of them, because they would stop its hub
  from starting.
- Agent template and CLAUDE.md rules: get refs with `browser_find` or `browser_snapshot`. Save
  evidence screenshots with a `filename`, which keeps the image out of the conversation. Avoid
  `fullPage`. Read text with `browser_evaluate`.

## 1.0.0 — 2026-09-24

First release meant to run on any Mac. Before this it was one machine's setup, with that
machine's paths, labels and node install hardcoded into the scripts and plists.

**New**
- One config file (`~/.config/browser-hub/config`, documented in `config.example`) holds
  everything machine-specific. launchd plists are generated from it (`hubctl regen`) instead of
  being hand-edited.
- `install.sh` and `uninstall.sh`. Install is idempotent and has a `--dry-run`. It registers the
  MCP server, the hooks and a marked CLAUDE.md rules block, backs up every file it edits, and
  leaves anything that isn't its own alone. Uninstall keeps profile data, config and logs.
- `hubctl doctor` checks the whole install, including the Claude Code side, and prints a fix for
  each problem.
- `hubctl open` (start a profile and open tabs in it, e.g. to sign in), `hubctl regen`,
  `hubctl reload`, `hubctl agent`, and `hubctl add --identity/--ports`.
- A test suite: unit tests in a sandbox, plus live tests that run a second, isolated hub through
  install, cold start, real browsing, crash repair, concurrent starts, config changes to running
  jobs, the reaper and uninstall.
- Several installs can share a machine (a sandbox next to a real one): hooks and the CLAUDE.md
  block are tagged with the config that wrote them, and one install never rewrites or removes
  another's (`--force-hooks` takes over leftovers). `BROWSER_HUB_CONFIG` selects the install;
  generated agents and rules carry it in their recovery commands.
- An agent template with safety rules (page text is data; don't snapshot login or payment pages;
  nothing is bought, submitted, sent or deleted without explicit confirmation) and cheaper
  browsing habits.

**Fixed**
- `hubctl add` created always-on profiles (`RunAtLoad`/`KeepAlive` true), contradicting the
  lazy-Chrome policy. New profiles are now lazy, like every other.
- The reaper only knew a hardcoded list of profiles. It now covers every profile.
- Recovery advice said to `launchctl kickstart` the Chrome job. That leaves the hub holding a dead
  browser context, so recovery is now `hubctl up <profile>` everywhere.
- Concurrent cold starts of one profile (e.g. several lanes spawned together) could restart the
  hub under each other. `hubctl up` now takes a per-profile lock.
- A plist edited while its job was running silently kept the old definition. It's now marked and
  applied at the job's next cold start.
- `hubctl rm` left lane agents behind. It now removes exactly the profile's own agents, and
  `uninstall.sh --remove-agents` likewise, never a hand-written agent that points at a hub.
- An interrupted cold start could leave a hub holding a dead browser context behind a fast path
  that never repaired it. The pending hub restart is now recorded on disk.

**Changed**
- The hook no longer starts Python on the tool-call path. It also covers browser agents' own tool
  calls, so a profile reaped mid-conversation comes straight back.
- Every hub gets `--timeout-action 15000`, and every Chrome gets a 200 MB disk-cache cap.
- New installs keep browser profiles in `~/.local/share/browser-hub/profiles`, not under
  `~/.claude` (which many people keep in git). Existing configs keep their paths.
- If a different MCP server named `playwright` exists, install stops before touching `~/.claude`.
- Paths with spaces are refused up front, rather than failing later.
- Logs, screenshots, generated plists and run stamps all live under `var/`.
- The reaper prunes old screenshots and rotates logs.
- Removed: hub-less "machine-only" profiles, extra startup URLs, `hubctl start/stop/restart`
  (use `up`, `down`, `reload`).
