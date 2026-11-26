# lib/common.zsh — shared by hubctl, install.sh, uninstall.sh and ensure-profile.sh. Sourced, never run.
#
# Everything machine-specific comes from ONE config file (see config.example). Everything else is
# derived: a profile exists because its generated launchd plist exists, and its ports are read
# back out of that plist. There is no registry to drift out of step with reality.

setopt no_unset pipe_fail extended_glob

BH_ROOT=${${(%):-%x}:A:h:h}
BH_VERSION=$(<$BH_ROOT/VERSION)
DEFAULT_CONFIG=$HOME/.config/browser-hub/config
BH_CONFIG=${BROWSER_HUB_CONFIG:-$DEFAULT_CONFIG}

# ------------------------------------------------------------------ defaults --
# Every one of these can be overridden in the config file. install.sh writes that file on first
# run with the values it detected; after that, edit it and run `hubctl regen`.
LABEL_PREFIX=com.${USER:-$(id -un)}
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
NODE_BIN=
PW_MCP=@playwright/mcp@0.0.85
FOCUS_GUARD=1
PROFILES_DIR=$HOME/.local/share/browser-hub/profiles
DEFAULT_PROFILE_DIR=
DEFAULT_CDP_PORT=9223
DEFAULT_HUB_PORT=8931
STATE_DIR=$BH_ROOT/var
LAUNCH_AGENTS_DIR=$HOME/Library/LaunchAgents
AGENTS_DIR=$HOME/.claude/agents
CLAUDE_MD=$HOME/.claude/CLAUDE.md
CLAUDE_SETTINGS=$HOME/.claude/settings.json
ACTION_TIMEOUT_MS=15000
SNAPSHOT_MODE=none
DISK_CACHE_BYTES=209715200
IDLE_MINUTES=120
REAP_INTERVAL_MINUTES=15
OUTPUT_RETENTION_DAYS=30
LOG_MAX_MB=10
# Chrome reads native-messaging manifests (how extensions like 1Password reach their desktop app)
# from <user-data-dir>/NativeMessagingHosts, not from the stock Chrome directory. Every hub
# profile has its own user-data-dir, so these hosts are linked into each one.
NATIVE_HOSTS_SRC=$HOME/Library/Application\ Support/Google/Chrome/NativeMessagingHosts
typeset -ga NATIVE_HOSTS
NATIVE_HOSTS=(com.1password.1password)
typeset -gA AGENT_PROFILES
AGENT_PROFILES=()
typeset -ga CHROME_EXTRA_ARGS
CHROME_EXTRA_ARGS=()
# Logins across Chrome updates (lib/cookies.mjs, DESIGN §1): save each profile's cookies when its
# Chrome stops and every COOKIE_SAVE_MINUTES while it runs; put them back after an update drops them.
COOKIE_BACKUP=1
COOKIE_SAVE_MINUTES=60
# Remote profiles (a profile whose Chrome runs on another Mac) are reached over ssh.
SSH_BIN=/usr/bin/ssh
RSYNC_BIN=rsync

[[ -r $BH_CONFIG ]] && source $BH_CONFIG

# ------------------------------------------------------------------- derived --
: ${DEFAULT_PROFILE_DIR:=$PROFILES_DIR/default}
: ${NODE_BIN:=${${commands[npx]:-/opt/homebrew/bin/npx}:h}}
PLIST_DIR=$STATE_DIR/launchd
LOG_DIR=$STATE_DIR/log
OUT_DIR=$STATE_DIR/output
RUN_DIR=$STATE_DIR/run
HUBCTL=$BH_ROOT/hubctl
HOOK=$BH_ROOT/ensure-profile.sh
REAPER_LABEL=$LABEL_PREFIX.browser-hub-reaper
# The PreToolUse matcher for browser tool calls: the main session's `playwright` server (the
# default profile) and every browser-* agent's own server (`mcp__browser-<profile>[-<lane>]__*`).
TOOL_MATCHER='mcp__playwright__.*|mcp__browser-.*'

# The command registered as the PreToolUse hook. A non-default config path has to travel with it,
# because the hook runs inside Claude Code's environment, not the shell that ran install.sh.
hook_command() { with_config $HOOK }
# Likewise for the recovery command written into agent files and the CLAUDE.md block: an agent
# that runs a bare `hubctl` against a non-default install would be looking at the wrong hub.
hubctl_command() { with_config $HUBCTL }
with_config() {
  if [[ $BH_CONFIG == $DEFAULT_CONFIG ]]; then print -r -- ${(q)1}
  else print -r -- "BROWSER_HUB_CONFIG=${(q)BH_CONFIG} ${(q)1}"; fi
}

