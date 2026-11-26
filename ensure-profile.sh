#!/bin/zsh
# PreToolUse hook: start the browser profile that a tool call or a subagent is about to need.
#
# install.sh registers it in ~/.claude/settings.json under two matchers:
#   Agent                          a subagent is about to spawn. Its MCP connection is made at
#                                  spawn time, before it can run anything, so a browser agent whose
#                                  Chrome is cold would start with dead tools and no way to fix it.
#   mcp__playwright__.*|mcp__browser-.*
#                                  a browser tool is about to run: in the main session (`playwright`
#                                  is the default profile) or inside a browser-* agent, whose MCP
#                                  server is named after the agent. This is what restarts a profile
#                                  whose Chrome was quit or reaped mid-conversation.
#
# Resolves a profile from the hook JSON on stdin and runs `hubctl up <profile>`: ~60 ms when the
# profile is already up, a 2-6 s cold start when it isn't. Always exits 0 — a hook must never block
# the call it guards. A real failure goes to stderr, where it explains the missing tools.
setopt no_unset extended_glob

HUB=${0:A:h}
input=$(cat)

# Two fields matter, and both are simple strings, so a regex on the raw JSON is enough and keeps
# python's startup off a path that runs before every browser tool call. (Inside a JSON string a
# quote is always escaped, so `"tool_name"` cannot match text inside a prompt.)
tool= agent= profile= origin=
[[ $input =~ '"tool_name"[[:space:]]*:[[:space:]]*"([^"]*)"' ]] && tool=$match[1]
case $tool in
  mcp__playwright__*) agent=main-session profile=default origin=tool ;;
  mcp__browser-*__*)  agent=${${tool#mcp__}%%__*} origin=tool ;;     # the server name is the agent name
  Agent|Task)
    [[ $input =~ '"subagent_type"[[:space:]]*:[[:space:]]*"([^"]*)"' ]] && agent=$match[1]
    origin=agent ;;
  *) exit 0 ;;
esac

if [[ -z $profile ]]; then
  [[ -n $agent ]] || exit 0
  source $HUB/lib/common.zsh
  base=${agent%%-<->}                                    # lanes share their base agent's profile
  profile=${AGENT_PROFILES[$agent]:-${AGENT_PROFILES[$base]:-}}   # agents mapped in the config
  if [[ -z $profile ]]; then
    [[ $base == browser-?* ]] || exit 0                  # not a browser agent: nothing to do
    profile=${base#browser-}
  fi
fi

out=$($HUB/hubctl up $profile 2>&1)
rc=$?

# A tool from some other MCP server that merely happens to be named browser-* is not ours.
(( rc == 3 )) && [[ $origin == tool ]] && exit 0

# Log cold starts and failures only. A warm no-op happens on every browser call and would bury
# the log; `hubctl up` has already stamped the profile as in use either way.
if [[ $out != *"already up"* ]]; then
  (( $+LOG_DIR )) || source $HUB/lib/common.zsh
  mkdir -p $LOG_DIR
  print -r -- "$(date '+%Y-%m-%d %H:%M:%S') $origin=$agent profile=$profile rc=$rc :: ${out//$'\n'/ | }" \
    >> $LOG_DIR/ensure-profile.log
fi
(( rc == 0 )) || print -u2 -r -- "browser-hub: could not start profile '$profile' — $out"
exit 0
