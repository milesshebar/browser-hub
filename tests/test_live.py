"""Live tests: real launchd jobs, a real Chrome and a real playwright-mcp hub.

They run a SECOND, fully isolated hub next to any real one: their own launchd labels
(test.browserhub.<pid>.*), their own ports (19223+/18931+), a temp dir for every path, a stub
`claude` CLI, and headless Chrome (BH_TEST_HEADED=1 shows the windows). Nothing is written to
~/Library/LaunchAgents, ~/.claude or your real profiles, and everything is torn down afterwards.

    BROWSER_HUB_LIVE=1 python3 -m unittest tests.test_live -v      (or: tests/run.sh --live)

Knobs: BH_TEST_NODE_BIN (default: npx on PATH), BH_TEST_PW_MCP (default: the pinned version).
Takes about a minute.
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_client import MCPError, Session  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.environ.get("BROWSER_HUB_LIVE") == "1"
UID = os.getuid()
PORTS = {"cdp": 19223, "hub": 18931, "cdp1": 19224, "hub1": 18932}


def port_up(port):
    # "localhost", not 127.0.0.1: playwright-mcp binds whichever address localhost resolves to
    # first, which on macOS is usually ::1.
    try:
        socket.create_connection(("localhost", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def wait(pred, secs, what):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out after {secs}s waiting for {what}")


@unittest.skipUnless(LIVE, "set BROWSER_HUB_LIVE=1 to run the live tests")
class TestLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        busy = [p for p in PORTS.values() if port_up(p)]
        if busy:
            raise unittest.SkipTest(f"test ports already in use: {busy}")
        cls.tmp = tempfile.mkdtemp(prefix="bh-live-")
        cls.prefix = f"test.browserhub.{os.getpid()}"
        node = os.environ.get("BH_TEST_NODE_BIN") or os.path.dirname(shutil.which("npx") or "")
        pw = os.environ.get("BH_TEST_PW_MCP")
        headless = "" if os.environ.get("BH_TEST_HEADED") else "--headless=new"
        cls.config = os.path.join(cls.tmp, "config")
        with open(cls.config, "w") as f:
            f.write(textwrap.dedent(f"""\
                LABEL_PREFIX={cls.prefix}
                NODE_BIN={node}
                PROFILES_DIR={cls.tmp}/profiles
                STATE_DIR={cls.tmp}/var
                LAUNCH_AGENTS_DIR={cls.tmp}/LaunchAgents
                AGENTS_DIR={cls.tmp}/agents
                CLAUDE_MD={cls.tmp}/CLAUDE.md
                CLAUDE_SETTINGS={cls.tmp}/settings.json
                DEFAULT_CDP_PORT={PORTS['cdp']}
                DEFAULT_HUB_PORT={PORTS['hub']}
                CHROME_EXTRA_ARGS=({headless})
                """) + (f"PW_MCP={pw}\n" if pw else ""))
        # A stand-in for the `claude` CLI: remembers one MCP server in a file.
        cls.claude = os.path.join(cls.tmp, "claude")
        with open(cls.claude, "w") as f:
            f.write(textwrap.dedent(f"""\
                #!/bin/zsh
                f={cls.tmp}/mcp-registration
                case "$1 $2" in
                  "mcp get")    [[ -f $f ]] && {{ print "$3:"; cat $f; exit 0 }}; print "No MCP server found"; exit 1 ;;
                  "mcp add")    print -r -- "${{@[-1]}}" > $f ;;
                  "mcp remove") rm -f $f ;;
                esac
                """))
        os.chmod(cls.claude, 0o755)
        cls.env = {**os.environ, "BROWSER_HUB_CONFIG": cls.config, "CLAUDE_BIN": cls.claude}
        # A page to browse to (file:// is blocked by playwright-mcp).
        site = os.path.join(cls.tmp, "site")
        os.makedirs(site)
        with open(os.path.join(site, "index.html"), "w") as f:
            f.write("<title>bh-live-test</title><h1>hello</h1><button>Go</button>")
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=site, **k)  # noqa: E731
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.site = f"http://127.0.0.1:{cls.httpd.server_address[1]}/"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        jobs = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        for line in jobs.splitlines():
            label = line.split("\t")[-1]
            if label.startswith(cls.prefix + "."):
                subprocess.run(["launchctl", "bootout", f"gui/{UID}/{label}"], capture_output=True)
        subprocess.run(["pkill", "-f", f"user-data-dir={cls.tmp}/profiles"], capture_output=True)
        time.sleep(1)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers --
    def hubctl(self, *args, check=True):
        r = subprocess.run([os.path.join(ROOT, "hubctl"), *args], env=self.env,
                           capture_output=True, text=True, timeout=180)
        if check:
            self.assertEqual(r.returncode, 0, f"hubctl {' '.join(args)}:\n{r.stdout}\n{r.stderr}")
        return r

    def label(self, job, profile="default"):
        return f"{self.prefix}.{job}" + ("" if profile == "default" else f"-{profile}")

    def pid(self, label):
        out = subprocess.run(["launchctl", "print", f"gui/{UID}/{label}"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.startswith("\tpid = "):
                return int(line.split(" = ")[1])
        return None

    def loaded_args(self, label):
        return subprocess.run(["launchctl", "print", f"gui/{UID}/{label}"],
                              capture_output=True, text=True).stdout

    def chrome_mains(self, profile):
        out = subprocess.run(["pgrep", "-fl", f"user-data-dir={self.tmp}/profiles/{profile}"],
                             capture_output=True, text=True).stdout
        return [ln for ln in out.splitlines() if "--type=" not in ln]

    def hook(self, payload):
        return subprocess.run([os.path.join(ROOT, "ensure-profile.sh")], input=json.dumps(payload),
                              env=self.env, capture_output=True, text=True, timeout=180)

    def browse(self, port):
        """Open our own tab, load the test page, read its title, close the tab."""
        s = Session(port)
        try:
            s.call("browser_tabs", action="new")
            s.call("browser_navigate", url=self.site)
            title = s.call("browser_evaluate", function="() => document.title")
            self.assertIn("bh-live-test", title)
            tabs = s.call("browser_tabs", action="list")
            mine = [ln for ln in tabs.splitlines() if self.site in ln]
            self.assertTrue(mine, tabs)
            s.call("browser_tabs", action="close")
        finally:
            s.close()

    # ------------------------------------------------------------------ steps --
    def test_01_install(self):
        r = subprocess.run([os.path.join(ROOT, "install.sh")], env=self.env,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("healthy", r.stdout)
        self.assertTrue(port_up(PORTS["hub"]), "the default hub is always on")
        self.assertFalse(port_up(PORTS["cdp"]), "Chrome must stay down until something needs it")
        with open(f"{self.tmp}/settings.json") as f:
            self.assertIn("ensure-profile.sh", f.read())
        with open(f"{self.tmp}/CLAUDE.md") as f:
            self.assertIn("browser-hub:begin", f.read())
        with open(f"{self.tmp}/mcp-registration") as f:
            self.assertEqual(f.read().strip(), f"http://localhost:{PORTS['hub']}/mcp")
        self.assertTrue(os.path.exists(f"{self.tmp}/agents/browser-default.md"))
        # Re-running is a no-op that stays healthy.
        r = subprocess.run([os.path.join(ROOT, "install.sh")], env=self.env,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("hooks already registered", r.stdout)

    def test_02_a_cold_hub_still_serves_its_tools(self):
        s = Session(PORTS["hub"])
        try:
            tools = s.tools()
        finally:
            s.close()
        for t in ["browser_navigate", "browser_tabs", "browser_evaluate", "browser_snapshot"]:
            self.assertIn(t, tools)
        self.assertFalse(port_up(PORTS["cdp"]))

    def test_03_up_starts_chrome_and_is_then_a_cheap_no_op(self):
        t = time.time()
        self.hubctl("up")
        cold = time.time() - t
        self.assertTrue(port_up(PORTS["cdp"]))
        self.assertEqual(len(self.chrome_mains("default")), 1)
        t = time.time()
        r = self.hubctl("up")
        warm = time.time() - t
        self.assertIn("already up", r.stdout)
        self.assertLess(warm, 1.0)
        print(f"\n    cold start {cold:.1f}s, warm no-op {warm * 1000:.0f} ms", end="")

    def test_04_browse_end_to_end(self):
        self.browse(PORTS["hub"])

    def test_04b_hub_flags_that_the_agent_rules_depend_on(self):
        s = Session(PORTS["hub"])
        out = f"{self.tmp}/var/output/default"
        try:
            s.call("browser_tabs", action="new")
            s.call("browser_navigate", url=self.site)
            # --snapshot-mode none: an action returns no page snapshot, and refs come on request.
            snap = s.call("browser_snapshot")
            ref = snap.split('button "Go" [ref=')[1].split("]")[0]
            click = s.call("browser_click", target=ref)
            self.assertNotIn("### Snapshot", click)
            self.assertNotIn("### Open tabs", click, "an unchanged tab list isn't repeated")
            # A screenshot saved with a filename is not sent back as an image, and
            # --file-paths absolute makes its link usable from any session's cwd.
            res = s._rpc("tools/call", {"name": "browser_take_screenshot",
                                        "arguments": {"filename": f"{out}/shot.png"}})
            self.assertFalse([c for c in res["content"] if c.get("type") == "image"])
            self.assertIn(f"]({out}/shot.png)", res["content"][0]["text"])
            self.assertTrue(os.path.exists(f"{out}/shot.png"))
            # In a shared context browser_close is refused rather than breaking the caller.
            with self.assertRaises(MCPError):
                s.call("browser_close")
            self.assertIn(self.site, s.call("browser_tabs", action="list"))
        finally:
            try:
                s.call("browser_tabs", action="close")
            except MCPError:
                pass
            s.close()

    def test_05_chrome_dying_under_a_live_hub_is_repaired_by_up(self):
        a = Session(PORTS["hub"])
        a.call("browser_tabs", action="list")
        hub_pid = self.pid(self.label("playwright-hub"))
        subprocess.run(["launchctl", "kill", "SIGKILL", f"gui/{UID}/{self.label('agent-chrome')}"])
        wait(lambda: not port_up(PORTS["cdp"]), 20, "Chrome to die")
        try:
            a.call("browser_tabs", action="list")
            broken = False
        except (MCPError, OSError):
            broken = True
        a.close()
        print(f"\n    old session after Chrome died: {'broken (as documented)' if broken else 'still worked'}",
              end="")
        self.hubctl("up")
        self.assertNotEqual(self.pid(self.label("playwright-hub")), hub_pid, "up must restart the hub")
        self.browse(PORTS["hub"])

    def test_05b_an_interrupted_cold_start_still_gets_its_hub_restart(self):
        # Simulate `hubctl up` killed after starting Chrome but before restarting the hub: both
        # ports answer, yet the hub may hold a dead context. The next `up` must not take the fast path.
        self.assertTrue(port_up(PORTS["cdp"]) and port_up(PORTS["hub"]))
        open(f"{self.tmp}/var/run/hub-restart-owed-default", "w").close()
        hub_pid = self.pid(self.label("playwright-hub"))
        r = self.hubctl("up")
        self.assertNotIn("already up", r.stdout)
        self.assertNotEqual(self.pid(self.label("playwright-hub")), hub_pid)
        self.assertFalse(os.path.exists(f"{self.tmp}/var/run/hub-restart-owed-default"))
        self.browse(PORTS["hub"])

    def test_06_add_a_profile_and_start_it_from_three_places_at_once(self):
        r = self.hubctl("add", "t1", "--identity", "the live test")
        self.assertIn(f"CDP {PORTS['cdp1']}", r.stdout)
        with open(f"{self.tmp}/agents/browser-t1.md") as f:
            self.assertIn(f"localhost:{PORTS['hub1']}/mcp", f.read())
        self.assertFalse(port_up(PORTS["cdp1"]), "add must not start Chrome")
        procs = [subprocess.Popen([os.path.join(ROOT, "hubctl"), "up", "t1"], env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(3)]
        outs = [p.communicate(timeout=180) for p in procs]
        for p, (o, e) in zip(procs, outs):
            self.assertEqual(p.returncode, 0, o + e)
        self.assertEqual(len(self.chrome_mains("t1")), 1, "exactly one Chrome per profile")
        self.assertTrue(port_up(PORTS["hub1"]))
        self.browse(PORTS["hub1"])

    def test_07_the_hook_starts_a_cold_profile(self):
        self.hubctl("down", "t1")
        self.assertFalse(port_up(PORTS["cdp1"]))
        self.assertFalse(port_up(PORTS["hub1"]), "a lazy hub goes down with its Chrome")
        r = self.hook({"tool_name": "Agent", "tool_input": {"subagent_type": "browser-t1-2"}})
        self.assertEqual(r.returncode, 0)
        self.assertTrue(port_up(PORTS["cdp1"]) and port_up(PORTS["hub1"]), r.stderr)
        self.hubctl("down", "t1")
        r = self.hook({"tool_name": "mcp__browser-t1__browser_tabs", "tool_input": {}})
        self.assertTrue(port_up(PORTS["cdp1"]) and port_up(PORTS["hub1"]), r.stderr)
        self.browse(PORTS["hub1"])

    def test_08_config_changes_reach_running_jobs_safely(self):
        with open(self.config, "a") as f:
            f.write("ACTION_TIMEOUT_MS=16000\n")
            f.write("CHROME_EXTRA_ARGS+=(--bh-test-flag)\n")
        r = self.hubctl("regen")
        # The always-on hub restarts at once; t1 is busy, so its jobs are only marked.
        self.assertIn("16000", self.loaded_args(self.label("playwright-hub")))
        self.assertIn("is running", r.stdout)
        self.assertNotIn("16000", self.loaded_args(self.label("playwright-hub", "t1")))
        self.assertEqual(len(self.chrome_mains("t1")), 1, "regen must not kill a running Chrome")
        # ...and the change lands at the next cold start.
        self.hubctl("down", "t1")
        self.hubctl("up", "t1")
        self.assertIn("16000", self.loaded_args(self.label("playwright-hub", "t1")))
        self.assertTrue(any("--bh-test-flag" in ln for ln in self.chrome_mains("t1")))
        self.assertEqual(os.listdir(f"{self.tmp}/var/run").count(
            f"stale-{self.label('agent-chrome', 't1')}"), 0)
        self.browse(PORTS["hub1"])

    def test_09_the_reaper_stops_idle_profiles_only(self):
        self.hubctl("up")
        r = self.hubctl("reap")
        self.assertTrue(port_up(PORTS["cdp"]) and port_up(PORTS["cdp1"]), "fresh stamps: nothing reaped")
        old = time.time() - 3 * 3600
        for p in ["default", "t1"]:
            os.utime(f"{self.tmp}/var/run/last-use-{p}", (old, old))
        # A client connected to t1's hub holds the reaper off, however stale the stamp...
        held = socket.create_connection(("localhost", PORTS["hub1"]))
        try:
            r = self.hubctl("reap")
            self.assertNotIn("reaped t1", r.stdout)
            self.assertTrue(port_up(PORTS["cdp1"]), "a connected client must keep its profile up")
            # (the default profile has no such guard — every session holds its hub — so it goes)
            self.assertIn("reaped default", r.stdout)
        finally:
            held.close()
        time.sleep(0.5)
        # ...and once the client is gone, t1 goes too.
        r = self.hubctl("reap")
        self.assertIn("reaped t1", r.stdout)
        self.assertFalse(port_up(PORTS["cdp"]) or port_up(PORTS["cdp1"]) or port_up(PORTS["hub1"]))
        self.assertTrue(port_up(PORTS["hub"]), "the default hub survives a reap")
        # and a reaped default profile comes straight back
        self.hubctl("up")
        self.browse(PORTS["hub"])

    def test_09c_logins_survive_a_chrome_update_that_drops_them(self):
        """Chrome 158 dropped every cookie it couldn't decrypt at its first launch (DESIGN §1). Fake
        one: save, empty the jar, make the saved copy look like an older Chrome's, then cold-start."""
        self.hubctl("up")
        def in_my_tab(js):
            s = Session(PORTS["hub"])
            try:
                s.call("browser_tabs", action="new")
                s.call("browser_navigate", url=self.site)
                out = s.call("browser_evaluate", function=js)
                s.call("browser_tabs", action="close")
                return out
            finally:
                s.close()
        in_my_tab("() => { document.cookie = 'bhlive=kept; max-age=86400; path=/'; return document.cookie }")
        self.hubctl("cookies", "default", "save")
        f = f"{self.tmp}/profiles/default/browser-hub-cookies.json"
        self.assertEqual(os.stat(f).st_mode & 0o777, 0o600, "live session secrets: owner-only")
        saved = json.load(open(f))
        self.assertIn("bhlive", [c["name"] for c in saved["cookies"]])
        saved["chrome"] = "0.0.0.0"                       # as if an older Chrome had written it
        with open(f, "w") as fh:
            json.dump(saved, fh)
        in_my_tab("() => { document.cookie = 'bhlive=; max-age=0; path=/'; return document.cookie }")
        # Stopping saves first, but a jar that collapsed under a new version never replaces the copy.
        self.hubctl("down")
        self.assertEqual(json.load(open(f))["chrome"], "0.0.0.0")
        self.assertIn("refuse", open(f"{self.tmp}/var/log/cookies.log").read())
        # The cold start sees the version change and the empty jar, and puts the cookies back.
        self.hubctl("up")
        self.assertIn("kept", in_my_tab("() => document.cookie"))
        now = json.load(open(f))
        self.assertNotEqual(now["chrome"], "0.0.0.0", "re-saved under the running version")
        self.assertEqual(json.load(open(f + ".prev"))["chrome"], "0.0.0.0", "the old copy is kept once")
        self.assertIn("restored", open(f"{self.tmp}/var/log/cookies.log").read())

    def test_10_open_puts_a_tab_in_the_right_browser(self):
        self.hubctl("open", "t1", self.site)
        import urllib.request
        with urllib.request.urlopen(f"http://localhost:{PORTS['cdp1']}/json/list") as r:
            self.assertIn(self.site, [t["url"] for t in json.load(r)])

    def test_10b_run_uses_its_own_tab_and_extends_the_lease(self):
        import urllib.request
        tabs = lambda: sorted(t["id"] for t in json.load(urllib.request.urlopen(  # noqa: E731
            f"http://localhost:{PORTS['cdp1']}/json/list")) if t["type"] == "page")
        ex = os.path.join(self.tmp, "title.js")
        with open(ex, "w") as f:
            f.write("export const name = 'title';\n"
                    "export const url = u => u;\n"
                    "export async function lookup(page, u) {\n"
                    "  await page.goto(u);\n"
                    "  return { status: 'ok', data: { title: await page.title(),\n"
                    "    h1: await page.textContent('h1') } };\n}\n")
        stamp = f"{self.tmp}/var/run/last-use-t1"
        os.utime(stamp, (time.time() - 3600, time.time() - 3600))
        before = tabs()
        r = self.hubctl("run", "t1", ex, self.site)
        out = json.loads(r.stdout)
        self.assertEqual(out["items"][0]["status"], "ok")
        self.assertEqual(out["items"][0]["data"], {"title": "bh-live-test", "h1": "hello"})
        self.assertEqual(tabs(), before, "run closes its own tab and touches no other")
        self.assertLess(time.time() - os.stat(stamp).st_mtime, 60, "run stamps the profile")
        # A failing lookup still closes its tab, and says so through the exit code.
        with open(ex, "a") as f:
            f.write("export const required = ['missing'];\n")
        r = self.hubctl("run", "t1", ex, self.site, check=False)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["items"][0]["missing"], ["missing"])
        self.assertEqual(tabs(), before)

    def test_11_rm_removes_the_profile_but_keeps_its_data(self):
        self.hubctl("rm", "t1")
        for job in ["agent-chrome", "playwright-hub"]:
            self.assertIsNone(self.pid(self.label(job, "t1")))
            self.assertFalse(os.path.lexists(f"{self.tmp}/LaunchAgents/{self.label(job, 't1')}.plist"))
        self.assertFalse(os.path.exists(f"{self.tmp}/agents/browser-t1.md"))
        self.assertTrue(os.path.isdir(f"{self.tmp}/profiles/t1"))
        self.assertFalse(port_up(PORTS["cdp1"]))

    def test_12_uninstall(self):
        r = subprocess.run([os.path.join(ROOT, "uninstall.sh"), "--remove-agents"], env=self.env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        wait(lambda: not port_up(PORTS["hub"]), 20, "the default hub to stop")
        self.assertFalse(port_up(PORTS["cdp"]))
        self.assertEqual(os.listdir(f"{self.tmp}/LaunchAgents"), [])
        with open(f"{self.tmp}/settings.json") as f:
            self.assertNotIn("ensure-profile.sh", f.read())
        with open(f"{self.tmp}/CLAUDE.md") as f:
            self.assertNotIn("browser-hub", f.read())
        self.assertFalse(os.path.exists(f"{self.tmp}/mcp-registration"))
        self.assertFalse(os.path.exists(f"{self.tmp}/agents/browser-default.md"))
        self.assertTrue(os.path.isdir(f"{self.tmp}/profiles/default"), "profile data is kept")


if __name__ == "__main__":
    unittest.main()