# Claude Code's own files, scoped to this install (see lib/claude_config.py on ownership).
claude_config() {  # subcommand args... — config args are appended
  local sub=$1; shift
  python3 $BH_ROOT/lib/claude_config.py $sub "$@" $BH_CONFIG $DEFAULT_CONFIG
}

# Paths end up unquoted in command substitutions and plist-building arrays throughout, where zsh
# would split them on spaces. Rather than trust every call site, refuse them up front.
paths_with_spaces() {
  local v
  for v in BH_ROOT STATE_DIR PROFILES_DIR DEFAULT_PROFILE_DIR LAUNCH_AGENTS_DIR AGENTS_DIR NODE_BIN; do
    [[ ${(P)v} == *[[:space:]]* ]] && print -r -- "$v=${(P)v}"
  done
  return 0
}

headless() { (( ${#${(M)CHROME_EXTRA_ARGS:#--headless*}} )) }

die()  { print -u2 -r -- "hubctl: $*"; exit 1 }
warn() { print -u2 -r -- "hubctl: $*" }

# -------------------------------------------------------------------- naming --
# The default profile keeps unsuffixed launchd labels; every other profile is suffixed -<name>.
suffix_of()   { [[ $1 == default ]] && print -r -- "" || print -r -- "-$1" }
chrome_label(){ print -r -- $LABEL_PREFIX.agent-chrome$(suffix_of $1) }
hub_label()   { print -r -- $LABEL_PREFIX.playwright-hub$(suffix_of $1) }
tunnel_label(){ print -r -- $LABEL_PREFIX.browser-tunnel-$1 }   # remote profiles only
plist_of()    { print -r -- $PLIST_DIR/$1.plist }            # label -> generated plist
la_of()       { print -r -- $LAUNCH_AGENTS_DIR/$1.plist }    # label -> LaunchAgents symlink
profile_dir() { [[ $1 == default ]] && print -r -- $DEFAULT_PROFILE_DIR || print -r -- $PROFILES_DIR/$1 }
# A profile directory holds every login in it: cookies encrypted under the mock keychain's public
# key, and the plaintext save (lib/cookies.mjs). Only its owner may read it. Created 0700 whatever
# the umask (parents too), and tightened when it, or a cookie save in it, is looser. It fails
# closed: a mkdir or chmod that didn't take, checked by reading the mode back, is an error the
# callers stop on, never a quiet pass.
secure_profile_dir() {  # profile -> 0 only when the directory is 0700 and its cookie saves 0600
  local d=$(profile_dir $1) f
  if [[ ! -d $d ]]; then
    ( umask 077; mkdir -p -- $d ) 2>/dev/null || { warn "could not create $d"; return 1 }
  fi
  [[ $(stat -f %Lp -- $d 2>/dev/null) == 700 ]] || chmod 700 -- $d 2>/dev/null
  [[ $(stat -f %Lp -- $d 2>/dev/null) == 700 ]] || { warn "$d is not private and could not be made mode 700"; return 1 }
  for f in $d/browser-hub-cookies.json(|.prev)(N); do
    [[ $(stat -f %Lp -- $f 2>/dev/null) == 600 ]] || chmod 600 -- $f 2>/dev/null
    [[ $(stat -f %Lp -- $f 2>/dev/null) == 600 ]] || { warn "$f is not private and could not be made mode 600"; return 1 }
  done
  return 0
}
# profile_perms profile -> one line per problem (for doctor); nothing when the profile is private
profile_perms() {
  local d=$(profile_dir $1) f m
  [[ -d $d ]] || return 0
  m=$(stat -f %Lp $d); [[ $m == 700 ]] || print -r -- "$d is mode $m, not 700"
  for f in $d/browser-hub-cookies.json(|.prev)(N); do
    m=$(stat -f %Lp $f); [[ $m == 600 ]] || print -r -- "$f is mode $m, not 600"
  done
  return 0
}

# Link each NATIVE_HOSTS manifest from the stock Chrome directory into the profile's own. Idempotent
# and cheap. A host that isn't installed is skipped, and a real file already in the profile is the
# user's and is left alone. Prints one line per link it (re)made.
link_native_hosts() {  # profile
  local h src dest d=$(profile_dir $1)/NativeMessagingHosts
  for h in $NATIVE_HOSTS; do
    src=$NATIVE_HOSTS_SRC/$h.json dest=$d/$h.json
    [[ -f $src ]] || continue
    [[ -L $dest && $(readlink $dest) == $src && -e $dest ]] && continue
    [[ -e $dest && ! -L $dest ]] && continue
    mkdir -p $d && ln -sfn $src $dest && print -r -- "linked $h into $1"
  done
  return 0
}
# native_hosts_ok profile -> 0 when every installed host is reachable from the profile
native_hosts_ok() {
  local h d=$(profile_dir $1)/NativeMessagingHosts
  for h in $NATIVE_HOSTS; do
    [[ -f $NATIVE_HOSTS_SRC/$h.json ]] || continue
    [[ -e $d/$h.json ]] || return 1
  done
  return 0
}
hub_url()     { print -r -- http://localhost:$(hub_port $1)/mcp }
always_on()   { [[ $1 == default ]] }   # only the default hub stays up while its Chrome is cold

# A profile name becomes part of paths, launchd labels, agent names and MCP tool names. Keep it to
# lowercase words joined by single dashes, and never ending in -<digits>: that shape means "lane".
valid_name() { [[ $1 == [a-z][a-z0-9]#(-[a-z][a-z0-9]#)# ]] }

# ----------------------------------------------------------------- discovery --
profiles() {  # every profile: default, then one per generated Chrome plist, then remote ones
  print -r -- default
  local f
  for f in $PLIST_DIR/$LABEL_PREFIX.agent-chrome-*.plist(N); do
    print -r -- ${${f:t:r}#$LABEL_PREFIX.agent-chrome-}
  done
  for f in $PLIST_DIR/$LABEL_PREFIX.browser-tunnel-*.plist(N); do
    print -r -- ${${f:t:r}#$LABEL_PREFIX.browser-tunnel-}
  done
}

# A remote profile is one whose Chrome and hub run under another Mac's browser-hub. Here it is
# only an ssh tunnel job, and that job's plist is its whole record: the ssh destination, the
# profile's name over there, the remote hubctl command and the port (see tunnel_plist).
is_remote() { [[ $1 != default && -f $(plist_of $(tunnel_label $1)) ]] }

profile_exists() { [[ $1 == default || -f $(plist_of $(chrome_label $1)) ]] || is_remote $1 }

cdp_port() {  # a remote profile has none here: its Chrome is on the other Mac
  [[ $1 == default ]] && { print -r -- $DEFAULT_CDP_PORT; return }
  local p=$(plist_of $(chrome_label $1)) l
  [[ -f $p ]] || return 1
  l=$(grep -o 'remote-debugging-port=[0-9]*' $p) && print -r -- ${l#*=}
}

hub_port() {  # for a remote profile: the tunnel's port, which is also the hub's port over there
  [[ $1 == default ]] && { print -r -- $DEFAULT_HUB_PORT; return }
  is_remote $1 && { remote_field $1 BH_REMOTE_PORT; return }
  local p=$(plist_of $(hub_label $1))
  [[ -f $p ]] || return 1
  grep -A1 -- '<string>--port</string>' $p | tail -1 | tr -dc 0-9
}

# One of the BH_REMOTE_* values a tunnel plist carries in its EnvironmentVariables.
remote_field() {  # profile key
  local p=$(plist_of $(tunnel_label $1)) v
  [[ -f $p ]] || return 1
  v=$(grep -A1 -- "<key>$2</key>" $p | tail -1)
  [[ $v == *'<string>'*'</string>'* ]] || return 1
  v=${${v#*<string>}%</string>*}
  v=${v//&lt;/<}; v=${v//&gt;/>}; v=${v//&amp;/&}
  print -r -- $v
}

port_up() { nc -z localhost $1 >/dev/null 2>&1 }

# Chrome is stopped once its port is closed AND launchd says its process is gone: it closes the
# DevTools port early in shutdown and writes Cookies and the rest after. A profile copy needs this.
chrome_stopped() { ! port_up $(cdp_port $1) && ! lc_running $(chrome_label $1) }
wait_chrome_stopped() {  # profile seconds
  local i
  for i in {1..$(( $2 * 4 ))}; do chrome_stopped $1 && return 0; sleep 0.25; done
  return 1
}

agents_using() {  # hub-port -> every agent file that points at that hub
  local f
  for f in $AGENTS_DIR/*.md(N); do grep -qF "localhost:$1/mcp" $f && print -r -- $f; done
  return 0
}

next_ports() {  # -> "cdp hub": one above the highest in use by any profile, skipping bound ports
  # Every port a profile is assigned is reserved, whatever its role: a stopped profile's ports
  # aren't bound, and with custom --ports one profile's hub port can sit right above every CDP
  # port. A remote profile's port is in the other Mac's range: it never raises the maximum here
  # (that would walk this Mac into the other's range); it's only reserved.
  local maxc=$DEFAULT_CDP_PORT maxh=$DEFAULT_HUB_PORT n c h
  local -a taken
  for n in $(profiles); do
    if is_remote $n; then taken+=($(hub_port $n)); continue; fi
    if c=$(cdp_port $n); then taken+=($c); (( c > maxc )) && maxc=$c; fi
    if h=$(hub_port $n); then taken+=($h); (( h > maxh )) && maxh=$h; fi
  done
  c=$((maxc + 1)); while port_up $c || (( ${taken[(Ie)$c]} )); do (( c++ )); done
  taken+=($c)   # skipping busy ports can walk the CDP pick up onto the hub's first candidate
  h=$((maxh + 1)); while port_up $h || (( ${taken[(Ie)$h]} )); do (( h++ )); done
  print -r -- $c $h
}

# ------------------------------------------------------------------- launchd --
lc_loaded()  { launchctl print gui/$UID/$1 >/dev/null 2>&1 }
# Capture, then match: `launchctl print | grep -q` under pipe_fail can report a false "no" when
# grep exits early and launchctl dies of SIGPIPE — and a false "not running" would get a live
# Chrome reloaded (killed).
lc_running() { [[ $(launchctl print gui/$UID/$1 2>/dev/null) == *$'\n\tstate = running'* ]] }
lc_pid()     { local o=$(launchctl print gui/$UID/$1 2>/dev/null); o=${${(M)${(f)o}:#$'\tpid = '*}[1]:-}; print -r -- ${o##*= } }

# Bootstrap straight after a bootout can fail with "Input/output error" while launchd is still
# tearing the old instance down, so retry briefly rather than trusting the first answer.
lc_bootstrap() {  # label
  local la=$(la_of $1) i
  [[ $(readlink $la) == $(plist_of $1) ]] || link_plist $1
  for i in {1..20}; do
    launchctl bootstrap gui/$UID $la >/dev/null 2>&1 && return 0
    lc_loaded $1 && return 0
    sleep 0.25
  done
  return 1
}

lc_bootout() {  # label — waits until launchd has really let go of it
  local i
  lc_loaded $1 || return 0
  launchctl bootout gui/$UID/$1 >/dev/null 2>&1
  for i in {1..40}; do lc_loaded $1 || return 0; sleep 0.25; done
  return 1
}

lc_reload() { lc_bootout $1 && lc_bootstrap $1 }  # label — picks up an edited plist

# Start a job whether or not it is loaded. A plain bootstrap of a RunAtLoad=false job loads it
# but does not run it, so the bootstrap fallback needs its own kickstart. `-k` restarts a job
# that is already running.
lc_kick() {  # label [-k]
  launchctl kickstart ${2:-} gui/$UID/$1 >/dev/null 2>&1 && return 0
  lc_bootstrap $1 || return 1
  launchctl kickstart gui/$UID/$1 >/dev/null 2>&1
}

link_plist() {  # label — point the LaunchAgents entry at the generated plist
  local la=$(la_of $1)
  mkdir -p $LAUNCH_AGENTS_DIR
  if [[ -e $la && ! -L $la ]]; then   # a real file we didn't make: keep it, don't clobber it
    mv $la $la.bak-browser-hub-$(date +%Y%m%d-%H%M%S)
    warn "moved aside an existing $la (not a browser-hub symlink)"
  fi
  ln -sfn $(plist_of $1) $la
}

# A plist rewritten while its job is RUNNING cannot take effect without killing the job. Mark it
# instead; `hubctl up` applies the new definition at the job's next cold start, when nothing is
# running to disturb.
mark_stale()  { mkdir -p $RUN_DIR; : > $RUN_DIR/stale-$1 }
is_stale()    { [[ -e $RUN_DIR/stale-$1 ]] }
clear_stale() { rm -f $RUN_DIR/stale-$1 }

# ------------------------------------------------------------------ writers --
xml() { local s=$1; s=${s//&/&amp;}; s=${s//</&lt;}; s=${s//>/&gt;}; print -r -- $s }

plist_strings() { local a; for a in "$@"; do print -r -- "        <string>$(xml $a)</string>"; done }

plist_doc() {  # label log-path program-args... — the common head; callers add keys, then close
  local label=$1 log=$2; shift 2
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Generated by browser-hub from $(xml $BH_ROOT). Do not edit: change the config, run \`hubctl regen\`. -->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$(xml $label)</string>
    <key>ProgramArguments</key>
    <array>
$(plist_strings "$@")
    </array>
    <key>StandardOutPath</key>
    <string>$(xml $log)</string>
    <key>StandardErrorPath</key>
    <string>$(xml $log)</string>
EOF
}

plist_bool() { print -r -- "    <key>$1</key>"; print -r -- "    <$2/>" }
plist_end()  { print -r -- '</dict>'; print -r -- '</plist>' }

chrome_plist() {  # profile — every Chrome is lazy: RunAtLoad and KeepAlive both false
  local n=$1 args
  args=(
    $CHROME
    --user-data-dir=$(profile_dir $n)
    --remote-debugging-port=$(cdp_port_for_write $n)
    # The profile's cookies are encrypted under this mock keychain. Launching the profile
    # without these two flags makes every saved login unreadable.
    --use-mock-keychain --password-store=basic
    --no-first-run --no-default-browser-check
    # Background tabs must keep running: several agents drive this browser at once.
    --disable-background-timer-throttling --disable-backgrounding-occluded-windows
    --disable-renderer-backgrounding
    --disable-session-crashed-bubble --hide-crash-restore-bubble
    # Agent browsers never use Chrome's built-in AI, and it otherwise pulls Gemini Nano (~4 GB,
    # OptGuideOnDeviceModel/) into every profile. A flag, not the GenAILocalFoundationalModelSettings
    # policy: policy on macOS is machine-wide managed prefs and would also hit the user's own Chrome.
    --disable-features=OptimizationGuideModelExecution,OnDeviceModelBackgroundDownload,OnDeviceModelPerformanceParams,AIPromptAPI,AIRewriterAPI,AISummarizationAPI,AIWriterAPI
  )
  (( DISK_CACHE_BYTES > 0 )) && args+=(--disk-cache-size=$DISK_CACHE_BYTES)
  # No startup window, no startup tab. On macOS a headed Chrome keeps running with no windows, so
  # nothing has to hold one open, and agents' tabs each open a window of their own (focus guard).
  args+=(--no-startup-window $CHROME_EXTRA_ARGS)
  plist_doc $(chrome_label $n) $LOG_DIR/chrome-$n.log $args
  plist_bool RunAtLoad false
  plist_bool KeepAlive false
  print -r -- '    <key>ProcessType</key>'
  print -r -- '    <string>Interactive</string>'
  plist_end
}

pw_mcp_at_least() {  # version — is the pinned playwright-mcp at least this?
  # No version or `latest` counts as new. A range or any other tag counts as old: a wrong "old"
  # only loses a flag, while a wrong "new" stops the always-on hub from starting.
  local v=${PW_MCP#@}
  [[ $v == *@* ]] || return 0
  v=${${v##*@}%%-*}
  [[ $v == latest ]] && return 0
  [[ $v == <->.<->.<-> ]] || return 1
  autoload -Uz is-at-least
  is-at-least $1 $v
}

hub_plist() {  # profile — the default hub is always on; every other hub lives and dies with its Chrome
  local n=$1 on=false
  always_on $n && on=true
  local args=() cdp=http://localhost:$(cdp_port_for_write $n)
  # FOCUS_GUARD: relay CDP through lib/focus-guard.mjs, which opens agents' tabs in the background
  # and drops their bring-to-front calls, so browsing never raises Chrome over what the person is
  # typing in. It runs the hub as its child, picks its own loopback port, and stands in for "GUARD".
  if (( FOCUS_GUARD )); then
    args=($NODE_BIN/node $BH_ROOT/lib/focus-guard.mjs $(cdp_port_for_write $n) --)
    cdp=GUARD
  fi
  args+=(
    $NODE_BIN/npx -y $PW_MCP
    --port $(hub_port_for_write $n)
    --cdp-endpoint $cdp
    # Built for many clients on one browser: each client keeps its own current tab.
    --shared-browser-context
    --output-dir $OUT_DIR/$n
  )
  (( ACTION_TIMEOUT_MS > 0 )) && args+=(--timeout-action $ACTION_TIMEOUT_MS)
  # With `none`, clicks and navigations stop writing a full page snapshot to disk and stop
  # repeating every open tab in each response. The tab list still appears whenever a tab's
  # URL/title or this client's current tab changes, which is the signal the agent rules rely on.
  # playwright-mcp exits on an unknown option, which would take down the always-on hub, so every
  # flag newer than 1.0's 0.0.78 pin follows the pinned version.
  [[ -n $SNAPSHOT_MODE ]] && pw_mcp_at_least 0.0.78 && args+=(--snapshot-mode $SNAPSHOT_MODE)
  if pw_mcp_at_least 0.0.85; then
    args+=(
      --file-paths absolute   # links to saved files resolve from any session's cwd
      --no-webmcp             # don't turn tools that a page registers into tools agents can call
    )
  fi
  plist_doc $(hub_label $n) $LOG_DIR/hub-$n.log $args
  plist_bool RunAtLoad $on
  plist_bool KeepAlive $on
  cat <<EOF
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(xml $NODE_BIN):/usr/local/bin:/usr/bin:/bin</string>
    </dict>
EOF
  plist_end
}

reaper_plist() {  # a one-shot on a timer
  plist_doc $REAPER_LABEL $LOG_DIR/reaper.log /bin/zsh $HUBCTL reap
  plist_bool RunAtLoad false
  cat <<EOF
    <key>StartInterval</key>
    <integer>$(( REAP_INTERVAL_MINUTES * 60 ))</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>BROWSER_HUB_CONFIG</key>
        <string>$(xml $BH_CONFIG)</string>
    </dict>
EOF
  plist_end
}

# ------------------------------------------------------------ remote profiles --
# ssh options for everything that talks to the other Mac. BatchMode: never stop to ask for a
# password, because nobody is there to type it (launchd, the hook). ServerAlive: a laptop that
# sleeps or leaves the network is noticed in ~15 s, instead of hanging every call on a dead link.
ssh_opts() {
  print -l -- -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=5 -o ServerAliveCountMax=3
}

ssh_sock() { print -r -- $RUN_DIR/ssh-$1.sock }   # profile -> its tunnel's ControlMaster socket

# The tunnel job: one ssh process per remote profile. It forwards the hub's port, and it is the
# ControlMaster that every other ssh call for that profile rides on (40 ms a command, instead of
# a fresh handshake). Lazy, like every Chrome: `hubctl up` starts it and the reaper stops it.
#
# The port is the SAME on both ends. playwright-mcp only answers requests whose Host header is
# localhost:<its own port> (DNS-rebinding protection), so a tunnel that moved the port would get a
# 403 on every call. That's why each Mac keeps its own port range (DEFAULT_*_PORT), and why a
# remote profile's hub port must be free here.
tunnel_plist() {  # profile — reads its own record back, or NEW_* while `add` writes it
  local n=$1 dest port rp rh
  dest=${NEW_REMOTE_SSH:-$(remote_field $n BH_REMOTE_SSH)}
  port=${NEW_HUB:-$(remote_field $n BH_REMOTE_PORT)}
  rp=${NEW_REMOTE_PROFILE:-$(remote_field $n BH_REMOTE_PROFILE)}
  rh=${NEW_REMOTE_HUBCTL:-$(remote_field $n BH_REMOTE_HUBCTL)}
  [[ -n $dest && $port == <-> && -n $rp && -n $rh ]] || die "remote profile '$n' has an incomplete record"
  local sock=$(ssh_sock $n)
  # macOS can't bind a unix socket path longer than 104 bytes, and ssh would fail at every start.
  (( ${#sock} <= 100 )) || die "the ssh socket path is too long for macOS ($sock); use a shorter STATE_DIR"
  # The forward is bound to loopback by name: with no bind address ssh follows GatewayPorts, and
  # `GatewayPorts yes` in an ssh config would serve this unauthenticated hub to the whole network.
  # "localhost" (not 127.0.0.1) keeps both ::1 and 127.0.0.1, which clients of localhost:<port> try.
  plist_doc $(tunnel_label $n) $LOG_DIR/tunnel-$n.log $SSH_BIN -N -M -S $sock \
    -o ControlPersist=no -o ExitOnForwardFailure=yes -o GatewayPorts=no $(ssh_opts) \
    -L localhost:${port}:localhost:${port} -- $dest
  plist_bool RunAtLoad false
  plist_bool KeepAlive false
  cat <<EOF
    <key>EnvironmentVariables</key>
    <dict>
        <key>BH_REMOTE_SSH</key>
        <string>$(xml $dest)</string>
        <key>BH_REMOTE_PROFILE</key>
        <string>$(xml $rp)</string>
        <key>BH_REMOTE_HUBCTL</key>
        <string>$(xml $rh)</string>
        <key>BH_REMOTE_PORT</key>
        <string>$port</string>
    </dict>
EOF
  plist_end
}

# Run hubctl on the other Mac, over the tunnel's master connection when it's up and a fresh
# connection when it isn't. The arguments are quoted for the remote shell. The remote hubctl
# command is used as written, so it may carry a BROWSER_HUB_CONFIG=… prefix.
remote_hubctl() {  # profile hubctl-args...
  local n=$1; shift
  local dest=$(remote_field $n BH_REMOTE_SSH) rh=$(remote_field $n BH_REMOTE_HUBCTL)
  [[ -n $dest && -n $rh ]] || { print -u2 -r -- "hubctl: remote profile '$n' has an incomplete record"; return 1 }
  $SSH_BIN -S $(ssh_sock $n) -o ControlMaster=no $(ssh_opts) -- $dest "$rh ${(j: :)${(q)@}}"
}

# What a profile copy (`hubctl push` / `pull`) leaves out: host-specific locks and ports, crash
# and metrics files, caches Chrome rebuilds on its own, Chrome's on-device AI model (~4 GB), and
# the native-messaging links, which `hubctl up` makes on each Mac. About 1.1 GB becomes ~100 MB.
# rsync never deletes an excluded path at the destination either, so each side keeps its own.
typeset -ga COPY_EXCLUDES
COPY_EXCLUDES=(
  'Singleton*' DevToolsActivePort RunningChromeVersion .DS_Store
  'Crashpad*' '*.pma' BrowserMetrics/
  Cache/ 'Code Cache/' GPUCache/ GPUPersistentCache/ DawnCache/ DawnGraphiteCache/
  DawnWebGPUCache/ GraphiteDawnCache/ GrShaderCache/ ShaderCache/ 'Shared Dictionary/'
  'Service Worker/CacheStorage/' 'Service Worker/ScriptCache/'
  component_crx_cache/ extensions_crx_cache/
  OptGuideOnDeviceModel/ optimization_guide_model_store/
  NativeMessagingHosts/ 'browser-hub-cookies.json.tmp-*'
)

# Port lookups for the writers. A new profile has no plist to read ports from yet, so `add`
# exports NEW_CDP/NEW_HUB for the duration of the write.
cdp_port_for_write() { [[ -n ${NEW_CDP:-} ]] && print -r -- $NEW_CDP || cdp_port $1 }
hub_port_for_write() { [[ -n ${NEW_HUB:-} ]] && print -r -- $NEW_HUB || hub_port $1 }

render() {  # template-file key=value... — replaces {{KEY}} with value
  local t=$(<$1) kv; shift
  for kv in "$@"; do t=${t//\{\{${kv%%=*}\}\}/${kv#*=}}; done
  print -r -- $t
}


# Write a job's plist only if its content changed. Returns 0 if it changed, 1 if not.
put_plist() {  # label content
  local p=$(plist_of $1)
  # A writer that died inside $(...) hands over nothing, and plutil -lint passes an empty file.
  [[ $2 == *'<plist'*'</plist>'* ]] || die "no plist generated for $1"
  mkdir -p $PLIST_DIR $LOG_DIR
  if [[ -f $p ]] && [[ "$(<$p)" == "$2" ]]; then return 1; fi
  print -r -- $2 > $p.tmp
  plutil -lint -s $p.tmp >/dev/null || { rm -f $p.tmp; die "generated an invalid plist for $1" }
  mv $p.tmp $p
  return 0
}
