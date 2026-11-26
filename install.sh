#!/bin/zsh
# install.sh — set browser-hub up on this Mac, or bring an existing install up to date.
# Safe to re-run: every step checks before it changes anything and says what it did.
#
#   ./install.sh               install / update everything
#   ./install.sh --dry-run     say what would be touched, change nothing
#   ./install.sh --force-mcp   replace an existing, different MCP server named "playwright"
#   ./install.sh --force-hooks take over hooks/rules left in ~/.claude by another (gone) install
#   ./install.sh --no-claude-md --no-mcp --no-hooks --no-launchd    skip a step
setopt no_unset pipe_fail extended_glob
ROOT=${0:A:h}

dry=0 force_mcp=0 force_hooks=0 do_md=1 do_mcp=1 do_hooks=1 do_launchd=1
for a in "$@"; do
  case $a in
    --dry-run)      dry=1 ;;
    --force-mcp)    force_mcp=1 ;;
    --force-hooks)  force_hooks=1 ;;
    --no-claude-md) do_md=0 ;;
    --no-mcp)       do_mcp=0 ;;
    --no-hooks)     do_hooks=0 ;;
    --no-launchd)   do_launchd=0 ;;
    -h|--help)      awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' $0; exit 0 ;;
    *)              print -u2 "install.sh: unknown option $a"; exit 2 ;;
  esac
done

step() { print -r -- ""; print -r -- "==> $*" }
take_over=(); (( force_hooks )) && take_over=(--force)
fail() { print -u2 -r -- "install.sh: $*"; exit 1 }

[[ $OSTYPE == darwin* ]] || fail "browser-hub needs macOS: it runs on launchd"

# ------------------------------------------------------------------- config --
export BROWSER_HUB_CONFIG=${BROWSER_HUB_CONFIG:-$HOME/.config/browser-hub/config}
step "Config: $BROWSER_HUB_CONFIG"
if [[ -f $BROWSER_HUB_CONFIG ]]; then
  print -r -- "  using the existing config (edit it, then re-run this or \`hubctl regen\`)"
