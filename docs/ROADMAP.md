# Roadmap: ideas that aren't built yet

In order of payoff for the effort. None of them is needed for browser-hub to work.

## Make repeat browsing cheaper

A subagent that checks one price on five websites can burn 30-40k tokens per lookup: a fresh
context, a snapshot or `find` per step, and prose reasoning between calls. The rest
of the field converges on one answer: **use the LLM once, replay deterministically, and bring the
LLM back only when the replay fails.**

1. **Compiled extractors.** *Built as `hubctl run` (DESIGN §10).* What's left is more sites,
   and a scheduled check of each extractor against known products to catch silent drift.
   Original note: Turn recurring page reads (a product price, a dashboard number) into
   small JS functions in a library, e.g. `extractors/<site>.js` exporting `lookup(page, id) →
   JSON`. Each owns its URL builder and its "blocked / logged out" check. Run them from one
   `hubctl run <profile> <extractor> <args>` command over CDP. Result: JSON in seconds, zero model
   tokens, and a structured `blocked: login` instead of 90 tool calls of probing. When a selector
   rots, a person or an agent fixes that one function.
2. **Code mode inside agents.** On a known page, one `browser_run_code_unsafe` call that
   navigates, waits and extracts replaces a navigate / snapshot / click / snapshot loop. The agent
   template now suggests it. What's left is measuring how often agents actually do it.
3. **A CLI lane for one-off lookups.** Lower priority since `--snapshot-mode none`: the CLI's main
   saving, keeping snapshots out of the conversation, is now the hub's default. `playwright-cli` shares playwright-mcp's tool code behind a
   shell. Output can be filtered (`| head`) before it reaches the model, and `playwright-cli show`
   gives a live view of sessions and tabs. A `hubctl cli <profile> -- …` wrapper would start the
   profile, stamp it, and keep the CLI's state files out of whatever repo you're in.
4. **Retry through a script, not the agent.** Resuming an agent to retry a step it was blocked on
   carries its whole transcript forward. Send "blocked" retries to (1) or (3) instead.

## Visibility

Tabs have no stable IDs, and from outside you can't tell which agent owns which tab. A cheap fix is
for each agent to prefix its tabs' titles (`document.title = '[L2] ' + document.title`, reapplied
after each navigation), so a person looking at Chrome can see who's who.

## Per-client isolation, for big fan-outs

`--isolated` plus a fresh browser context per client, seeded from a periodically exported
`--storage-state`, would remove the shared-tab-list hazard completely. The costs: logins go stale
between exports, and many contexts replaying one session may trip a site's bot defenses. Worth a
prototype only if lanes stop being enough.
