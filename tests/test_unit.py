"""Unit tests: no launchd, no Chrome, no network. Everything runs inside a temp sandbox.

    python3 -m unittest discover -s tests -v      (or: tests/run.sh)
"""
import glob
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "lib", "claude_config.py")


class Sandbox:
    """A throwaway config whose every path points inside a temp dir."""

    def __init__(self, extra="", base=None):
        self.dir = tempfile.mkdtemp(prefix="bh-unit-", dir=base)
        self.config = os.path.join(self.dir, "config")
        self.state = os.path.join(self.dir, "var")
        self.agents = os.path.join(self.dir, "agents")
        self.la = os.path.join(self.dir, "LaunchAgents")
        with open(self.config, "w") as f:
            f.write(textwrap.dedent(f"""\
                LABEL_PREFIX=test.bhunit
                CHROME=/bin/echo
                NODE_BIN={self.dir}/node/bin
                PROFILES_DIR={self.dir}/profiles
                STATE_DIR={self.state}
                LAUNCH_AGENTS_DIR={self.la}
                AGENTS_DIR={self.agents}
                CLAUDE_MD={self.dir}/CLAUDE.md
                CLAUDE_SETTINGS={self.dir}/settings.json
                DEFAULT_CDP_PORT=29223
                DEFAULT_HUB_PORT=28931
                """) + extra)
        self.env = {**os.environ, "BROWSER_HUB_CONFIG": self.config}

    def zsh(self, script, check=True):
        """Run zsh with lib/common.zsh sourced; returns stdout."""
        r = subprocess.run(["zsh", "-c", f"source {ROOT}/lib/common.zsh\n{script}"],
                           env=self.env, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise AssertionError(f"zsh failed ({r.returncode}): {r.stderr}\n{script}")
        return r.stdout

    def hubctl(self, *args):
        return subprocess.run([os.path.join(ROOT, "hubctl"), *args], env=self.env,
                              capture_output=True, text=True)

    def write_profile(self, name, cdp, hub):
        """Generate a profile's plists the way `hubctl add` does, minus launchd."""
        self.zsh(f"""mkdir -p $PLIST_DIR
            local NEW_CDP={cdp} NEW_HUB={hub}
            put_plist $(chrome_label {name}) "$(chrome_plist {name})"
            put_plist $(hub_label {name}) "$(hub_plist {name})" """)

    def plist(self, label):
        with open(os.path.join(self.state, "launchd", f"{label}.plist"), "rb") as f:
            return plistlib.load(f)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class TestSyntax(unittest.TestCase):
    def test_zsh_scripts_parse(self):
        for f in ["hubctl", "install.sh", "uninstall.sh", "ensure-profile.sh", "lib/common.zsh"]:
            r = subprocess.run(["zsh", "-n", os.path.join(ROOT, f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{f}: {r.stderr}")

    def test_scripts_are_executable(self):
        for f in ["hubctl", "install.sh", "uninstall.sh", "ensure-profile.sh"]:
            self.assertTrue(os.access(os.path.join(ROOT, f), os.X_OK), f)


class TestPlists(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox("CHROME_EXTRA_ARGS=(--foo 'a b')\n")
        self.sb.zsh('put_plist $(chrome_label default) "$(chrome_plist default)"; '
                    'put_plist $(hub_label default) "$(hub_plist default)"; '
                    'put_plist $REAPER_LABEL "$(reaper_plist)"')
        self.sb.write_profile("work", 29230, 28940)

    def tearDown(self):
        self.sb.cleanup()

    def test_chrome_is_lazy_and_keeps_its_keychain_flags(self):
        for label in ["test.bhunit.agent-chrome", "test.bhunit.agent-chrome-work"]:
            p = self.sb.plist(label)
            self.assertIs(p["RunAtLoad"], False)
            self.assertIs(p["KeepAlive"], False)
            args = p["ProgramArguments"]
            self.assertIn("--use-mock-keychain", args)
            self.assertIn("--password-store=basic", args)
            self.assertIn("--disk-cache-size=209715200", args)
            self.assertTrue(any(x.startswith("--disable-features=") and "OptimizationGuideModelExecution" in x
                                and "OnDeviceModelBackgroundDownload" in x
                                for x in args))          # no Gemini Nano download
            self.assertEqual(args[-2:], ["--foo", "a b"])         # extra args, verbatim, last
            # No startup tab: on macOS Chrome stays up with no windows, and a tab would be a stray
            # window of its own (DESIGN §1).
            self.assertIn("--no-startup-window", args)
            self.assertFalse([a for a in args if a.startswith("file://")])

    def test_ports_and_profile_dirs(self):
        d = self.sb.plist("test.bhunit.agent-chrome")["ProgramArguments"]
        w = self.sb.plist("test.bhunit.agent-chrome-work")["ProgramArguments"]
        self.assertIn("--remote-debugging-port=29223", d)
        self.assertIn("--remote-debugging-port=29230", w)
        self.assertIn(f"--user-data-dir={self.sb.dir}/profiles/default", d)
        self.assertIn(f"--user-data-dir={self.sb.dir}/profiles/work", w)

    def test_only_the_default_hub_is_always_on(self):
        d = self.sb.plist("test.bhunit.playwright-hub")
        w = self.sb.plist("test.bhunit.playwright-hub-work")
        self.assertIs(d["RunAtLoad"], True)
        self.assertIs(d["KeepAlive"], True)
        self.assertIs(w["RunAtLoad"], False)
        self.assertIs(w["KeepAlive"], False)
        a = w["ProgramArguments"]
        for flag in ["--shared-browser-context", "--output-dir"]:
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("--port") + 1], "28940")
        self.assertEqual(a[a.index("--timeout-action") + 1], "15000")
        # FOCUS_GUARD (on by default): the hub runs under lib/focus-guard.mjs, which relays CDP to
        # this profile's Chrome and stands in for the endpoint marker.
        self.assertTrue(a[0].endswith("/node"))
        self.assertTrue(a[1].endswith("/lib/focus-guard.mjs"))
        self.assertEqual(a[2:4], ["29230", "--"])
        self.assertTrue(a[4].endswith("/npx"))
        self.assertEqual(a[a.index("--cdp-endpoint") + 1], "GUARD")

    def test_focus_guard_off_talks_to_chrome_directly(self):
        sb = Sandbox("FOCUS_GUARD=0\n")
        try:
            a = sb.zsh("hub_plist default")
        finally:
            sb.cleanup()
        self.assertNotIn("focus-guard", a)
        self.assertIn("<string>http://localhost:29223</string>", a)

    def test_hub_flags_follow_the_pinned_version(self):
        a = self.sb.plist("test.bhunit.playwright-hub")["ProgramArguments"]
        self.assertEqual(a[a.index("--snapshot-mode") + 1], "none")
        self.assertEqual(a[a.index("--file-paths") + 1], "absolute")
        self.assertIn("--no-webmcp", a)

        def args_with(extra):
            # Older playwright-mcp exits on an unknown option, which would take the always-on
            # hub down, so the newer flags must not reach it.
            sb = Sandbox(extra)
            try:
                return sb.zsh("hub_plist default")
            finally:
                sb.cleanup()
        old = args_with("PW_MCP=@playwright/mcp@0.0.78\n")
        for flag in ["--file-paths", "--no-webmcp"]:
            self.assertNotIn(flag, old)
        self.assertIn("--snapshot-mode", old)          # exists in 0.0.78 already
        self.assertNotIn("--snapshot-mode", args_with("PW_MCP=@playwright/mcp@0.0.70\n"))
        for new in ["@playwright/mcp@0.0.100", "@playwright/mcp@latest", "@playwright/mcp",
                    "@playwright/mcp@0.0.86-alpha.1"]:
            self.assertIn("--no-webmcp", args_with(f"PW_MCP={new}\n"), new)
        # Anything it can't place is treated as old: a missing flag beats a hub that won't start.
        for old_or_unknown in ["@playwright/mcp@0.0.84-alpha", "@playwright/mcp@^0.0.78",
                               "@playwright/mcp@next"]:
            self.assertNotIn("--no-webmcp", args_with(f"PW_MCP='{old_or_unknown}'\n"), old_or_unknown)
        full = args_with("SNAPSHOT_MODE=full\n")
        self.assertIn("<string>full</string>", full)
        self.assertNotIn("--snapshot-mode", args_with("SNAPSHOT_MODE=\n"))

    def test_reaper_is_a_timed_one_shot_that_knows_the_config(self):
        r = self.sb.plist("test.bhunit.browser-hub-reaper")
        self.assertEqual(r["StartInterval"], 900)
        self.assertNotIn("KeepAlive", r)
        self.assertEqual(r["ProgramArguments"][-1], "reap")
        self.assertEqual(r["EnvironmentVariables"]["BROWSER_HUB_CONFIG"], self.sb.config)

    def test_ports_read_back_and_discovery(self):
        out = self.sb.zsh("print $(cdp_port work) $(hub_port work) $(cdp_port default) "
                          "$(hub_port default); profiles")
        lines = out.split()
        self.assertEqual(lines[:4], ["29230", "28940", "29223", "28931"])
        self.assertEqual(lines[4:], ["default", "work"])

    def test_next_ports_go_above_the_highest(self):
        self.assertEqual(self.sb.zsh("next_ports").split(), ["29231", "28941"])

    def test_next_ports_never_reuse_a_port_of_the_other_role(self):
        # A stopped profile whose hub port sits just above the highest CDP port: Chrome must not
        # be handed that hub's port (reported: 9223:8931 plus 9000:9224 gave 9224:9225).
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        sb.write_profile("custom", 29000, 29224)
        self.assertEqual(sb.zsh("next_ports").split(), ["29225", "29226"])

    def test_next_ports_are_two_different_ports(self):
        # Skipping busy ports can walk the CDP candidate up onto the hub candidate.
        import socket
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        sb.write_profile("p", 29100, 29226)
        busy = []
        for port in (29224, 29225, 29226):
            s = socket.socket()
            s.bind(("127.0.0.1", port))
            s.listen(1)
            busy.append(s)
        try:
            c, h = sb.zsh("next_ports").split()
        finally:
            for s in busy:
                s.close()
        self.assertEqual((c, h), ("29227", "29228"))

    def test_unchanged_plist_is_not_rewritten(self):
        out = self.sb.zsh('put_plist $(chrome_label default) "$(chrome_plist default)" '
                          '&& print changed || print same')
        self.assertEqual(out.strip(), "same")

    def test_xml_special_characters_are_escaped(self):
        sb = Sandbox('CHROME="/tmp/a&b <c>/Chrome"\n')
        try:
            sb.zsh('put_plist $(chrome_label default) "$(chrome_plist default)"')
            self.assertEqual(sb.plist("test.bhunit.agent-chrome")["ProgramArguments"][0],
                             "/tmp/a&b <c>/Chrome")
        finally:
            sb.cleanup()


class TestNames(unittest.TestCase):
    def test_valid_names(self):
        sb = Sandbox()
        try:
            for name, ok in [("work", True), ("client-acme", True), ("a1", True),
                             ("team-2", False), ("Work", False), ("", False), ("a--b", False),
                             ("../x", False), ("x_y", False), ("-x", False)]:
                out = sb.zsh(f"valid_name '{name}' && print yes || print no").strip()
                self.assertEqual(out, "yes" if ok else "no", name)
        finally:
            sb.cleanup()


class TestHook(unittest.TestCase):
    """ensure-profile.sh against a stub hubctl that records what it was asked to start."""

    def setUp(self):
        self.sb = Sandbox("AGENT_PROFILES=([personal-shopper]=default [browser-shopper]=default)\n")
        self.hub = os.path.join(self.sb.dir, "hub")
        os.makedirs(os.path.join(self.hub, "lib"))
        shutil.copy(os.path.join(ROOT, "ensure-profile.sh"), self.hub)
        shutil.copy(os.path.join(ROOT, "lib", "common.zsh"), os.path.join(self.hub, "lib"))
        shutil.copy(os.path.join(ROOT, "VERSION"), self.hub)
        self.calls = os.path.join(self.sb.dir, "calls")
        with open(os.path.join(self.hub, "hubctl"), "w") as f:
            f.write(f"""#!/bin/zsh
print -r -- "$*" >> {self.calls}
case $2 in
  missing) print -u2 "hubctl: unknown profile '$2'"; exit 3 ;;
  broken)  print "boom"; exit 1 ;;
  cold)    print "$2 up (CDP 1, hub 2)" ;;
  *)       print "$2 already up (hub 2)" ;;
esac
""")
        os.chmod(os.path.join(self.hub, "hubctl"), 0o755)

    def tearDown(self):
        self.sb.cleanup()

    def run_hook(self, payload):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        if os.path.exists(self.calls):
            os.remove(self.calls)
        r = subprocess.run([os.path.join(self.hub, "ensure-profile.sh")], input=raw,
                           env=self.sb.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "the hook must never block a tool call")
        calls = []
        if os.path.exists(self.calls):
            with open(self.calls) as f:
                calls = f.read().split("\n")[:-1]
        return calls, r.stderr

    def agent(self, subagent_type, prompt="do it"):
        return {"tool_name": "Agent", "tool_input": {"prompt": prompt, "subagent_type": subagent_type}}

    def test_main_session_tool_starts_default(self):
        self.assertEqual(self.run_hook({"tool_name": "mcp__playwright__browser_tabs"})[0], ["up default"])

    def test_agents_resolve_to_their_profile(self):
        for sub, want in [("browser-shop", "up shop"), ("browser-shop-2", "up shop"),
                          ("browser-default", "up default"), ("browser-client-acme-3", "up client-acme"),
                          ("personal-shopper", "up default")]:
            self.assertEqual(self.run_hook(self.agent(sub))[0], [want], sub)

    def test_browser_agent_tool_calls_resolve_via_server_name(self):
        for tool, want in [("mcp__browser-shop-3__browser_navigate", ["up shop"]),
                           ("mcp__browser-shopper__browser_click", ["up default"]),
                           ("mcp__github__get_me", [])]:
            self.assertEqual(self.run_hook({"tool_name": tool})[0], want, tool)

    def test_non_browser_calls_do_nothing(self):
        for payload in [self.agent("general-purpose"),
                        {"tool_name": "Agent", "tool_input": {"prompt": "x"}},
                        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
                        "", "not json at all", "{"]:
            self.assertEqual(self.run_hook(payload)[0], [], payload)

    def test_quoted_text_inside_a_prompt_is_not_mistaken_for_the_field(self):
        p = self.agent("general-purpose", prompt='use "subagent_type": "browser-evil" please')
        self.assertEqual(self.run_hook(p)[0], [])

    def test_unknown_profile_is_silent_for_tools_but_reported_for_agents(self):
        calls, err = self.run_hook({"tool_name": "mcp__browser-missing__x"})
        self.assertEqual((calls, err), (["up missing"], ""))
        calls, err = self.run_hook(self.agent("browser-missing"))
        self.assertIn("could not start profile 'missing'", err)

    def test_failures_are_reported_and_cold_starts_logged(self):
        _, err = self.run_hook(self.agent("browser-broken"))
        self.assertIn("could not start profile 'broken'", err)
        self.run_hook(self.agent("browser-cold"))
        self.run_hook(self.agent("browser-warm"))
        log = open(os.path.join(self.sb.state, "log", "ensure-profile.log")).read()
        self.assertIn("profile=cold rc=0", log)
        self.assertIn("profile=broken rc=1", log)
        self.assertNotIn("profile=warm", log)


class TestSettingsHooks(unittest.TestCase):
    CMD = "/opt/bh/ensure-profile.sh"
    TM = "mcp__playwright__.*|mcp__browser-.*"
    DEF = "/home/u/.config/browser-hub/config"          # the default config: no prefix on the hook
    SB = "/tmp/sb/config"                               # a second install (a sandbox)
    SB_CMD = f"BROWSER_HUB_CONFIG={SB} /tmp/sb/repo/ensure-profile.sh"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bh-settings-")
        self.path = os.path.join(self.dir, "settings.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def py(self, *args, config=DEF):
        return subprocess.run(["python3", PY, *args, config, self.DEF], capture_output=True, text=True)

    def install(self, cmd=CMD, config=DEF, *flags):
        return self.py("hooks-install", self.path, cmd, self.TM, *flags, config=config)

    def check(self, cmd=CMD, config=DEF):
        return self.py("hooks-check", self.path, cmd, self.TM, config=config)

    def write(self, data):
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2)

    def read(self):
        with open(self.path) as f:
            return json.load(f)

    def backups(self):
        return [f for f in os.listdir(self.dir) if ".bak-browser-hub-" in f]

    def test_install_preserves_everything_else_and_is_idempotent(self):
        other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/x/guard.py", "timeout": 5}]}
        self.write({"model": "x", "hooks": {"PreToolUse": [other], "Stop": [{"hooks": []}]}})
        self.install()
        d = self.read()
        self.assertEqual(d["model"], "x")
        self.assertIn("Stop", d["hooks"])
        pre = d["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], other)
        self.assertEqual([e["matcher"] for e in pre[1:]], ["Agent", self.TM])
        self.assertEqual(self.check().returncode, 0)
        n = len(self.backups())
        self.install()
        self.assertEqual(self.read(), d)
        self.assertEqual(len(self.backups()), n, "no-op installs must not write or back up")

    def test_upgrades_an_old_install_in_place(self):
        old = "/old/place/ensure-profile.sh"        # same install (default config), moved repo
        guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/g.py"}]}
        self.write({"hooks": {"PreToolUse": [
            {"matcher": "Agent", "hooks": [{"type": "command", "command": old}]},
            {"matcher": "mcp__playwright__.*", "hooks": [{"type": "command", "command": old}]},
            guard]}})
        self.install()
        pre = self.read()["hooks"]["PreToolUse"]
        self.assertEqual([e["matcher"] for e in pre], ["Agent", self.TM, "Bash"])
        self.assertEqual({e["hooks"][0]["command"] for e in pre[:2]}, {self.CMD})

    def test_another_install_is_never_touched(self):
        self.install()
        live = self.read()
        r = self.install(self.SB_CMD, self.SB)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(self.read(), live, "a sandbox install must not take over the live hooks")
        self.py("hooks-remove", self.path, config=self.SB)
        self.assertEqual(self.read(), live, "a sandbox uninstall must not remove the live hooks")
        self.assertNotEqual(self.check(self.SB_CMD, self.SB).returncode, 0)
        # ...unless explicitly told the other install is gone
        self.install(self.SB_CMD, self.SB, "--force")
        self.assertEqual(self.check(self.SB_CMD, self.SB).returncode, 0)

    def test_two_installs_side_by_side_are_reported(self):
        self.install()
        d = self.read()
        d["hooks"]["PreToolUse"].append({"matcher": "Agent", "hooks": [{"type": "command", "command": self.SB_CMD}]})
        self.write(d)
        r = self.check()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("another install", r.stdout)

    def test_check_fails_when_missing_or_wrong(self):
        self.write({})
        self.assertNotEqual(self.check().returncode, 0)
        self.write({"hooks": {"PreToolUse": [
            {"matcher": "Agent", "hooks": [{"type": "command", "command": "/elsewhere/ensure-profile.sh"}]}]}})
        self.assertNotEqual(self.check().returncode, 0)

    def test_remove_takes_only_ours(self):
        guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/g.py"}]}
        self.write({"hooks": {"PreToolUse": [guard]}})
        self.install()
        self.py("hooks-remove", self.path)
        self.assertEqual(self.read(), {"hooks": {"PreToolUse": [guard]}})
        self.write({})
        self.install()
        self.py("hooks-remove", self.path)
        self.assertEqual(self.read(), {})

    def test_quoted_command_with_spaces_is_still_ours(self):
        cmd = "/Users/x/Code\\ Projects/bh/ensure-profile.sh"
        self.install(cmd)
        self.assertEqual(self.check(cmd).returncode, 0)
        self.py("hooks-remove", self.path)
        self.assertNotIn("ensure-profile", json.dumps(self.read()))

    def test_symlinked_settings_stay_a_symlink(self):
        real = os.path.join(self.dir, "dotfiles-settings.json")
        with open(real, "w") as f:
            f.write("{}")
        os.symlink(real, self.path)
        self.install()
        self.assertTrue(os.path.islink(self.path))
        with open(real) as f:
            self.assertIn("ensure-profile.sh", f.read())

    def test_missing_settings_file_is_created(self):
        self.install()
        self.assertEqual(self.check().returncode, 0)


class TestClaudeMd(unittest.TestCase):
    DEF = "/home/u/.config/browser-hub/config"
    SB = "/tmp/sb/config"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bh-md-")
        self.md = os.path.join(self.dir, "CLAUDE.md")
        self.block = os.path.join(self.dir, "block")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def set_block(self, body, config=DEF):
        with open(self.block, "w") as f:
            f.write(f"<!-- browser-hub:begin config={config} — x -->\n{body}\n<!-- browser-hub:end -->\n")

    def py(self, *args, config=DEF):
        return subprocess.run(["python3", PY, *args, config, self.DEF], capture_output=True, text=True)

    def text(self):
        with open(self.md) as f:
            return f.read()

    def test_append_then_replace_then_remove(self):
        with open(self.md, "w") as f:
            f.write("# Mine\n\nkeep me\n")
        self.set_block("v1")
        self.py("md-install", self.md, self.block)
        self.assertEqual(self.py("md-check", self.md).stdout.strip(), "managed")
        self.set_block("v2")
        self.py("md-install", self.md, self.block)
        text = self.text()
        self.assertIn("v2", text)
        self.assertNotIn("v1", text)
        self.assertEqual(text.count("browser-hub:begin"), 1)
        self.assertTrue(text.startswith("# Mine\n\nkeep me\n"))
        self.py("md-remove", self.md)
        self.assertEqual(self.text(), "# Mine\n\nkeep me\n")

    def test_removal_keeps_surrounding_paragraphs_apart(self):
        with open(self.md, "w") as f:
            f.write("Intro paragraph.\n\n<!-- browser-hub:begin config=/home/u/.config/browser-hub/config -->\n"
                    "x\n<!-- browser-hub:end -->\n\nLater paragraph.\n")
        self.py("md-remove", self.md)
        self.assertEqual(self.text(), "Intro paragraph.\n\nLater paragraph.\n")

    def test_another_installs_block_is_left_alone(self):
        self.set_block("live")
        self.py("md-install", self.md, self.block)
        live = self.text()
        self.set_block("sandbox", config=self.SB)
        r = self.py("md-install", self.md, self.block, config=self.SB)
        self.assertEqual(r.returncode, 3)
        self.py("md-remove", self.md, config=self.SB)
        self.assertEqual(self.text(), live)
        self.assertEqual(self.py("md-check", self.md, config=self.SB).stdout.strip(), "other")

    def test_untagged_block_belongs_to_the_default_install(self):
        with open(self.md, "w") as f:
            f.write("<!-- browser-hub:begin -->\nold\n<!-- browser-hub:end -->\n")
        self.set_block("new")
        self.py("md-install", self.md, self.block)
        self.assertIn("new", self.text())

    def test_hand_written_section_is_left_alone(self):
        with open(self.md, "w") as f:
            f.write("## My browser notes\nrun hubctl up\n")
        self.set_block("v1")
        out = self.py("md-install", self.md, self.block).stdout
        self.assertIn("left alone", out)
        self.assertEqual(self.py("md-check", self.md).stdout.strip(), "custom")

    def test_rules_never_serve_files_beyond_loopback(self):
        # A bare `python3 -m http.server` listens on every interface and serves its whole working
        # directory: the rules that tell agents to serve a folder must bind loopback and name it.
        for name in os.listdir(os.path.join(ROOT, "templates")):
            with open(os.path.join(ROOT, "templates", name)) as f:
                for line in f.read().replace("\n   ", " ").splitlines():
                    if "http.server" in line:
                        self.assertIn("--bind 127.0.0.1", line, f"{name}: {line}")
                        self.assertIn("--directory", line, f"{name}: {line}")

    def test_missing_file(self):
        self.assertEqual(self.py("md-check", self.md).stdout.strip(), "missing")
        self.set_block("v1")
        self.py("md-install", self.md, self.block)
        self.assertTrue(self.text().startswith("<!-- browser-hub:begin"))


class TestAgentsAndLanes(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.sb.write_profile("shop", 29224, 28932)

    def tearDown(self):
        self.sb.cleanup()

    def agent_text(self, name):
        with open(os.path.join(self.sb.agents, f"{name}.md")) as f:
            return f.read()

    def test_agent_from_template(self):
        r = self.sb.hubctl("agent", "shop", "--identity", "the team's supplier accounts")
        self.assertEqual(r.returncode, 0, r.stderr)
        t = self.agent_text("browser-shop")
        self.assertNotIn("{{", t)
        self.assertIn("name: browser-shop\n", t)
        self.assertIn("  - browser-shop:\n      type: http\n      url: http://localhost:28932/mcp", t)
        self.assertIn("`mcp__browser-shop__*`", t)
        self.assertIn("the team's supplier accounts", t)
        # A non-default config must travel with the recovery command, or an agent running it
        # would be looking at a different install's hub.
        self.assertIn(f"BROWSER_HUB_CONFIG={self.sb.config} {ROOT}/hubctl up shop", t)
        # an existing agent is kept unless forced
        self.sb.hubctl("agent", "shop", "--identity", "other")
        self.assertIn("the team's supplier", self.agent_text("browser-shop"))
        self.sb.hubctl("agent", "shop", "--identity", "other", "--force")
        self.assertIn("Signed in as: other", self.agent_text("browser-shop"))

    def test_agent_for_unknown_profile_fails(self):
        self.assertEqual(self.sb.hubctl("agent", "nope").returncode, 3)

    def test_lanes(self):
        self.sb.hubctl("agent", "shop")
        r = self.sb.hubctl("lanes", "shop", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        base, two, three = (self.agent_text(n) for n in
                            ["browser-shop", "browser-shop-2", "browser-shop-3"])
        self.assertIn("PARALLEL LANE 1 of 3", base)
        self.assertIn("name: browser-shop-2\n", two)
        self.assertIn("  - browser-shop-2:\n", two)
        self.assertIn("`mcp__browser-shop-2__*`", two)
        self.assertIn("url: http://localhost:28932/mcp", two)   # same hub, own server name
        self.assertIn("PARALLEL LANE 3 of 3", three)
        self.assertNotIn("browser-shop-3-", three)
        self.assertEqual(three.count("PARALLEL LANE"), 1)
        # shrinking removes the extra lane and re-numbers the notes
        self.sb.hubctl("lanes", "shop", "2")
        self.assertFalse(os.path.exists(os.path.join(self.sb.agents, "browser-shop-3.md")))
        self.assertIn("PARALLEL LANE 2 of 2", self.agent_text("browser-shop-2"))
        self.assertEqual(self.agent_text("browser-shop").count("PARALLEL LANE"), 1)

    def test_list_runs_without_launchd(self):
        self.sb.hubctl("agent", "shop")
        r = self.sb.hubctl("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("shop", r.stdout)
        self.assertIn("browser-shop", r.stdout)

    def test_bad_commands(self):
        self.assertEqual(self.sb.hubctl("frobnicate").returncode, 2)
        self.assertNotEqual(self.sb.hubctl("add", "Team-2").returncode, 0)
        self.assertNotEqual(self.sb.hubctl("add", "default").returncode, 0)
        self.assertNotEqual(self.sb.hubctl("rm", "default").returncode, 0)


class TestReviewRegressions(unittest.TestCase):
    """One test per defect found in review, so none of them comes back."""

    def setUp(self):
        self.sb = Sandbox()
        self.sb.write_profile("shop", 29224, 28932)

    def tearDown(self):
        self.sb.cleanup()

    def agent_file(self, name):
        with open(os.path.join(self.sb.agents, f"{name}.md")) as f:
            return f.read()

    @unittest.skipUnless(shutil.which("ruby"), "needs ruby to parse YAML")
    def test_identity_with_yaml_syntax_still_gives_valid_frontmatter(self):
        ident = 'Acme: admin "root" #2 \\ ops'
        self.assertEqual(self.sb.hubctl("agent", "shop", "--identity", ident).returncode, 0)
        front = self.agent_file("browser-shop").split("---")[1]
        r = subprocess.run(["ruby", "-ryaml", "-e", "puts YAML.load(STDIN.read)['description']"],
                           input=front, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(ident, r.stdout)

    def test_identity_must_be_one_line(self):
        self.assertNotEqual(self.sb.hubctl("agent", "shop", "--identity", "a\nb").returncode, 0)

    def test_a_flag_missing_its_value_fails_instead_of_hanging(self):
        for args in [("agent", "shop", "--identity"), ("add", "x", "--ports"), ("add", "x", "--identity")]:
            r = subprocess.run([os.path.join(ROOT, "hubctl"), *args], env=self.sb.env,
                               capture_output=True, text=True, timeout=10)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertLess(len(r.stderr), 500, args)

    def test_lanes_keep_quotes_and_leave_paths_alone(self):
        # A profile named "hub": its agent is browser-hub, a string that also appears in the
        # repo path the agent file mentions.
        self.sb.write_profile("hub", 29225, 28933)
        self.sb.hubctl("agent", "hub", "--identity", "x")
        self.sb.hubctl("lanes", "hub", "2")
        two = self.agent_file("browser-hub-2")
        self.assertIn("name: browser-hub-2\n", two)
        self.assertIn("mcp__browser-hub-2__", two)
        self.assertIn(f"{ROOT}/hubctl up hub", two)
        self.assertNotIn("browser-hub-2/", two, "a path containing the name must not be rewritten")
        desc = [ln for ln in two.splitlines() if ln.startswith("description: ")][0]
        self.assertTrue(desc.startswith('description: "') and desc.endswith('lane."'), desc)

    def test_ports_already_taken_are_refused(self):
        for ports in ["29224:28999", "29999:28931", "29300:29300"]:
            r = self.sb.hubctl("add", "work", "--ports", ports)
            self.assertNotEqual(r.returncode, 0, ports)
        self.assertFalse(os.path.exists(os.path.join(self.sb.state, "launchd",
                                                     "test.bhunit.agent-chrome-work.plist")))

    def test_incomplete_profile_is_explained_not_crashed(self):
        os.remove(os.path.join(self.sb.state, "launchd", "test.bhunit.playwright-hub-shop.plist"))
        for args in [("up", "shop"), ("agent", "shop")]:
            r = self.sb.hubctl(*args)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("incomplete", r.stderr)
        self.assertEqual(self.sb.hubctl("rm", "shop").returncode, 0)

    def test_rm_leaves_a_same_named_agent_it_did_not_write(self):
        os.makedirs(self.sb.agents, exist_ok=True)
        mine = os.path.join(self.sb.agents, "browser-shop.md")
        with open(mine, "w") as f:
            f.write("---\nname: browser-shop\n---\nsomething else entirely\n")
        self.assertEqual(self.sb.hubctl("rm", "shop").returncode, 0)
        self.assertTrue(os.path.exists(mine))

    def test_mapped_agent_whose_name_ends_in_digits(self):
        t = TestHook("test_main_session_tool_starts_default")   # borrow its stub-hubctl wiring
        t.setUp()
        try:
            with open(t.sb.config, "a") as f:
                f.write("AGENT_PROFILES=([research-2]=shop)\n")
            self.assertEqual(t.run_hook(t.agent("research-2"))[0], ["up shop"])
        finally:
            t.tearDown()


class TestInstallDryRun(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        real = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        node = os.path.dirname(shutil.which("npx") or "/opt/homebrew/bin/npx")
        sb = Sandbox(f'CHROME="{real}"\nNODE_BIN={node}\n')
        try:
            r = subprocess.run([os.path.join(ROOT, "install.sh"), "--dry-run"], env=sb.env,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("nothing changed", r.stdout)
            for p in ["var", "settings.json", "CLAUDE.md", "agents", "LaunchAgents"]:
                self.assertFalse(os.path.exists(os.path.join(sb.dir, p)), p)
        finally:
            sb.cleanup()

    def test_dry_run_does_not_write_a_first_config(self):
        sb = Sandbox()
        try:
            env = {**sb.env, "BROWSER_HUB_CONFIG": os.path.join(sb.dir, "new", "config")}
            subprocess.run([os.path.join(ROOT, "install.sh"), "--dry-run"], env=env,
                           capture_output=True, text=True)
            self.assertFalse(os.path.exists(os.path.join(sb.dir, "new")))
        finally:
            sb.cleanup()


class TestNativeHosts(unittest.TestCase):
    """Chrome finds native-messaging manifests in the profile's user-data-dir, so hubctl links them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bh-nmh-")
        self.src = os.path.join(self.tmp, "stock dir")  # a space, like the real macOS path
        os.makedirs(self.src)
        self.manifest = os.path.join(self.src, "com.1password.1password.json")
        self.sb = Sandbox(f'NATIVE_HOSTS_SRC="{self.src}"\n')
        self.dest = os.path.join(self.sb.dir, "profiles", "shop", "NativeMessagingHosts",
                                 "com.1password.1password.json")

    def tearDown(self):
        self.sb.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def link(self):
        return self.sb.zsh("link_native_hosts shop")

    def install_manifest(self, text="{}"):
        with open(self.manifest, "w") as f:
            f.write(text)

    def test_links_the_manifest_and_is_idempotent(self):
        self.install_manifest()
        self.assertIn("linked", self.link())
        self.assertEqual(os.readlink(self.dest), self.manifest)
        self.assertEqual(self.link(), "")

    def test_follows_a_manifest_rewritten_by_the_app(self):
        self.install_manifest()
        self.link()
        self.install_manifest('{"v": 2}')
        self.assertIn("v", open(self.dest).read())

    def test_repairs_a_dangling_link(self):
        self.install_manifest()
        os.makedirs(os.path.dirname(self.dest))
        os.symlink("/nonexistent", self.dest)
        self.assertIn("linked", self.link())
        self.assertEqual(os.readlink(self.dest), self.manifest)

    def test_never_replaces_a_real_file(self):
        self.install_manifest()
        os.makedirs(os.path.dirname(self.dest))
        with open(self.dest, "w") as f:
            f.write("mine")
        self.link()
        self.assertFalse(os.path.islink(self.dest))
        self.assertEqual(open(self.dest).read(), "mine")

    def test_skips_a_host_that_is_not_installed(self):
        self.assertEqual(self.link(), "")
        self.assertFalse(os.path.exists(os.path.dirname(self.dest)))

    def test_regen_links_every_profile(self):
        self.install_manifest()
        self.sb.write_profile("shop", 29224, 28932)
        try:
            self.sb.hubctl("regen")
            self.assertTrue(os.path.islink(self.dest))
        finally:
            # regen loads the sandbox's jobs into the real launchd; don't leave them there.
            self.sb.zsh("for n in $(profiles); do lc_bootout $(chrome_label $n); lc_bootout $(hub_label $n); done; "
                        "lc_bootout $REAPER_LABEL")


if __name__ == "__main__":
    unittest.main()


class TestProfilePermissions(unittest.TestCase):
    """A profile directory holds every login in it (cookies under a public key, plus the plaintext
    save), so only its owner may read it: created 0700 whatever the umask, tightened if looser,
    and `doctor` says so when it isn't."""

    def setUp(self):
        self.sb = Sandbox()
        # Cleanups run after tearDown and last-in first-out: this one, registered first, runs
        # after the tests' own (which lift chflags and modes so the tree can be removed).
        self.addCleanup(self.sb.cleanup)
        self.dir = os.path.join(self.sb.dir, "profiles", "shop")

    @staticmethod
    def mode(path):
        return oct(os.stat(path).st_mode & 0o777)

    def test_created_private_under_a_permissive_umask(self):
        self.sb.zsh("umask 022; secure_profile_dir shop")
        self.assertEqual(self.mode(self.dir), "0o700")
        self.assertEqual(self.mode(os.path.dirname(self.dir)), "0o700", "parents it creates are private too")

    def test_loose_directory_and_cookie_saves_are_tightened_and_reported(self):
        os.makedirs(self.dir)
        os.chmod(self.dir, 0o755)
        for name in ("browser-hub-cookies.json", "browser-hub-cookies.json.prev"):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write("{}")
            os.chmod(os.path.join(self.dir, name), 0o644)
        problems = self.sb.zsh("profile_perms shop").splitlines()
        self.assertEqual(len(problems), 3, problems)
        self.assertIn("not 700", problems[0])
        self.sb.zsh("umask 022; secure_profile_dir shop")
        self.assertEqual(self.mode(self.dir), "0o700")
        self.assertEqual(self.mode(os.path.join(self.dir, "browser-hub-cookies.json")), "0o600")
        self.assertEqual(self.mode(os.path.join(self.dir, "browser-hub-cookies.json.prev")), "0o600")
        self.assertEqual(self.sb.zsh("profile_perms shop"), "")

    def test_doctor_reports_a_readable_profile(self):
        self.sb.write_profile("shop", 29224, 28932)
        os.makedirs(self.dir)
        os.chmod(self.dir, 0o755)
        r = self.sb.hubctl("doctor", "--no-mcp", "--no-hooks", "--no-claude-md")
        self.assertIn(f"shop: {self.dir} is mode 755, not 700 (run: hubctl regen)", r.stdout + r.stderr)

    def test_every_path_that_starts_or_fills_a_profile_secures_it_first(self):
        # up (before Chrome starts), add, regen and pull all go through secure_profile_dir, and
        # each stops (die, or skip that profile) when it fails: the line also handles `||`.
        with open(os.path.join(ROOT, "hubctl")) as f:
            src = f.read()
        calls = [ln for ln in src.splitlines() if "secure_profile_dir" in ln and not ln.lstrip().startswith("#")]
        self.assertGreaterEqual(len(calls), 4)
        for ln in calls:
            self.assertIn("||", ln, ln)
        self.assertNotIn("mkdir -p $(profile_dir $n)", src)

    def immutable(self, path):
        # chflags uchg: even the owner's chmod fails, a real failure with no stubs involved.
        subprocess.run(["chflags", "uchg", path], check=True)
        self.addCleanup(subprocess.run, ["chflags", "-R", "nouchg", self.sb.dir])

    def test_a_chmod_that_fails_is_an_error_not_a_pass(self):
        os.makedirs(self.dir)
        os.chmod(self.dir, 0o755)
        self.immutable(self.dir)
        r = subprocess.run(["zsh", "-c", f"source {ROOT}/lib/common.zsh\nsecure_profile_dir shop"],
                           env=self.sb.env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not private", r.stderr)

    def test_a_cookie_save_that_stays_readable_is_an_error(self):
        os.makedirs(self.dir, mode=0o700)
        f = os.path.join(self.dir, "browser-hub-cookies.json")
        with open(f, "w") as fh:
            fh.write("{}")
        os.chmod(f, 0o644)
        self.immutable(f)
        r = subprocess.run(["zsh", "-c", f"source {ROOT}/lib/common.zsh\nsecure_profile_dir shop"],
                           env=self.sb.env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f, r.stderr)

    def test_a_directory_that_cannot_be_created_is_an_error(self):
        parent = os.path.dirname(self.dir)
        os.makedirs(parent)
        os.chmod(parent, 0o500)
        self.addCleanup(lambda: os.path.exists(parent) and os.chmod(parent, 0o700))
        r = subprocess.run(["zsh", "-c", f"source {ROOT}/lib/common.zsh\nsecure_profile_dir shop"],
                           env=self.sb.env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.dir))


class TestRun(unittest.TestCase):
    """`hubctl run`: argument handling, and the shape every extractor in the library must have."""

    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def test_usage_and_unknown_extractor(self):
        self.sb.write_profile("shop", 29224, 28932)
        r = self.sb.hubctl("run", "shop")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("usage", r.stderr)
        r = self.sb.hubctl("run", "shop", "no-such-site")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("amazon", r.stderr)          # it lists what exists
        self.assertNotIn("_lib", r.stderr)         # but not the helpers

    def test_unknown_profile(self):
        r = self.sb.hubctl("run", "nope", "amazon", "B000000000")
        self.assertNotEqual(r.returncode, 0)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_every_extractor_exports_the_contract(self):
        script = textwrap.dedent("""
            import { readdirSync } from 'node:fs';
            const dir = process.argv[1], bad = [];
            for (const f of readdirSync(dir).filter(f => f.endsWith('.js') && !f.startsWith('_'))) {
              const m = await import(dir + '/' + f);
              if (typeof m.lookup !== 'function' || typeof m.url !== 'function' || !m.name) bad.push(f);
              else if (!/^https:\\/\\//.test(m.url('X1'))) bad.push(f + ': url()');
            }
            console.log(JSON.stringify(bad));
            """)
        r = subprocess.run(["node", "--input-type=module", "-e", script, os.path.join(ROOT, "extractors")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [])

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_extractors_load_as_modules_without_syntax_detection(self):
        # Node 18 (the README's minimum) treats a .js file as CommonJS unless a package.json says
        # "type": "module"; newer Node guesses from the syntax. Turn the guessing off to check.
        r = subprocess.run(["node", "--help"], capture_output=True, text=True)
        flag = ["--no-experimental-detect-module"] if "detect-module" in r.stdout else []
        for path in sorted(glob.glob(os.path.join(ROOT, "extractors", "*.js"))):
            script = f"await import({json.dumps('file://' + path)}); console.log('ok')"
            r = subprocess.run(["node", *flag, "--input-type=module", "-e", script],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, f"{os.path.basename(path)}: {r.stderr[-400:]}")

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_walls_are_blocked_not_partial(self):
        script = textwrap.dedent("""
            const { wallFrom } = await import(process.argv[1] + '/_lib.js');
            const nav = 'All Departments\\\\nAutomotive Parts & Accessories\\\\n'.repeat(30);
            const cases = {
              amazonCheckpoint: wallFrom({ url: 'https://www.amazon.com/checkpoint/continue?token=x', title: 'Amazon.com',
                body: nav + 'We\\u2019re noticing unusual browser activity\\\\nCheck the box to confirm you\\u2019re human.' }),
              checkpointTextOnly: wallFrom({ url: 'https://www.amazon.com/dp/B0D6NN6TM7', title: 'Amazon.com', content: false,
                body: nav + 'Check the box to confirm you\\u2019re human.' }),
              turnstileInsidePage: wallFrom({ url: 'https://www.microcenter.com/product/1/x', title: 'PSU - Micro Center', weakMarker: true }),
              turnstileAlone: wallFrom({ url: 'https://www.microcenter.com/product/1/x', title: 'Micro Center', weakMarker: true, content: false }),
              domMarker: wallFrom({ url: 'https://www.amazon.com/dp/B0D6NN6TM7', title: 'Amazon.com', marker: true }),
              // Product copy that mentions these words is not a wall while the product is on the page.
              powerButton: wallFrom({ url: 'https://www.amazon.com/dp/B0X', title: 'Amazon.com: Keyboard',
                body: 'Press and hold the power button for 3 seconds. Access denied error fix. captcha-free.' }),
              yubikey: wallFrom({ url: 'https://www.newegg.com/p/x', title: 'Two-factor security verification key - Newegg.com', body: '' }),
              cloudflare: wallFrom({ url: 'https://www.newegg.com/p/x', title: 'Just a moment...', body: '' }),
              portalLogin: wallFrom({ url: 'https://portal.example.com/partneraccess/Auth/Login.aspx?ReturnUrl=x' }, { loginUrl: /login/i }),
              product: wallFrom({ url: 'https://www.amazon.com/dp/B0D6NN6TM7', title: 'Amazon.com: AMD Ryzen 5', body: 'Add to Cart' }),
            };
            console.log(JSON.stringify(Object.fromEntries(Object.entries(cases).map(([k, v]) => [k, v && v.reason]))));
            """)
        r = subprocess.run(["node", "--input-type=module", "-e", script, os.path.join(ROOT, "extractors")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {
            "amazonCheckpoint": "captcha", "checkpointTextOnly": "captcha", "domMarker": "captcha",
            "powerButton": None, "yubikey": None,
            "turnstileInsidePage": None, "turnstileAlone": "captcha", "cloudflare": "captcha", "portalLogin": "login",
            "product": None})


def pw_modules():
    """A node_modules with playwright-core in it, from the npx cache, or None."""
    for d in sorted(glob.glob(os.path.expanduser("~/.npm/_npx/*/node_modules")), reverse=True):
        if os.path.isdir(os.path.join(d, "playwright-core")):
            return d
    return None


@unittest.skipUnless(shutil.which("node") and pw_modules(),
                     "needs node and playwright-core in the npx cache: run ./install.sh first, or "
                     "fetch it with `npx -y <PW_MCP from config.example> --version` (tests/run.sh says so)")
class TestFocusGuard(unittest.TestCase):
    """lib/focus-guard.mjs against a fake Chrome: what it rewrites, what it swallows, what it passes."""

    def test_relay(self):
        nm = pw_modules()
        tmp = tempfile.mkdtemp(prefix="bh-guard-")
        self.addCleanup(shutil.rmtree, tmp, True)
        chrome = os.path.join(tmp, "chrome.mjs")
        hub = os.path.join(tmp, "hub.mjs")
        with open(chrome, "w") as f:
            f.write(textwrap.dedent(f"""
                import {{ createRequire }} from 'node:module'; import http from 'node:http'; import fs from 'node:fs';
                const {{ wsServer }} = createRequire('{nm}/')('playwright-core/lib/utilsBundle');
                const seen = [];
                const srv = http.createServer((req, res) => res.end(JSON.stringify({{
                  webSocketDebuggerUrl: `ws://localhost:${{srv.address().port}}/devtools/browser/x` }})));
                const wss = new wsServer({{ server: srv }});
                wss.on('connection', ws => ws.on('message', (d, bin) => {{
                  const m = JSON.parse(d.toString()); seen.push({{ ...m, bin }});
                  fs.writeFileSync('{tmp}/seen.json', JSON.stringify(seen));
                  // The guard asks whether a tab is hidden before letting bringToFront through.
                  const result = m.params?.expression === 'document.visibilityState'
                    ? {{ result: {{ value: m.sessionId === 'HIDDEN' ? 'hidden' : 'visible' }} }} : {{ from: 'chrome' }};
                  const reply = JSON.stringify({{ id: m.id, result, ...(m.sessionId ? {{ sessionId: m.sessionId }} : {{}}) }});
                  // SLOW: a page whose dialog holds Runtime.evaluate past the guard's 2 s fallback.
                  if (m.sessionId === 'SLOW' && m.params?.expression) setTimeout(() => ws.send(reply), 3000);
                  else ws.send(reply);
                }}));
                srv.listen(0, () => fs.writeFileSync('{tmp}/port', String(srv.address().port)));
                """))
        with open(hub, "w") as f:
            f.write(textwrap.dedent(f"""
                import {{ createRequire }} from 'node:module';
                const {{ ws: WebSocket }} = createRequire('{nm}/')('playwright-core/lib/utilsBundle');
                const v = await (await fetch(process.argv[2] + '/json/version')).json();
                const ws = new WebSocket(v.webSocketDebuggerUrl);
                const replies = {{}};
                ws.on('message', d => {{ const m = JSON.parse(d); replies[m.id] = m; if (Object.keys(replies).length === 6) setTimeout(done, 1500); }});
                ws.on('open', () => {{
                  ws.send(JSON.stringify({{ id: 1, method: 'Target.createTarget', params: {{ url: 'about:blank' }} }}));
                  ws.send(JSON.stringify({{ id: 2, method: 'Page.bringToFront', sessionId: 'S1' }}));
                  ws.send(JSON.stringify({{ id: 3, method: 'Target.activateTarget', params: {{ targetId: 'T' }} }}));
                  ws.send(JSON.stringify({{ id: 4, method: 'Runtime.evaluate', params: {{ expression: '1' }} }}));
                  ws.send(JSON.stringify({{ id: 5, method: 'Page.bringToFront', sessionId: 'HIDDEN' }}));
                  ws.send(JSON.stringify({{ id: 6, method: 'Page.bringToFront', sessionId: 'SLOW' }}));
                }});
                function done() {{ console.log(JSON.stringify({{ url: v.webSocketDebuggerUrl, replies }})); ws.close(); process.exit(7); }}
                """))
        fake = subprocess.Popen(["node", chrome])
        self.addCleanup(fake.kill)
        for _ in range(100):
            if os.path.exists(os.path.join(tmp, "port")):
                break
            time.sleep(0.05)
        port = open(os.path.join(tmp, "port")).read()
        r = subprocess.run(["node", os.path.join(ROOT, "lib", "focus-guard.mjs"), port, "--", "node", hub, "GUARD"],
                           env={**os.environ, "BH_PW_MODULES": nm}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 7, "the guard exits with its hub's code\n" + r.stderr)
        out = json.loads(r.stdout)
        self.assertNotIn(f":{port}/", out["url"], "the browser URL points back at the guard")
        rep = out["replies"]
        self.assertEqual(rep["1"]["result"], {"from": "chrome"})
        self.assertEqual(rep["2"], {"id": 2, "result": {}, "sessionId": "S1"})   # answered by the guard
        self.assertEqual(rep["3"], {"id": 3, "result": {}})
        self.assertEqual(rep["4"]["result"], {"from": "chrome"})
        # A hidden tab has to come forward to render at all: that bringToFront reaches Chrome.
        self.assertEqual(rep["5"], {"id": 5, "result": {"from": "chrome"}, "sessionId": "HIDDEN"})
        # No answer in 2 s: answered as visible, and Chrome's late reply never reaches the hub.
        self.assertEqual(rep["6"], {"id": 6, "result": {}, "sessionId": "SLOW"})
        self.assertFalse([k for k in rep if int(k) >= 10**9], "the guard's own replies leaked to the hub")
        seen = json.load(open(os.path.join(tmp, "seen.json")))
        self.assertEqual(sorted(m["method"] for m in seen if m["id"] < 10**9),
                         ["Page.bringToFront", "Runtime.evaluate", "Target.createTarget"])
        self.assertEqual([m["sessionId"] for m in seen if m["method"] == "Page.bringToFront"], ["HIDDEN"])
        self.assertEqual(sorted(m["sessionId"] for m in seen if m["id"] >= 10**9), ["HIDDEN", "S1", "SLOW"])
        self.assertEqual(seen[0]["params"], {"url": "about:blank", "newWindow": True, "background": True})
        self.assertFalse([m for m in seen if m["bin"]], "text frames stay text (Chrome drops binary)")

    def test_refuses_web_pages_and_foreign_hosts(self):
        # Chrome's own DevTools server refuses a Host that isn't localhost or an IP (DNS rebinding)
        # and a WebSocket handshake that carries an Origin (Chrome 111+). The guard must not be the
        # way around either: only a local, non-browser client (the hub) gets through.
        nm = pw_modules()
        tmp = tempfile.mkdtemp(prefix="bh-guard-")
        self.addCleanup(shutil.rmtree, tmp, True)
        chrome = os.path.join(tmp, "chrome.mjs")
        hub = os.path.join(tmp, "hub.mjs")
        with open(chrome, "w") as f:
            f.write(textwrap.dedent(f"""
                import {{ createRequire }} from 'node:module'; import http from 'node:http'; import fs from 'node:fs';
                const {{ wsServer }} = createRequire('{nm}/')('playwright-core/lib/utilsBundle');
                const seen = {{ http: [], ws: [] }};
                const save = () => fs.writeFileSync('{tmp}/seen.json', JSON.stringify(seen));
                const srv = http.createServer((req, res) => {{ seen.http.push(req.url); save(); res.end(JSON.stringify({{
                  webSocketDebuggerUrl: `ws://127.0.0.1:${{srv.address().port}}/devtools/browser/x` }})); }});
                const wss = new wsServer({{ server: srv }});
                wss.on('connection', (ws, req) => {{ seen.ws.push(req.url); save(); ws.on('message', d => ws.send(d)); }});
                srv.listen(0, '127.0.0.1', () => fs.writeFileSync('{tmp}/port', String(srv.address().port)));
                """))
        with open(hub, "w") as f:
            f.write(textwrap.dedent(f"""
                import {{ createRequire }} from 'node:module'; import http from 'node:http';
                const {{ ws: WebSocket }} = createRequire('{nm}/')('playwright-core/lib/utilsBundle');
                const base = new URL(process.argv[2]);
                const get = (path, headers = {{}}) => new Promise(ok => {{
                  const r = http.get({{ host: base.hostname, port: base.port, path, headers }}, res => {{ res.resume(); ok(res.statusCode); }});
                  r.on('error', e => ok('error ' + e.message));
                }});
                const ws = (opts = {{}}) => new Promise(ok => {{
                  const s = new WebSocket(`ws://${{base.host}}/devtools/browser/x`, opts);
                  s.on('open', () => {{ s.close(); ok('open'); }});
                  s.on('unexpected-response', (q, res) => ok(res.statusCode));
                  s.on('error', e => ok('error ' + e.message));
                }});
                const out = {{
                  plain: await get('/json/version'),
                  localhost: await get('/json/version', {{ Host: `localhost:${{base.port}}` }}),
                  foreignHost: await get('/json/version', {{ Host: 'evil.example:' + base.port }}),
                  origin: await get('/json/list', {{ Origin: 'http://evil.example' }}),
                  fetchMetadata: await get('/json/new?about:blank', {{ 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'no-cors' }}),
                  wsPlain: await ws(),
                  wsOrigin: await ws({{ origin: 'http://evil.example' }}),
                  wsForeignHost: await ws({{ headers: {{ Host: 'evil.example:' + base.port }} }}),
                }};
                console.log(JSON.stringify(out)); process.exit(7);
                """))
        fake = subprocess.Popen(["node", chrome])
        self.addCleanup(fake.kill)
        for _ in range(100):
            if os.path.exists(os.path.join(tmp, "port")):
                break
            time.sleep(0.05)
        port = open(os.path.join(tmp, "port")).read()
        r = subprocess.run(["node", os.path.join(ROOT, "lib", "focus-guard.mjs"), port, "--", "node", hub, "GUARD"],
                           env={**os.environ, "BH_PW_MODULES": nm}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertEqual(json.loads(r.stdout), {
            "plain": 200, "localhost": 200, "foreignHost": 403, "origin": 403, "fetchMetadata": 403,
            "wsPlain": "open", "wsOrigin": 403, "wsForeignHost": 403})
        seen = json.load(open(os.path.join(tmp, "seen.json")))
        self.assertEqual(seen, {"http": ["/json/version", "/json/version"], "ws": ["/devtools/browser/x"]},
                         "refused requests never reach Chrome")

    def test_fails_open_without_its_library(self):
        # No relay is better than no hub: the hub must start, pointed straight at Chrome.
        r = subprocess.run(["node", os.path.join(ROOT, "lib", "focus-guard.mjs"), "29999", "--",
                            "node", "-e", "console.log(process.argv[1]); process.exit(5)", "GUARD"],
                           env={**os.environ, "BH_PW_MODULES": "/nonexistent"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual(r.stdout.strip(), "http://127.0.0.1:29999")
        self.assertIn("focus not guarded", r.stderr)


def socket_safe_tmp():
    """A temp base short enough for unix sockets under it. macOS caps a socket path at 104 bytes,
    and a tunnel's ControlMaster socket is <sandbox>/var/run/ssh-<profile>.sock (44 bytes plus the
    base, for the profile names used here). A long $TMPDIR (a repo-confined one, say) falls back to
    /tmp; mkdtemp still makes the sandbox 0700."""
    d = tempfile.gettempdir().rstrip("/")
    return d if len(d) <= 56 else "/tmp"


class TestRemote(unittest.TestCase):
    """Remote profiles: a profile whose Chrome runs on another Mac, reached over an ssh tunnel.

    `ssh` is a stub that runs the remote command right here, and the other Mac's hubctl is a stub
    that records its arguments and answers like the real one. Nothing touches launchd: the tunnel
    plist is written the way `add --remote` writes it, and the tunnel's port and ControlMaster
    socket are faked with a listening socket and a bound unix socket."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bh-rem-")
        self.calls = os.path.join(self.tmp, "calls")
        self.ssh = os.path.join(self.tmp, "ssh")
        self.rhub = os.path.join(self.tmp, "rhubctl")
        # The other Mac: a second sandbox with its own label prefix and port range. Its `held` is
        # the real one, so the copy tests exercise its lock and its running-Chrome refusal.
        self.rsb = Sandbox("LABEL_PREFIX=test.bhunitr\nDEFAULT_CDP_PORT=29323\nDEFAULT_HUB_PORT=29031\n")
        self.rsb.write_profile("shop", 29324, 29032)
        self.rprofiles = os.path.join(self.rsb.dir, "profiles")
        with open(self.ssh, "w") as f:
            f.write(textwrap.dedent(f"""\
                #!/bin/zsh
                # stub ssh: run the remote command here. Rejects what real ssh rejects (an option
                # with a newline in it: rsync splits -e on spaces only). "asleep" is unreachable.
                ctl= sock=
                while (( $# )); do
                  [[ $1 == *$'\\n'* ]] && {{ print -u2 "ssh: unsupported option"; exit 255 }}
                  case $1 in
                    -O) ctl=$2; shift 2 ;;
                    -S) sock=$2; shift 2 ;;
                    -[oLlpiF]) [[ $2 == *$'\\n'* ]] && {{ print -u2 "ssh: unsupported option"; exit 255 }}; shift 2 ;;
                    --) shift; break ;;
                    -*) shift ;;
                    *) break ;;
                  esac
                done
                dest=$1; shift
                [[ $ctl == check ]] && {{ [[ -S $sock ]]; exit $(( $? ? 255 : 0 )) }}
                print -r -- "ssh $dest" >> {self.calls}
                [[ $dest == asleep ]] && {{ print -u2 "ssh: connect to host asleep: Operation timed out"; exit 255 }}
                exec zsh -c "$*"
                """))
        with open(self.rhub, "w") as f:
            f.write(textwrap.dedent(f"""\
                #!/bin/zsh
                print -r -- "remote $*" >> {self.calls}
                case $1 in
                  info)
                    [[ -n ${{R_MISSING:-}} ]] && {{ print -u2 "hubctl: unknown profile '$2'"; exit 3 }}
                    print version=1.1.0; print profile=$2
                    [[ -n ${{R_REMOTE:-}} ]] && print remote=elsewhere:$2
                    print cdp=39324; print hub=${{R_HUB:-39032}}
                    print dir={self.rprofiles}/$2; print chrome=${{R_CHROME:-down}} ;;
                  up)
                    [[ -n ${{R_MISSING:-}} ]] && {{ print -u2 "hubctl: unknown profile '$2'"; exit 3 }}
                    print "$2 already up (hub 39032)" ;;
                  run)  print '{{"ok":true}}'; exit 3 ;;
                  held) shift; BROWSER_HUB_CONFIG={self.rsb.config} exec {ROOT}/hubctl held "$@" ;;
                  *)    print "$1 $2 done" ;;
                esac
                """))
        for f in (self.ssh, self.rhub):
            os.chmod(f, 0o755)
        self.sb = Sandbox(f"SSH_BIN={self.ssh}\n", base=socket_safe_tmp())
        self.sb.write_profile("shop", 29224, 28932)
        self.sb.hubctl("agent", "shop", "--identity", "the supplier accounts")

    def tearDown(self):
        for s in getattr(self, "socks", []):
            s.close()
        self.sb.cleanup()
        self.rsb.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hubctl(self, *args, **env):
        if os.path.exists(self.calls):
            os.remove(self.calls)
        r = subprocess.run([os.path.join(ROOT, "hubctl"), *args], env={**self.sb.env, **env},
                           capture_output=True, text=True, timeout=60)
        return r

    def log(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return f.read().splitlines()

    @staticmethod
    def text(path):
        with open(path) as f:
            return f.read()

    def write_remote(self, name="fake-shop", dest="fake", rp="shop", port=39032):
        self.sb.zsh(f"""mkdir -p $PLIST_DIR
            local NEW_HUB={port} NEW_REMOTE_SSH={dest} NEW_REMOTE_PROFILE={rp} NEW_REMOTE_HUBCTL={self.rhub}
            put_plist $(tunnel_label {name}) "$(tunnel_plist {name})" """)

    def fake_tunnel(self, name="fake-shop"):
        """A listener on the tunnel's port and a live ControlMaster socket: tunnel_ok is true."""
        import socket
        lsn = socket.socket()
        lsn.bind(("127.0.0.1", 0))
        lsn.listen(8)
        port = lsn.getsockname()[1]
        self.write_remote(name, port=port)
        sock_path = self.sb.zsh(f"print -r -- $(ssh_sock {name})").strip()
        os.makedirs(os.path.dirname(sock_path), exist_ok=True)
        us = socket.socket(socket.AF_UNIX)
        us.bind(sock_path)
        self.socks = [lsn, us]
        return port

    def test_tunnel_plist_is_the_record(self):
        self.write_remote()
        p = self.sb.plist("test.bhunit.browser-tunnel-fake-shop")
        self.assertIs(p["RunAtLoad"], False)
        self.assertIs(p["KeepAlive"], False)
        a = p["ProgramArguments"]
        self.assertEqual(a[0], self.ssh)
        for x in ["-N", "-M", "ExitOnForwardFailure=yes", "BatchMode=yes", "ControlPersist=no"]:
            self.assertIn(x, a)
        # Same port on both ends: playwright-mcp answers only Host: localhost:<its own port>. The
        # forward is bound to loopback explicitly: with no bind address ssh follows GatewayPorts,
        # and `GatewayPorts yes` in ~/.ssh/config would serve the hub to the network.
        self.assertEqual(a[a.index("-L") + 1], "localhost:39032:localhost:39032")
        self.assertIn("GatewayPorts=no", a)
        self.assertEqual(a[-2:], ["--", "fake"])
        self.assertTrue(a[a.index("-S") + 1].endswith("/run/ssh-fake-shop.sock"))
        self.assertEqual(p["EnvironmentVariables"], {"BH_REMOTE_SSH": "fake", "BH_REMOTE_PROFILE": "shop",
                                                     "BH_REMOTE_HUBCTL": self.rhub, "BH_REMOTE_PORT": "39032"})
        out = self.sb.zsh("profiles; is_remote fake-shop && print remote; is_remote shop || print local; "
                          "hub_port fake-shop; cdp_port fake-shop || print no-cdp; profile_exists fake-shop && print exists")
        self.assertEqual(out.split(), ["default", "shop", "fake-shop", "remote", "local", "39032", "no-cdp", "exists"])
        # regen rewrites it from its own record: unchanged
        written = self.text(os.path.join(self.sb.state, "launchd", "test.bhunit.browser-tunnel-fake-shop.plist"))
        self.assertEqual(self.sb.zsh("print -r -- \"$(tunnel_plist fake-shop)\""), written)

    @unittest.skipUnless(os.path.exists("/usr/bin/ssh"), "needs OpenSSH")
    def test_tunnel_stays_on_loopback_whatever_ssh_config_says(self):
        self.write_remote()
        a = self.sb.plist("test.bhunit.browser-tunnel-fake-shop")["ProgramArguments"]
        cfg = os.path.join(self.tmp, "ssh_config")
        with open(cfg, "w") as f:
            f.write("Host *\n  GatewayPorts yes\n")
        # ssh -G prints the configuration it would use, offline. Options on the command line win
        # over the config file, so the hub's port is never opened to the network.
        r = subprocess.run(["/usr/bin/ssh", "-G", "-F", cfg, *a[1:]], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        conf = dict(ln.split(" ", 1) for ln in r.stdout.splitlines() if " " in ln)
        self.assertEqual(conf["gatewayports"], "no")
        self.assertEqual(conf["localforward"], "[localhost]:39032 [localhost]:39032")

    def test_a_socket_path_too_long_for_macos_writes_no_tunnel(self):
        # ssh can't bind a unix socket path over 104 bytes. tunnel_plist refuses, and the refusal
        # must not leave an empty plist behind (plutil -lint passes an empty file).
        deep = os.path.join(self.sb.dir, "s" * 90)
        sb = Sandbox(f"SSH_BIN={self.ssh}\nSTATE_DIR={deep}\n")
        self.addCleanup(sb.cleanup)
        out = subprocess.run(["zsh", "-c", f"""source {ROOT}/lib/common.zsh
            local NEW_HUB=39032 NEW_REMOTE_SSH=fake NEW_REMOTE_PROFILE=shop NEW_REMOTE_HUBCTL=x
            put_plist $(tunnel_label fake-shop) "$(tunnel_plist fake-shop)" """],
            env=sb.env, capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("too long", out.stderr)
        self.assertEqual(glob.glob(os.path.join(deep, "launchd", "*.plist")), [])

    def test_next_ports_skip_remote_ports_without_following_them(self):
        self.write_remote(port=39032)
        self.assertEqual(self.sb.zsh("next_ports").split(), ["29225", "28933"])
        self.write_remote(name="fake-two", port=28933)
        self.assertEqual(self.sb.zsh("next_ports").split(), ["29225", "28934"])

    def test_add_remote_refuses_what_cannot_work(self):
        cases = [
            (["--remote", "fake"], {}, "<ssh-host>:<profile>"),
            (["--remote", "fake:shop", "--ports", "1:2"], {}, "don't apply"),
            (["--remote", "asleep:shop"], {}, "can't reach asleep"),
            (["--remote", "fake:shop"], {"R_MISSING": "1"}, "failed"),
            (["--remote", "fake:shop"], {"R_REMOTE": "1"}, "itself a remote profile"),
            (["--remote", "fake:shop"], {"R_HUB": "28932"}, "own port range"),
        ]
        for args, env, msg in cases:
            r = self.hubctl("add", "fake-shop", *args, "--hubctl", self.rhub, **env)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn(msg, r.stderr, args)
        self.assertEqual(glob.glob(os.path.join(self.sb.state, "launchd", "*tunnel*")), [])
        self.assertNotEqual(self.hubctl("add", "fake-shop", "--hubctl", "x").returncode, 0)

    def test_agent_is_copied_from_this_macs_agent_for_the_profile(self):
        self.write_remote()
        r = self.hubctl("agent", "fake-shop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("from browser-shop.md", r.stdout)
        with open(os.path.join(self.sb.agents, "browser-fake-shop.md")) as f:
            t = f.read()
        self.assertIn("name: browser-fake-shop\n", t)
        self.assertIn("  - browser-fake-shop:\n      type: http\n      url: http://localhost:39032/mcp", t)
        self.assertIn("`mcp__browser-fake-shop__*`", t)
        self.assertNotIn("mcp__browser-shop__", t)
        self.assertIn("Signed in as: the supplier accounts", t)         # the body says what it holds
        self.assertIn(f"{ROOT}/hubctl up fake-shop", t)
        self.assertNotIn("hubctl up shop", t)
        desc = [ln for ln in t.splitlines() if ln.startswith("description: ")][0]
        self.assertTrue(desc.startswith('description: "ON FAKE, A DIFFERENT MAC'), desc)
        self.assertIn("otherwise use browser-shop.", desc)
        self.assertIn("## This Chrome runs on fake, not on this Mac", t)
        self.assertIn("ssh fake cat <path>", t)
        # Rewriting it doesn't stack the note, and lanes work as for any agent.
        self.assertEqual(self.hubctl("agent", "fake-shop", "--force").returncode, 0)
        self.assertEqual(self.text(os.path.join(self.sb.agents, "browser-fake-shop.md")), t)
        self.assertEqual(self.hubctl("lanes", "fake-shop", "2").returncode, 0)
        two = self.text(os.path.join(self.sb.agents, "browser-fake-shop-2.md"))
        self.assertIn("url: http://localhost:39032/mcp", two)
        self.assertIn("`mcp__browser-fake-shop-2__*`", two)

    def test_agent_from_the_template_when_this_mac_has_no_such_profile(self):
        self.write_remote(name="fake-books", rp="books", port=39033)
        self.assertEqual(self.hubctl("agent", "fake-books").returncode, 0)
        t = self.text(os.path.join(self.sb.agents, "browser-fake-books.md"))
        self.assertNotIn("{{", t)
        self.assertIn("url: http://localhost:39033/mcp", t)
        self.assertIn('Signed in as: the same accounts as the "books" profile on fake.', t)
        self.assertIn('description: "ON FAKE, A DIFFERENT MAC: the \\"books\\" browser profile on fake,', t)
        self.assertIn("hubctl up fake-books", t)
        self.assertNotIn("otherwise use", t)

    def test_up_runs_up_on_the_other_mac(self):
        self.fake_tunnel()
        r = self.hubctl("up", "fake-shop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.log(), ["ssh fake", f"remote up shop"])
        self.assertIn("already up", r.stdout)          # so the hook doesn't log a warm call
        self.assertTrue(os.path.exists(os.path.join(self.sb.state, "run", "last-use-fake-shop")))

    def test_up_failures_never_exit_3_and_fail_fast(self):
        # Exit 3 tells the hook "not a browser-hub profile", which would hide a real failure.
        self.fake_tunnel()
        r = self.hubctl("up", "fake-shop", R_MISSING="1")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("unknown profile", r.stderr)
        # With the tunnel up, the next call tries again: one failure doesn't block a working profile.
        r = self.hubctl("up", "fake-shop")
        self.assertEqual(r.returncode, 0, r.stderr)
        # With it down (a sleeping laptop), calls fail fast for a minute without touching ssh.
        self.assertEqual(self.hubctl("up", "fake-shop", R_MISSING="1").returncode, 1)
        sock = self.socks[1].getsockname()
        self.socks[1].close()
        os.remove(sock)
        r = self.hubctl("up", "fake-shop")
        self.assertEqual(r.returncode, 1)
        self.assertIn("less than a minute ago", r.stderr)
        self.assertEqual(self.log(), [], "a fail-fast must not touch ssh")

    def test_unreachable(self):
        self.fake_tunnel()
        self.write_remote(dest="asleep", port=self.socks[0].getsockname()[1])
        r = self.hubctl("up", "fake-shop")
        self.assertEqual(r.returncode, 1)
        self.assertIn("lost the ssh connection to asleep", r.stderr)

    def test_run_open_down_happen_over_there(self):
        self.fake_tunnel()
        r = self.hubctl("run", "fake-shop", "amazon", "B0X")
        self.assertEqual(r.returncode, 3, r.stderr)       # its exit code comes back
        self.assertEqual(r.stdout.strip(), '{"ok":true}')
        self.assertIn("remote run shop amazon B0X", self.log())
        self.assertNotEqual(self.hubctl("run", "fake-shop", "./x.js", "1").returncode, 0)
        self.hubctl("open", "fake-shop", "https://example.com/a b")
        self.assertIn("remote open shop https://example.com/a b", self.log())
        r = self.hubctl("down", "fake-shop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("remote down shop", self.log())
        self.assertIn("tunnel closed", r.stdout)
        info = self.hubctl("info", "fake-shop").stdout
        self.assertIn("remote=fake:shop", info)
        self.assertIn("hub=", info)

    def test_hook_routes_remote_agents_and_their_lanes(self):
        t = TestHook("test_main_session_tool_starts_default")
        t.setUp()
        try:
            self.assertEqual(t.run_hook(t.agent("browser-fake-shop"))[0], ["up fake-shop"])
            call = {"tool_name": "mcp__browser-fake-shop-2__browser_click", "tool_input": {}}
            self.assertEqual(t.run_hook(call)[0], ["up fake-shop"])
        finally:
            t.tearDown()

    @unittest.skipUnless(shutil.which("rsync"), "needs rsync")
    def test_push_and_pull_copy_one_way_and_keep_the_replaced_copy(self):
        self.write_remote()
        local = os.path.join(self.sb.dir, "profiles", "shop")
        remote = os.path.join(self.rprofiles, "shop")

        def put(root, rel, text):
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "w") as f:
                f.write(text)

        put(local, "Default/Cookies", "here")
        put(local, "Default/Cache/blob", "cache")
        put(local, "Default/Code Cache/js", "cache")
        put(local, "SingletonLock", "lock")
        put(local, "Local State", "state")
        put(remote, "Default/Cookies", "there")
        put(remote, "Default/Cache/theirs", "their cache")
        put(remote, "stale", "only there")
        # Both copies as an older install left them: readable by other users. Every copy, and every
        # replaced copy kept beside it, holds live cookies, so each must come out 0700.
        os.chmod(local, 0o755)
        os.chmod(remote, 0o755)
        mode = lambda path: oct(os.stat(path).st_mode & 0o777)

        r = self.hubctl("push", "fake-shop", R_CHROME="up")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--stop", r.stderr)
        # The other Mac's `held` refuses while its Chrome is up, whatever `info` said.
        import socket
        live = socket.socket()
        live.bind(("127.0.0.1", 29324))
        live.listen(1)
        try:
            r = self.hubctl("push", "fake-shop")
        finally:
            live.close()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("is running", r.stderr)
        self.assertEqual(self.text(os.path.join(remote, "Default/Cookies")), "there")
        r = self.hubctl("push", "fake-shop", "--stop", R_CHROME="up")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("remote down shop", self.log())
        self.assertIn(f"remote held shop rsync", " ".join(self.log()))   # the rsync server ran held
        self.assertEqual(self.text(os.path.join(remote, "Default/Cookies")), "here")
        self.assertTrue(os.path.exists(os.path.join(remote, "Local State")))
        self.assertFalse(os.path.exists(os.path.join(remote, "stale")))
        for gone in ["SingletonLock", "Default/Cache/blob", "Default/Code Cache"]:
            self.assertFalse(os.path.exists(os.path.join(remote, gone)), gone)
        self.assertTrue(os.path.exists(os.path.join(remote, "Default/Cache/theirs")), "its own cache stays")
        kept = glob.glob(remote + ".before-copy-*")
        self.assertEqual(len(kept), 1, kept)
        self.assertEqual(self.text(os.path.join(kept[0], "Default/Cookies")), "there")
        self.assertTrue(os.path.exists(os.path.join(kept[0], "stale")))
        self.assertEqual([mode(local), mode(remote), mode(kept[0])], ["0o700"] * 3)

        put(remote, "Default/Cookies", "signed in there")
        os.chmod(local, 0o755)
        os.chmod(remote, 0o755)
        r = self.hubctl("pull", "fake-shop")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.text(os.path.join(local, "Default/Cookies")), "signed in there")
        kept = glob.glob(local + ".before-copy-*")
        self.assertEqual(len(kept), 1, kept)
        self.assertEqual(self.text(os.path.join(kept[0], "Default/Cookies")), "here")
        self.assertEqual([mode(local), mode(kept[0])], ["0o700"] * 2)
        self.assertTrue(os.path.exists(os.path.join(local, "SingletonLock")), "excluded: never deleted")
        # Only the newest two replaced copies are kept.
        for i in range(3):
            time.sleep(1.1)
            self.assertEqual(self.hubctl("push", "fake-shop").returncode, 0)
        self.assertEqual(len(glob.glob(remote + ".before-copy-*")), 2)

    def test_an_interrupted_pull_from_a_loose_copy_still_leaves_this_mac_private(self):
        # rsync -a copies the other side's modes even when the transfer then fails partway.
        self.write_remote()
        local = os.path.join(self.sb.dir, "profiles", "shop")
        remote = os.path.join(self.rprofiles, "shop")
        os.makedirs(os.path.join(remote, "Default"))
        with open(os.path.join(remote, "Default", "Cookies"), "w") as f:
            f.write("there")
        os.chmod(remote, 0o755)
        os.makedirs(local, mode=0o700)
        stub = os.path.join(self.tmp, "rsync-interrupted")
        with open(stub, "w") as f:
            f.write("#!/bin/zsh\nrsync \"$@\"\nexit 23\n")   # did its work, then reported a partial transfer
        os.chmod(stub, 0o755)
        with open(self.sb.config, "a") as f:
            f.write(f"RSYNC_BIN={stub}\n")
        r = self.hubctl("pull", "fake-shop")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rsync failed", r.stderr)
        self.assertEqual(oct(os.stat(local).st_mode & 0o777), "0o700")

    def test_copy_stops_when_this_macs_copy_cannot_be_made_private(self):
        self.write_remote()
        local = os.path.join(self.sb.dir, "profiles", "shop")
        remote = os.path.join(self.rprofiles, "shop")
        os.makedirs(local)
        os.makedirs(remote)
        with open(os.path.join(remote, "marker"), "w") as f:
            f.write("there")
        os.chmod(local, 0o755)
        subprocess.run(["chflags", "uchg", local], check=True)
        try:
            r = self.hubctl("pull", "fake-shop")
        finally:
            subprocess.run(["chflags", "-R", "nouchg", self.sb.dir])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not private", r.stderr)
        self.assertNotIn("copying", r.stdout, "nothing may be copied into a readable profile")

    def test_a_kept_copy_that_cannot_be_made_private_stops_the_copy(self):
        # The other Mac's copy is loose and can't be chmod'ed (cp -p carries the flag to the
        # clone): the clone must not stay behind readable, and nothing is copied.
        self.write_remote()
        local = os.path.join(self.sb.dir, "profiles", "shop")
        remote = os.path.join(self.rprofiles, "shop")
        os.makedirs(local, mode=0o700)
        with open(os.path.join(local, "marker"), "w") as f:
            f.write("here")
        os.makedirs(remote)
        os.chmod(remote, 0o755)
        subprocess.run(["chflags", "uchg", remote], check=True)
        try:
            r = self.hubctl("push", "fake-shop")
            kept = [oct(os.stat(k).st_mode & 0o777) for k in glob.glob(remote + ".before-copy-*")]
        finally:
            subprocess.run(["chflags", "-R", "nouchg", self.rsb.dir])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing was copied", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(remote, "marker")))
        self.assertEqual([k for k in kept if k != "0o700"], [], "a kept copy left readable")

    def test_copy_needs_a_remote_profile_with_a_local_twin(self):
        self.write_remote(name="fake-books", rp="books", port=39033)
        self.assertIn("no profile 'books'", self.hubctl("push", "fake-books").stderr)
        self.assertIn("isn't a remote profile", self.hubctl("push", "shop").stderr)
        self.assertIn("unknown option", self.hubctl("pull", "fake-books", "--force").stderr)


class TestCookieBackup(unittest.TestCase):
    """lib/cookies.mjs: the rules that decide when saved cookies are written, kept and put back."""

    def node(self, expr):
        script = f"import * as c from {json.dumps(os.path.join(ROOT, 'lib', 'cookies.mjs'))};\n" \
                 f"console.log(JSON.stringify({expr}));"
        r = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_cookie_files_are_private_even_over_a_readable_file(self):
        # {mode: 0o600} only applies when a file is created: writing over an existing 0644 file
        # (or a leftover temp file) would keep it readable. Exports and rollbacks go through an
        # exclusively created 0600 temp file, renamed into place.
        tmp = tempfile.mkdtemp(prefix="bh-ck-")
        self.addCleanup(shutil.rmtree, tmp, True)
        dest = os.path.join(tmp, "out.json")
        for f in (dest, f"{dest}.tmp-1"):
            with open(f, "w") as fh:
                fh.write("old")
            os.chmod(f, 0o644)
        self.node(f"(c.writePrivate({json.dumps(dest)}, 'secret'), 1)")
        self.assertEqual(oct(os.stat(dest).st_mode & 0o777), "0o600")
        self.assertEqual(open(dest).read(), "secret")
        self.assertEqual(sorted(os.listdir(tmp)), ["out.json", "out.json.tmp-1"], "no temp file left behind")
        with open(os.path.join(ROOT, "tools", "add-cookies-via-cdp.mjs")) as f:
            tool = f.read()
        self.assertIn("writePrivate(rollback", tool)
        self.assertNotIn("writeFileSync(rollback", tool)

    def test_recovery_output_file_is_made_private_before_anything_is_written(self):
        # `python3 tools/v10-cookies-to-cdp.py Cookies > cookies.json`: the shell creates the file
        # under the umask before the script runs, so the script tightens its own stdout first.
        tmp = tempfile.mkdtemp(prefix="bh-ck-")
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, "cookies.json")
        with open(out, "w") as fh:
            os.chmod(out, 0o644)
            subprocess.run(["python3", "-I", os.path.join(ROOT, "tools", "v10-cookies-to-cdp.py"),
                            os.path.join(tmp, "missing")], stdout=fh, stderr=subprocess.DEVNULL, timeout=30)
        self.assertEqual(oct(os.stat(out).st_mode & 0o777), "0o600")

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_save_never_overwrites_the_export_that_can_undo_a_collapse(self):
        prev = '{chrome: "157.0", count: 1900}'
        self.assertEqual(self.node("c.saveAction(null, '158.0', 5)"), "write")
        self.assertEqual(self.node(f"c.saveAction({prev}, '157.0', 5)"), "write", "same version: a sign-out is real")
        self.assertEqual(self.node(f"c.saveAction({prev}, '158.0', 8)"), "refuse", "new version, jar collapsed")
        self.assertEqual(self.node(f"c.saveAction({prev}, '158.0', 1850)"), "rotate", "new version, jar intact")

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_restore_only_after_an_update_that_emptied_the_jar(self):
        saved = '{chrome: "157.0", count: 1900}'
        self.assertFalse(self.node("c.shouldRestore(null, '158.0', 3)"))
        self.assertTrue(self.node(f"c.shouldRestore({saved}, '158.0', 8)"))
        self.assertFalse(self.node(f"c.shouldRestore({saved}, '158.0', 1700)"))
        self.assertFalse(self.node(f"c.shouldRestore({saved}, '157.0', 3)"), "no update: leave a sign-out alone")
        self.assertEqual(self.node("c.productVersion('HeadlessChrome/158.0.8127.37')"), "158.0.8127.37")

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_restore_skips_what_is_there_expired_or_opaque_and_keeps_attributes(self):
        now = 1_800_000_000
        saved = [
            {"name": "SID", "value": "a", "domain": ".google.com", "path": "/", "expires": now + 9999,
             "session": False, "httpOnly": True, "secure": True, "sameSite": "Lax", "priority": "High",
             "sourceScheme": "Secure", "sourcePort": 443, "size": 4},
            {"name": "NID", "value": "old", "domain": ".google.com", "path": "/", "expires": now + 9999, "session": False},
            {"name": "gone", "value": "x", "domain": "a.com", "path": "/", "expires": now - 1, "session": False},
            {"name": "tab", "value": "s", "domain": "b.com", "path": "/", "expires": -1, "session": True},
            {"name": "chip", "value": "p", "domain": "c.com", "path": "/", "expires": now + 5, "session": False,
             "partitionKey": {"topLevelSite": "https://d.com", "hasCrossSiteAncestor": False}},
            {"name": "op", "value": "q", "domain": "e.com", "path": "/", "expires": now + 5, "session": False,
             "partitionKeyOpaque": True},
        ]
        current = [{"name": "NID", "value": "new", "domain": ".google.com", "path": "/"}]
        out = self.node(f"c.toRestore({json.dumps(saved)}, {json.dumps(current)}, {now})")
        self.assertEqual([c["name"] for c in out], ["SID", "tab", "chip"])
        sid = out[0]
        self.assertEqual(sid, {"name": "SID", "value": "a", "domain": ".google.com", "path": "/", "secure": True,
                               "httpOnly": True, "sameSite": "Lax", "priority": "High", "sourceScheme": "Secure",
                               "sourcePort": 443, "expires": now + 9999})
        self.assertNotIn("expires", out[1], "a session cookie goes back as a session cookie")
        self.assertEqual(out[2]["partitionKey"]["topLevelSite"], "https://d.com")

    def test_command_usage_and_remote_refusal(self):
        sb = Sandbox(base=socket_safe_tmp())   # it writes a tunnel plist
        try:
            self.assertNotEqual(sb.hubctl("cookies", "default").returncode, 0)
            self.assertNotEqual(sb.hubctl("cookies", "default", "frob").returncode, 0)
            self.assertEqual(sb.hubctl("cookies", "nope", "save").returncode, 3)
            sb.zsh("""mkdir -p $PLIST_DIR
                local NEW_HUB=39032 NEW_REMOTE_SSH=fake NEW_REMOTE_PROFILE=shop NEW_REMOTE_HUBCTL=/x
                put_plist $(tunnel_label fake-shop) "$(tunnel_plist fake-shop)" """)
            r = sb.hubctl("cookies", "fake-shop", "save")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("another Mac", r.stderr)
            # With no saved file, a cold start's check costs nothing and does nothing.
            self.assertEqual(sb.zsh("source <(sed -n '/^cookie_file()/,/^tunnel_ok()/p' $HUBCTL); "
                                    "cookies_after_start default; print rc=$?").strip(), "rc=0")
        finally:
            sb.cleanup()
