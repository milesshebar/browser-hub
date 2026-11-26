# AGENTS.md: for an AI agent installing, running or changing browser-hub

Read `README.md` first for what this is. This file covers what you need to do the work without
breaking anything. `CLAUDE.md` is a symlink to this file.

## Installing it for someone

1. **Ask, don't guess**, about these:
   - Which identities do they want as separate profiles (e.g. "work Google + Jira", "a client's
     AWS")? Everything else goes in `default`.
   - Do they already have an MCP server named `playwright`? (`claude mcp get playwright`.) If so,
     it's replaced only with their OK (`./install.sh --force-mcp`).
   - Is anything already on ports 9223 or 8931? `install.sh` checks and refuses. Set
     `DEFAULT_CDP_PORT` / `DEFAULT_HUB_PORT` in the config if so.
2. Run `./install.sh --dry-run`, show them what it will touch, then run `./install.sh`. It ends
   with `hubctl doctor`, which must say `healthy` (warnings are fine; read them).
3. Describe the default profile so work gets routed to it:
   `./hubctl agent default --identity "<accounts>" --force`. For each extra identity:
   `./hubctl add <name> --identity "<accounts>"`.
4. **The human signs in.** Never type their passwords, even if you could. Run
   `./hubctl open <profile> https://accounts.google.com` (and any other sites), and let them sign
   in and install extensions (e.g. 1Password) in that window.
5. Tell them to restart their Claude Code sessions. Hooks, MCP servers and agent files all load at
   session start.
6. Check it for real, from a *new* session: call `mcp__playwright__browser_tabs` (action `list`),
   and spawn a `browser-<profile>` agent that opens a page and reports its title.

Run `tests/run.sh --live` if you want proof beyond `doctor`. It's isolated from their live setup.

**If this machine already runs browser-hub** (or you just want to try it first), don't touch that
install. Build a sandbox as described in README § "Trying it without touching an existing setup":
a temp config via `BROWSER_HUB_CONFIG` with its own label prefix, ports and directories; `--no-mcp`
on install, uninstall and `doctor`; `CLAUDE_BIN` for a stub `claude`. Never run `launchctl`
against labels outside your sandbox's prefix.

## Invariants: don't break these

Each one was learned the hard way. `docs/DESIGN.md` has the evidence.

1. **Chrome keeps `--use-mock-keychain --password-store=basic`.** The profile's cookies are
   encrypted under that mock keychain. Launch the profile without those flags and every login
   becomes unreadable. Never start Chrome on a profile except through `hubctl`.
2. **The hub keeps `--shared-browser-context`.** That's the multi-client mode. Without it, a second
   client evicts the first.
3. **The default hub is always on** (`RunAtLoad`/`KeepAlive` true). Sessions connect to it at
   startup and never retry. Every Chrome, and every other hub, is lazy.
4. **Whenever `hubctl up` has to start Chrome, it also restarts the hub.** A hub whose Chrome died
   fails every call until it restarts. Starting Chrome alone (e.g. `launchctl kickstart` on the
   Chrome job) leaves the hub broken. Always recover with `hubctl up <profile>`.
5. **Chrome starts with no window (`--no-startup-window`) and needs no tab to stay up.** On macOS a
   headed Chrome keeps running with no windows (DESIGN §1). Don't add a startup tab back: with
   the focus guard it becomes a stray window of its own.
6. **Extra profiles' hubs are declared only inline in their agent files**, never in
   `~/.claude.json`. That keeps their tools out of the main session's context, and each identity
   reachable only on purpose.
7. **Every agent/lane has a distinct MCP server name.** The server name is the connection: two
   concurrent agents with the same name share one current-tab pointer.
8. **The hook always exits 0, fast, and quietly unless something is really wrong.** It runs before
   every browser call: exit 2 would block the call, and anything slow or noisy is paid every time.
9. **Profile directories are never committed or synced, and stay private (0700).** The one way
   one leaves the machine is `hubctl push`/`pull` to the user's own other Mac, and only when they
   ask for it.
10. **Agents don't take keyboard focus** (best effort; DESIGN §11 lists the exceptions). Hubs reach
    Chrome through `lib/focus-guard.mjs`. Keep its frames' text/binary type when relaying, and
    give each agent tab its own window: a background tab doesn't render, and clicks in one time
    out. Only `hubctl open` raises a window on purpose. The guard also refuses any request that
    isn't from a local, non-browser client (Host, Origin, Sec-Fetch-Site): keep that check.
11. **A remote profile's tunnel keeps the hub's port number on both ends**, and each Mac keeps its
    own port range. playwright-mcp 403s any Host header but `localhost:<its own port>`. Hubs never
    listen on a network address; other Macs come in over ssh only.
12. **A remote `up` never exits 3.** The hook reads 3 as "not ours" and would hide the failure.
13. **A cookie save never overwrites the saved copy with a jar that collapsed after a Chrome
    update**, and the copy is always read from the running Chrome, never from its database: after
    an update, the database is in a format the new version can't read (DESIGN §1).

## Where things live

| thing | location | notes |
|---|---|---|
| CLI | `hubctl` | all commands; `hubctl help` |
| shared code | `lib/common.zsh` | config loading, naming, plist writers, launchd helpers |
| Claude Code file edits | `lib/claude_config.py` | settings.json hooks, CLAUDE.md block, lane cloning |
| hook | `ensure-profile.sh` | runs `hubctl up` for the profile a call needs |
| remote profiles | `tunnel_plist`, `remote_hubctl` in `lib/common.zsh`; `up_remote`, `cmd_copy` in `hubctl` | DESIGN §12 |
| templates | `templates/` | agent file, CLAUDE.md rules block |
| config | `~/.config/browser-hub/config`, or whatever `$BROWSER_HUB_CONFIG` names | `config.example` documents every key |
| test/sandbox knobs | `BROWSER_HUB_CONFIG`, `CLAUDE_BIN` (stub `claude`), `BH_TEST_*` in `tests/test_live.py` | |
| generated state | `var/` | plists, logs, screenshots, run stamps; gitignored |

## Changing it

- Launchd job contents come from the writers in `lib/common.zsh` (`chrome_plist`, `hub_plist`,
  `reaper_plist`). Change a writer, then run `hubctl regen`. It rewrites only the plists that
  changed, restarts the always-on hub at once (safe for live sessions), and marks a running
  Chrome to pick up the change at its next cold start. It never kills a Chrome someone's using.
- Agent wording is in `templates/agent.md`. `hubctl agent <p> --force` rewrites that agent; lanes
  are regenerated with `hubctl lanes <p> <n>`. Keep the frontmatter shape: `mcpServers` is a
  **list** of single-key maps.
- The rules agents follow are in `templates/claude-md.md`. `./install.sh` rewrites the marked block
  in `~/.claude/CLAUDE.md`, and leaves alone a hand-written section without markers.
- Run `tests/run.sh` after any change, and `tests/run.sh --live` after touching `hubctl`,
  `lib/common.zsh` or `ensure-profile.sh`. Add a test for whatever you fixed.
- Style: zsh, `setopt no_unset`. Comments explain *why*, not what.

## Upgrading playwright-mcp

`PW_MCP` in the config pins the version. Before bumping it:

1. Run `tests/run.sh --live` with `BH_TEST_PW_MCP=@playwright/mcp@<new>`.
2. Re-check the behaviors in `docs/DESIGN.md` § "playwright-mcp behaviors this depends on". In
   particular: the hub still serves tools with no browser behind it; a hub restart still repairs a
   dead-Chrome context; `--shared-browser-context` and `--timeout-action` still exist; and how a
   screenshot `filename` resolves.
3. If the new version adds hub flags you want, put them in `hub_plist` behind `pw_mcp_at_least
   <version>`. playwright-mcp exits on an unknown option, so an unguarded flag takes down the
   always-on hub for any config still pinned to an older version.
4. Set `PW_MCP` and run `hubctl regen`. Each hub picks up the new version at its next restart.

## Common adaptations

- **Existing agent should use a profile:** give it an inline `mcpServers` entry pointing at the
  profile's hub URL, with a server name that's unique among agents and starts with `browser-`
  (the hook only sees tool calls from `mcp__playwright__*` and `mcp__browser-*`). Then add the
  agent's name, and its server name, to `AGENT_PROFILES` in the config, so the hook starts the
  right Chrome both at spawn and mid-conversation.
- **Another install's leftovers in `~/.claude`:** hooks and the CLAUDE.md block are tagged with
  the config of the install that wrote them, and an install never touches another's. If the other
  one is truly gone, `./install.sh --force-hooks` takes them over.
- **Headless machine:** set `CHROME_EXTRA_ARGS=(--headless=new)`, then run `hubctl regen`. Nobody
  can sign in through a headless window, though, so sign profiles in while headed first.
- **Extractors that shouldn't live in this repo:** put them in a folder of their own and list it
  in `EXTRACTOR_DIRS` in the config (README, "Look things up without a model").
- **Different ports, or a different Chrome build:** edit the config, then run `hubctl regen`.
- **Drive a profile on another Mac:** install browser-hub there too, with a port range of its own
  (`DEFAULT_CDP_PORT`/`DEFAULT_HUB_PORT`), then `hubctl add <host>-<profile> --remote
  <ssh-host>:<profile>` here and `hubctl push <host>-<profile> --stop` to copy the logins over.
  Ask before the push: it replaces the other Mac's copy, and `--stop` closes both Chromes' tabs.
