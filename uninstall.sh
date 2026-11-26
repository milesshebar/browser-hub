#!/bin/zsh
# uninstall.sh — take browser-hub off this Mac.
#
# Stops and unloads every job, and removes the hooks, the MCP registration and the CLAUDE.md block
# that install.sh added. Keeps what's yours: browser profile data (logins), agent files (unless
# --remove-agents), the config file and the logs. It prints where each of those lives.
#
#   ./uninstall.sh [--remove-agents] [--no-mcp]
#   --no-mcp leaves the user-scope `playwright` MCP registration alone (pairs with install.sh --no-mcp)
setopt no_unset pipe_fail extended_glob
ROOT=${0:A:h}
remove_agents=0 do_mcp=1
for a in "$@"; do
  case $a in
    --remove-agents) remove_agents=1 ;;
    --no-mcp)        do_mcp=0 ;;
    -h|--help)       awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' $0; exit 0 ;;
    *)               print -u2 "uninstall.sh: unknown option $a"; exit 2 ;;
  esac
done
source $ROOT/lib/common.zsh

print -r -- "==> launchd jobs"
typeset -a dirs agents
for n in $(profiles); do
  if is_remote $n; then             # only a tunnel here; nothing on the other Mac is touched
    for a in ${(f)"$(agents_using $(hub_port $n))"}; do
      [[ ${a:t} == browser-$n(|-<->).md ]] && agents+=($a)
    done
    l=$(tunnel_label $n)
    lc_bootout $l
    [[ -L $(la_of $l) ]] && rm -f $(la_of $l)
    rm -f $(plist_of $l)
    print -r -- "  removed $n (remote: its tunnel)"
    continue
  fi
  [[ -d $(profile_dir $n) ]] && dirs+=($(profile_dir $n))
  # Only this install's agents: named browser-<profile>[-N] and pointing at that profile's hub.
  for a in ${(f)"$(agents_using $(hub_port $n))"}; do
    [[ ${a:t} == browser-$n(|-<->).md ]] && agents+=($a)
  done
  for l in $(hub_label $n) $(chrome_label $n); do
    lc_bootout $l
    [[ -L $(la_of $l) ]] && rm -f $(la_of $l)
    rm -f $(plist_of $l)
  done
  print -r -- "  removed $n"
done
lc_bootout $REAPER_LABEL; [[ -L $(la_of $REAPER_LABEL) ]] && rm -f $(la_of $REAPER_LABEL)
rm -f $(plist_of $REAPER_LABEL)
rm -rf $RUN_DIR
print -r -- "  removed the idle reaper"

print -r -- "==> Claude Code"
claude_config hooks-remove $CLAUDE_SETTINGS
claude_config md-remove $CLAUDE_MD
claude=${CLAUDE_BIN:-claude}
if (( do_mcp )) && { (( $+commands[$claude] )) || [[ -x $claude ]] }; then
  if [[ $($claude mcp get playwright 2>&1) == *"localhost:$DEFAULT_HUB_PORT/mcp"* ]]; then
    $claude mcp remove playwright -s user >/dev/null 2>&1 &&
      print -r -- "  removed MCP server 'playwright'" ||
      print -r -- "  could not remove MCP server 'playwright': claude mcp remove playwright"
  fi
fi

agents=(${(u)agents})
if (( $#agents )); then
  if (( remove_agents )); then
    rm -f $agents; print -r -- "  removed agents: ${(j:, :)${agents[@]:t}}"
  else
    print -r -- "  kept agents (./uninstall.sh --remove-agents deletes them): ${(j:, :)${agents[@]:t}}"
  fi
fi

cat <<EOF

browser-hub is uninstalled. Still on disk, on purpose:
  browser profiles (your logins):  ${(j:, :)dirs:-none were created}
  config:                          $BH_CONFIG
  logs and screenshots:            $STATE_DIR
Delete them yourself once you're sure you don't need them.
EOF
