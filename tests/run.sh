#!/bin/zsh
# tests/run.sh — unit tests (sandboxed, ~30 s). Add --live for the real launchd/Chrome/hub tests
# (~1 min; fully isolated from any hub already running on this machine).
cd ${0:A:h:h}
[[ ${1:-} == --live ]] && export BROWSER_HUB_LIVE=1
# The focus-guard tests borrow playwright-core from the npx cache that the pinned playwright-mcp
# fills. A fresh clone before ./install.sh has none, and they would skip without saying why.
if [[ -z $(print -l ~/.npm/_npx/*/node_modules/playwright-core(N/)) ]]; then
  pw=$(sed -n 's/^PW_MCP=//p' config.example)
  print -u2 "note: no playwright-core in the npx cache, so the focus-guard tests will be skipped."
  print -u2 "      Fetch it first (network): npx -y ${pw:-@playwright/mcp} --version"
fi
exec python3 -m unittest discover -s tests -p 'test_*.py' -v