else
  chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  [[ -x $chrome ]] || chrome="$HOME$chrome"
  [[ -x $chrome ]] || fail "Google Chrome isn't installed (looked in /Applications and ~/Applications)"
  # Prefer Homebrew's node: its path survives upgrades, and it runs fine under launchd. Version
  # managers' shims (asdf, mise, nodenv, volta, fnm) usually don't: launchd starts jobs in / with
  # none of the shell setup the shim relies on.
  nodebin=
  for d in /opt/homebrew/bin /usr/local/bin ${commands[npx]:h}; do
    [[ -x $d/npx && -x $d/node ]] || continue
    v=$($d/node --version 2>/dev/null); [[ ${${v#v}%%.*} == <-> ]] && (( ${${v#v}%%.*} >= 18 )) || continue
    nodebin=$d; break
  done
  [[ -n $nodebin ]] || fail "no node 18+ with npx found: install it first (e.g. brew install node)"
  [[ $nodebin == *(asdf|mise|nodenv|volta|fnm|shims)* ]] &&
    print -r -- "  warning: $nodebin looks like a version manager's shim; launchd jobs may not be able to run it"
  prefix=com.${USER:-$(id -un)}
  print -r -- "  new config: LABEL_PREFIX=$prefix  NODE_BIN=$nodebin"
  [[ $nodebin == */.nvm/versions/* ]] &&
    print -r -- "  note: that node is inside nvm — the hubs will break if that version is removed"
  if (( ! dry )); then
    mkdir -p ${BROWSER_HUB_CONFIG:h}
    sed -e "s|^LABEL_PREFIX=.*|LABEL_PREFIX=$prefix|" \
        -e "s|^NODE_BIN=.*|NODE_BIN=$nodebin|" \
        -e "s|^CHROME=.*|CHROME=\"$chrome\"|" $ROOT/config.example > $BROWSER_HUB_CONFIG
  fi
fi
source $ROOT/lib/common.zsh

# ---------------------------------------------------------------- preflight --
step "Preflight"
problems=()
[[ -x $CHROME ]] || problems+=("no Chrome at CHROME=$CHROME")
[[ -x $NODE_BIN/npx ]] || problems+=("no npx in NODE_BIN=$NODE_BIN")
v=$($NODE_BIN/node --version 2>/dev/null); major=${${v#v}%%.*}
[[ $major == <-> ]] && (( major >= 18 )) || problems+=("node 18+ needed, found ${v:-none} in $NODE_BIN")
(( $+commands[python3] )) || problems+=("no python3 (xcode-select --install)")
sp=$(paths_with_spaces)
[[ -z $sp ]] || problems+=("paths with spaces aren't supported: ${sp//$'\n'/, } (clone the repo somewhere without spaces)")
# The default ports must be free, or already held by this hub's own jobs.
port_up $DEFAULT_CDP_PORT && ! lc_running $(chrome_label default) &&
  problems+=("port $DEFAULT_CDP_PORT (DEFAULT_CDP_PORT) is taken by something else")
port_up $DEFAULT_HUB_PORT && ! lc_running $(hub_label default) &&
  problems+=("port $DEFAULT_HUB_PORT (DEFAULT_HUB_PORT) is taken by something else")
if (( $#problems )); then
  print -u2 -rl -- '  '${^problems}
  fail "fix the above (ports and paths are set in $BROWSER_HUB_CONFIG), then re-run"
fi
print -r -- "  Chrome, node $v, python3 and ports $DEFAULT_CDP_PORT/$DEFAULT_HUB_PORT: ok"
# macOS privacy protection denies launchd jobs access to these folders.
[[ $ROOT == $HOME/(Documents|Desktop|Downloads|Library/Mobile\ Documents)(/*|) ]] &&
  print -r -- "  warning: $ROOT is in a privacy-protected folder; launchd jobs may be denied access. Somewhere like ~/browser-hub is safer."
# A second install (a sandbox) must not share Claude Code's files with the first.
if [[ $BROWSER_HUB_CONFIG != $DEFAULT_CONFIG ]]; then
  for v in CLAUDE_SETTINGS CLAUDE_MD AGENTS_DIR LAUNCH_AGENTS_DIR; do
    [[ ${(P)v} == $HOME/(.claude|Library/LaunchAgents)* ]] &&
      print -r -- "  note: non-default config, but $v is the real ${(P)v}. Fine for a relocated primary install; for a sandbox, point it into the sandbox."
  done
fi

if (( dry )); then
  step "Dry run — nothing changed. A real run would touch:"
  [[ -f $BROWSER_HUB_CONFIG ]] || print -r -- "  $BROWSER_HUB_CONFIG   (new config, from what was detected)"
  if (( do_launchd )); then
    print -r -- "  $PLIST_DIR/   launchd jobs for every profile + the idle reaper (generated)"
    print -r -- "  $LAUNCH_AGENTS_DIR/$LABEL_PREFIX.{agent-chrome,playwright-hub,browser-hub-reaper}*.plist   (symlinks)"
    print -r -- "  $DEFAULT_PROFILE_DIR   the default profile's browser data (created on first start)"
  fi
  (( do_mcp )) && print -r -- "  claude mcp, user scope: server \"playwright\" -> http://localhost:$DEFAULT_HUB_PORT/mcp"
  (( do_hooks )) && print -r -- "  $CLAUDE_SETTINGS   two PreToolUse hooks running: $(hook_command)   (backed up first)"
  print -r -- "  $AGENTS_DIR/browser-default.md   unless an agent already targets the default hub"
  (( do_md )) && print -r -- "  $CLAUDE_MD   a marked browser rules block (backed up first)"
  exit 0
fi

# ------------------------------------------------------------------ package --
step "Fetching $PW_MCP (first run downloads it)"
# npx runs `node` from PATH, which over ssh (or from launchd) may not include NODE_BIN.
PATH=$NODE_BIN:$PATH $NODE_BIN/npx -y $PW_MCP --help >/dev/null 2>&1 || fail "could not run $PW_MCP with $NODE_BIN/npx"
print -r -- "  ok"

# ------------------------------------------------------------------ launchd --
if (( do_launchd )); then
  step "launchd jobs"
  $HUBCTL regen || fail "hubctl regen failed"
  for i in {1..120}; do port_up $DEFAULT_HUB_PORT && break; sleep 0.5; done
  port_up $DEFAULT_HUB_PORT || fail "the default hub never came up on :$DEFAULT_HUB_PORT (see $LOG_DIR/hub-default.log)"
  print -r -- "  the default hub is listening on :$DEFAULT_HUB_PORT"
fi

# ------------------------------------------------------------- claude code --
url=http://localhost:$DEFAULT_HUB_PORT/mcp
claude=${CLAUDE_BIN:-claude}
if (( do_mcp )); then
  step "Claude Code MCP server \"playwright\""
  if ! (( $+commands[$claude] )) && [[ ! -x $claude ]]; then
    print -r -- "  no claude CLI on PATH. Register it yourself:"
    print -r -- "    claude mcp add --scope user --transport http playwright $url"
  elif cur=$($claude mcp get playwright 2>&1); [[ $? -ne 0 ]]; then
    $claude mcp add --scope user --transport http playwright $url >/dev/null || fail "claude mcp add failed"
    print -r -- "  added (user scope) -> $url"
  elif [[ $cur == *"$url"* ]]; then
    print -r -- "  already points at $url"
  elif (( force_mcp )); then
    $claude mcp remove playwright -s user >/dev/null || fail "could not remove the old 'playwright' server"
    $claude mcp add --scope user --transport http playwright $url >/dev/null || fail "claude mcp add failed"
    print -r -- "  replaced the old 'playwright' server -> $url"
  else
    print -r -- "  a different MCP server named \"playwright\" already exists:"
    print -rl -- ${${(f)cur}[1,6]/#/    }
    # Stop here: hooks and rules for a hub the session's tools don't talk to would boot a
    # Chrome nothing uses on every browser call, and describe tools that aren't there.
    fail "stopped before the hooks and CLAUDE.md. Re-run with --force-mcp to replace that server, or remove it yourself."
  fi
fi

if (( do_hooks )); then
  step "PreToolUse hooks"
  claude_config hooks-install $CLAUDE_SETTINGS "$(hook_command)" $TOOL_MATCHER $take_over; rc=$?
  (( rc == 3 )) && fail "another browser-hub install owns the hooks in $CLAUDE_SETTINGS (see above)"
  (( rc == 0 )) || fail "could not update $CLAUDE_SETTINGS"
fi

step "Agent for the default profile"
existing=($(agents_using $DEFAULT_HUB_PORT)) wrote_default=0
if (( $#existing )); then
  print -r -- "  an agent already targets the default hub: ${(j:, :)${existing[@]:t}}"
else
  $HUBCTL agent default && wrote_default=1
fi

if (( do_md )); then
  step "Browser rules in $CLAUDE_MD"
  block=$(mktemp)
  render $ROOT/templates/claude-md.md HUBCTL="$(hubctl_command)" OUTPUT_DIR=$OUT_DIR CONFIG=$BH_CONFIG > $block
  claude_config md-install $CLAUDE_MD $block $take_over; rc=$?
  (( rc == 0 || rc == 3 )) || fail "could not update $CLAUDE_MD"
  rm -f $block
fi

step "Health check"
skips=()
(( do_mcp )) || skips+=(--no-mcp)
(( do_hooks )) || skips+=(--no-hooks)
(( do_md )) || skips+=(--no-claude-md)
$HUBCTL doctor $skips
rc=$?

cat <<EOF

Next:
  1. Restart any open Claude Code sessions: hooks and MCP servers load at session start.
  2. Sign the default profile into your accounts (it's a normal Chrome window; install
     extensions like 1Password there too):
       $(hubctl_command) open default https://accounts.google.com
$( (( wrote_default )) && print -r -- "  3. Say what it's signed into, so work gets routed to browser-default correctly:
       $(hubctl_command) agent default --identity \"personal Google account, GitHub, ...\" --force" )
  4. Want a separate identity (work, a client, a shop account)? Add a profile:
       $(hubctl_command) add work --identity "work Google account, Jira, AWS console"
  Everything else: $ROOT/README.md
EOF
exit $rc
