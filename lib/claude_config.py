#!/usr/bin/env python3
"""Structured edits to Claude Code's own files, for install.sh, uninstall.sh, hubctl and the tests.

These are JSON and markdown edits that must preserve everything else the user already has in those
files. Every write is atomic, follows symlinks (dotfiles repos), and backs the old file up first.
Nothing is written when nothing would change.

Ownership: several browser-hub installs can coexist (a sandbox next to a real one), so every edit
is scoped to one install, identified by its config path. A hook belongs to the install whose
config its command names (`BROWSER_HUB_CONFIG=<path> …/ensure-profile.sh`, or the default config
when there is no prefix); the CLAUDE.md block carries `config=<path>` in its begin marker. One
install never rewrites or removes another's unless forced.

  hooks-install  <settings> <hook-command> <tool-matcher> <config> <default-config> [--force]
  hooks-remove   <settings> <config> <default-config>
  hooks-check    <settings> <hook-command> <tool-matcher> <config> <default-config>
  md-install     <CLAUDE.md> <rendered-block-file> <config> <default-config> [--force]
  md-remove      <CLAUDE.md> <config> <default-config>
  md-check       <CLAUDE.md> <config> <default-config>
  lanes          <base-agent.md> <count>
  remote-agent   <src-agent.md> <out.md> <name> <url> <profile> <src-profile> <host> <hubctl> <remote-profile>

Exit status 3 means "another install owns this"; nothing was changed.
"""
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import textwrap
import time

HOOK_SCRIPT = "/ensure-profile.sh"
BEGIN = "<!-- browser-hub:begin"
END = "<!-- browser-hub:end -->"
CONFLICT = 3


def atomic_write(path, text):
    real = os.path.realpath(path)
    os.makedirs(os.path.dirname(real), exist_ok=True)
    if os.path.exists(real):
        backup = f"{real}.bak-browser-hub-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(real, backup)
        print(f"  backed up {path} -> {backup}")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=".browser-hub-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    if os.path.exists(real):
        shutil.copymode(real, tmp)
    os.replace(tmp, real)


def read(path):
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


# ------------------------------------------------------------------------ ownership --

def is_hub_hook(hook):
    cmd = hook.get("command", "")
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    return hook.get("type") == "command" and bool(words) and words[-1].endswith(HOOK_SCRIPT)


def config_of_command(cmd, default_config):
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    for w in words:
        if w.startswith("BROWSER_HUB_CONFIG="):
            return w.split("=", 1)[1]
    return default_config


class Owner:
    def __init__(self, config, default_config, force=False):
        self.config, self.default, self.force = config, default_config, force

    def owns_hook(self, hook):
        return is_hub_hook(hook) and (
            self.force or config_of_command(hook["command"], self.default) == self.config)

    def foreign_hooks(self, data):
        return [h["command"] for e in data.get("hooks", {}).get("PreToolUse", [])
                for h in e.get("hooks", []) if is_hub_hook(h) and not self.owns_hook(h)]

    def block_config(self, text):
        m = re.search(re.escape(BEGIN) + r"\s+config=(\S+)", text)
        return m.group(1) if m else None

    def owns_block(self, text):
        # An untagged block predates tagging; it belongs to the default install.
        return self.force or (self.block_config(text) or self.default) == self.config


# ---------------------------------------------------------------- settings.json hooks --

def load_json(path):
    text = read(path)
    return json.loads(text) if text.strip() else {}


