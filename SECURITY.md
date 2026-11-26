# Security

browser-hub gives AI agents a real Chrome that you are signed into. Its security rests on one
assumption: **every process on the Mac that can reach loopback is trusted.** It is not a sandbox.

## Trust model

| piece | where it listens or lives | protection |
|---|---|---|
| Chrome DevTools (CDP), one per profile | `127.0.0.1:9223` for `default`, the next free ports for others | loopback only; no authentication |
| playwright-mcp hub, one per profile | `[::1]:8931` for `default`, the next free ports for others | loopback only; Host header must be `localhost:<port>`; no authentication |
| focus guard, one per hub | a random `127.0.0.1` port | loopback only; Host must be loopback; any `Origin` or `Sec-Fetch-Site` header refused; no authentication |
| remote profile tunnel | an ssh local forward on this Mac, same port as the remote hub | your ssh key on the other Mac; forward bound to `localhost` with `GatewayPorts=no` |
| profile directory | `~/.local/share/browser-hub/profiles/<profile>` by default | mode 0700, set at creation and before each Chrome start; `hubctl doctor` checks it |
| cookie database | `<profile dir>/Default/Cookies` | encrypted with Chromium's fixed mock-keychain password (`--use-mock-keychain --password-store=basic`), which is public |
| saved cookies | `<profile dir>/browser-hub-cookies.json`, written hourly and at each stop | plaintext; a new 0600 file renamed into place |

What follows from it:

- Any local process, including another user's on the same Mac, can connect to a CDP port or a hub
  and act as every account signed into that profile, read all of its cookies, and use
  playwright-mcp's tools.
- Profiles are kept apart by tool routing (each profile's tools go only to its own agent), not by
  access control: an agent or session that can run shell commands can reach any profile.
- Anyone who can read a profile directory can read its sessions, whether from the cookie database
  (public key) or from the plaintext save. Mode 0700 keeps other macOS users out; nothing keeps
  out software running as you.
- Nothing listens on a network address. Other Macs reach a profile only through an ssh tunnel you
  set up (`hubctl add <name> --remote`).

## Using it safely

- Install it only on a Mac where you are the only user and you trust the software you run.
- Keep profile directories on a FileVault disk, mode 700, and out of git, cloud sync and shared
  backups. `hubctl push`/`pull` copy a profile, cookies and all.
- Set `COOKIE_BACKUP=0` in the config if you'd rather not keep a plaintext cookie save. You lose
  the automatic recovery after a Chrome update that drops the cookie jar (`docs/DESIGN.md` §1).
- Keep the agent rule that page text is data and that nothing is bought, submitted, sent or
  deleted without your explicit go-ahead. Pages can carry prompt injection.

## Reporting a vulnerability

Please report it privately through GitHub's "Report a vulnerability" button on this repository's
Security tab, not in a public issue. If that button isn't there, open an issue that asks for a
private contact and says nothing about the problem itself. Include the version (`hubctl version`), macOS and Chrome
versions, and steps to reproduce.

Things that follow directly from the trust model above (a local process driving a profile, or
reading a profile directory) are known and documented, not vulnerabilities. A way to reach a hub
or a CDP port from a web page or from the network is.