def dump_json(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def with_hooks(data, cmd, tool_matcher, owner):
    """Return a copy of settings with exactly our two PreToolUse entries, edited in place where
    they already exist so the rest of the file (and entry order) is left alone."""
    data = json.loads(json.dumps(data))
    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    want = {"Agent": False, tool_matcher: False}
    out = []
    for entry in pre:
        hooks = entry.get("hooks", [])
        ours = [h for h in hooks if owner.owns_hook(h)]
        if not ours:
            out.append(entry)
            continue
        others = [h for h in hooks if not owner.owns_hook(h)]
        m = entry.get("matcher", "")
        slot = "Agent" if m == "Agent" else (
            tool_matcher if ("playwright" in m or "browser" in m) else None)
        if slot and not want[slot] and not others:
            want[slot] = True
            out.append({**entry, "matcher": slot, "hooks": [{**ours[0], "command": cmd}]})
        elif others:
            out.append({**entry, "hooks": others})
        # anything else is a duplicate of ours: drop it
    for slot, done in want.items():
        if not done:
            out.append({"matcher": slot, "hooks": [{"type": "command", "command": cmd}]})
    data["hooks"]["PreToolUse"] = out
    return data


def without_hooks(data, owner):
    data = json.loads(json.dumps(data))
    hooks = data.get("hooks", {})
    pre = hooks.get("PreToolUse")
    if pre is None:
        return data
    out = []
    for entry in pre:
        kept = [h for h in entry.get("hooks", []) if not owner.owns_hook(h)]
        if kept or not entry.get("hooks"):
            out.append({**entry, "hooks": kept})
    if out:
        hooks["PreToolUse"] = out
    else:
        hooks.pop("PreToolUse")
        if not hooks:
            data.pop("hooks")
    return data


def hooks_install(path, cmd, tool_matcher, config, default_config, force=False):
    owner = Owner(config, default_config, force)
    old = load_json(path)
    foreign = owner.foreign_hooks(old)
    if foreign:
        print(f"  {path} already has browser-hub hooks belonging to a different install:")
        for c in foreign:
            print(f"    {c}")
        print("  Left alone. If that install is gone, re-run with --force-hooks to take them over.")
        return CONFLICT
    new = with_hooks(old, cmd, tool_matcher, owner)
    if new == old:
        print(f"  hooks already registered in {path}")
        return 0
    atomic_write(path, dump_json(new))
    print(f"  registered the PreToolUse hooks in {path}")
    return 0


def hooks_remove(path, config, default_config):
    if not os.path.exists(path):
        return 0
    owner = Owner(config, default_config)
    old = load_json(path)
    new = without_hooks(old, owner)
    if new != old:
        atomic_write(path, dump_json(new))
        print(f"  removed this install's hooks from {path}")
    return 0


def hooks_check(path, cmd, tool_matcher, config, default_config):
    owner = Owner(config, default_config)
    data = load_json(path)
    found = {}
    for entry in data.get("hooks", {}).get("PreToolUse", []):
        for h in entry.get("hooks", []):
            if owner.owns_hook(h):
                found.setdefault(entry.get("matcher", ""), []).append(h.get("command"))
    problems = []
    for slot in ("Agent", tool_matcher):
        if found.get(slot) != [cmd]:
            problems.append(f"matcher {slot!r}: expected [{cmd}], found {found.get(slot)}")
    stray = set(found) - {"Agent", tool_matcher}
    if stray:
        problems.append(f"unexpected hooks under matchers {sorted(stray)}")
    for c in owner.foreign_hooks(data):
        problems.append(f"another install's hook is also registered: {c}")
    for p in problems:
        print(p)
    return 1 if problems else 0


# ------------------------------------------------------------------- CLAUDE.md block --

def split_block(text):
    """-> (before, block, after) or None when the markers are absent."""
    i = text.find(BEGIN)
    j = text.find(END, i if i >= 0 else 0)
    if i < 0 or j < 0:
        return None
    return text[:i], text[i:j + len(END)], text[j + len(END):]


def custom_section(text):
    return re.search(r"hubctl|ensure-profile\.sh|browser-hub", text)


def md_install(path, block_file, config, default_config, force=False):
    owner = Owner(config, default_config, force)
    block = read(block_file).strip("\n")
    text = read(path)
    parts = split_block(text)
    if parts and not owner.owns_block(parts[1]):
        print(f"  {path} holds the rules block of a different install "
              f"(config={owner.block_config(parts[1])}) — left alone")
        return CONFLICT
    if parts:
        before, _, after = parts
        new = before + block + after
    elif custom_section(text):
        print(f"  {path} already has its own browser-hub section (no markers) — left alone")
        return 0
    else:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"
    if new == text:
        print(f"  browser rules already current in {path}")
        return 0
    atomic_write(path, new)
    print(f"  wrote the browser rules block into {path}")
    return 0


def md_remove(path, config, default_config):
    text = read(path)
    parts = split_block(text)
    if not parts or not Owner(config, default_config).owns_block(parts[1]):
        return 0
    before, _, after = parts
    before, after = before.rstrip("\n"), after.lstrip("\n")
    new = (before + "\n\n" + after if before and after else before + after).rstrip("\n") + "\n"
    atomic_write(path, new if new.strip() else "")
    print(f"  removed the browser rules block from {path}")
    return 0


def md_check(path, config, default_config):
    text = read(path)
    parts = split_block(text)
    if parts:
        print("managed" if Owner(config, default_config).owns_block(parts[1]) else "other")
    elif custom_section(text):
        print("custom")
    else:
        print("missing")
    return 0


# ------------------------------------------------------------------------------ lanes --

LANE_NOTE = re.compile(r"\s*PARALLEL LANE \d+ of \d+ —.*?never spawn two of the same lane\.")


def lanes(base_path, count):
    """Clone browser-<x>.md into browser-<x>-2 … -<count>. Each lane differs only in its name and
    its MCP server name, which gives it its own connection and its own current-tab pointer."""
    count = int(count)
    base_dir, base_file = os.path.split(base_path)
    base = base_file[:-3]
    src = read(base_path)

    def with_note(text, i):
        def relane(m):
            value = m.group(1)
            quoted = len(value) >= 2 and value[0] == value[-1] == '"'
            desc = LANE_NOTE.sub("", value[1:-1] if quoted else value).rstrip()
            if count >= 2:
                desc += (f" PARALLEL LANE {i} of {count} — identical to the other lanes, and safe "
                         f"to run concurrently with them. When running several of these agents at "
                         f"once, give each a DIFFERENT lane; never spawn two of the same lane.")
            return f'description: "{desc}"' if quoted else f"description: {desc}"
        return re.sub(r"^description: (.*)$", relane, text, count=1, flags=re.M)

    def renamed(text, lane):
        # Only the three places that ARE the agent's identity: its name, its MCP server key, and
        # that server's tool namespace. Anything else (a path that happens to contain the name,
        # say) is left alone.
        text = re.sub(r"^name: " + re.escape(base) + r"\s*$", f"name: {lane}", text, count=1, flags=re.M)
        text = re.sub(r"^(\s*-\s*)" + re.escape(base) + r":\s*$", rf"\g<1>{lane}:", text, count=1, flags=re.M)
        return text.replace(f"mcp__{base}__", f"mcp__{lane}__")

    # Lane 1 is the base agent itself; keep its note in step with the lane count.
    new_base = with_note(src, 1)
    if new_base != src:
        with open(base_path, "w") as f:
            f.write(new_base)
        print(f"  updated {base_path} (lane 1)")
    for i in range(2, count + 1):
        lane = f"{base}-{i}"
        path = os.path.join(base_dir, f"{lane}.md")
        with open(path, "w") as f:
            f.write(with_note(renamed(new_base, lane), i))
        print(f"  wrote {path}")
    for name in os.listdir(base_dir):  # drop lanes beyond the new count
        m = re.fullmatch(re.escape(base) + r"-(\d+)\.md", name)
        if m and int(m.group(1)) > count:
            os.remove(os.path.join(base_dir, name))
            print(f"  removed {os.path.join(base_dir, name)}")
    return 0


# ----------------------------------------------------------------------- remote agents --

REMOTE_NOTE = "## This Chrome runs on "


def remote_agent(src_path, out_path, name, url, profile, src_profile, host, hubctl, remote_profile):
    """Write the agent for a remote profile from `src_path`: the agent of this Mac's profile of the
    same name (or a freshly rendered template). It keeps that agent's body, which says what the
    profile is signed into, and changes what makes it a different agent: its name, its MCP server
    (name, tool namespace and URL: the tunnel), the profile its hubctl commands name, and its
    description, which must keep the orchestrator from sending ordinary work to the other Mac."""
    text = read(src_path)
    m = re.search(r"^name: (\S+)\s*$", text, flags=re.M)
    if not m:
        print(f"{src_path}: no name: line", file=sys.stderr)
        return 1
    base = m.group(1)
    text = re.sub(r"^name: " + re.escape(base) + r"\s*$", f"name: {name}", text, count=1, flags=re.M)
    text = re.sub(r"^(\s*-\s*)" + re.escape(base) + r":\s*$", rf"\g<1>{name}:", text, count=1, flags=re.M)
    text = re.sub(r"^(\s*url:\s*)\S+\s*$", rf"\g<1>{url}", text, count=1, flags=re.M)
    text = text.replace(f"mcp__{base}__", f"mcp__{name}__")
    # `hubctl up work` -> `hubctl up laptop-work`, and run/open likewise. Not work-quick.
    text = re.sub(r"(hubctl\s+(?:up|run|open)\s+)" + re.escape(src_profile) + r"(?![\w-])",
                  rf"\g<1>{profile}", text)
    if base == name:  # rendered from the template: this Mac has no such profile to compare with
        what = f"the \"{remote_profile}\" browser profile on {host}"
    else:
        what = f"a copy of this Mac's browser profile, signed into the same accounts as {base}"
    desc = (f"ON {host.upper()}, A DIFFERENT MAC: {what}, in a Chrome that runs on {host}. Use only "
            f"when the task asks for {host} (for example, so someone there can watch or take over)"
            + ("" if base == name else f"; otherwise use {base}") + ".")
    text = re.sub(r"^description: .*$", lambda _: 'description: "' + desc.replace('"', '\\"') + '"',
                  text, count=1, flags=re.M)
    if REMOTE_NOTE not in text:
        text = text.rstrip("\n") + "\n\n" + textwrap.dedent(f"""\
            {REMOTE_NOTE}{host}, not on this Mac
            - Every browser call goes through an ssh tunnel to {host}'s browser-hub. If {host} is
              asleep or offline, calls fail with "can't reach {host}". Report that and stop; don't
              switch to another profile.
            - The browser's own paths are {host}'s. A screenshot or snapshot saved with `filename`,
              and anything downloaded, lands on {host}: read it with `ssh {host} cat <path>`, or
              copy it here with `scp {host}:<path> <dest>`. A file you upload must already be on
              {host} (`scp <file> {host}:/tmp/` first).
            - `{hubctl} up {profile}` (and `run`, `open`) work from this Mac and act on {host}.
            """)
    with open(out_path, "w") as f:
        f.write(text)
    return 0


def main(argv):
    cmds = {
        "hooks-install": hooks_install, "hooks-remove": hooks_remove, "hooks-check": hooks_check,
        "md-install": md_install, "md-remove": md_remove, "md-check": md_check, "lanes": lanes,
        "remote-agent": remote_agent,
    }
    if len(argv) < 2 or argv[1] not in cmds:
        print(__doc__, file=sys.stderr)
        return 2
    args = [a for a in argv[2:] if a != "--force"]
    if "--force" in argv[2:]:
        return cmds[argv[1]](*args, force=True)
    return cmds[argv[1]](*args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
