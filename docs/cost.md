# Cost and context

A wake-up can be a full model turn. A long-running session carries its
conversation and tool output into that turn, even when the new message is
short. Prompt caching can reduce the cost of processing a repeated prefix.
When that prefix changes or the cache expires during a break, the next
request may reprocess the full context. Have the coordinator wake agents
for actionable work and keep their briefs short. Use a fresh session for
an unrelated task once the durable decisions and evidence are recorded.
[Claude Code cost guidance](https://code.claude.com/docs/en/costs#why-usage-climbs-in-a-long-session)
and [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
explain the underlying behavior.

## Claude Code controls

- Use `/context` to inspect the context budget and `/compact` with a narrow
  instruction when you need continuity. For `/autocompact`, check
  `claude --version` and type `/autocompact` in the command menu to confirm
  that your installed client offers the control. When supported, the command
  displays or sets the auto-compaction window. If unavailable, use the
  client's built-in automatic compaction or `/compact`.
  [Command reference](https://code.claude.com/docs/en/commands)
- Check `/effort status` and choose an effort setting supported by your model.
  Set a lower effort for straightforward work and raise it for tasks that
  benefit from deeper reasoning. Effort is a model control, and its exact
  effect depends on the model.
  [Effort settings](https://code.claude.com/docs/en/model-config#adjust-effort-level)
- Disable unused MCP servers in the project's `/mcp` panel to reduce the tool
  inventory and background connections. Enable the ones the job needs.
  [Managing MCP servers](https://code.claude.com/docs/en/mcp#disable-a-server-without-removing-it)
- Pin the subagent's `model` to one that fits the task. Inheriting the main
  session model can give a routine worker the same model you reserved for
  complex decisions. Independent agents bring their own context and usage.
  [Subagent model selection](https://code.claude.com/docs/en/sub-agents#choose-a-model)
- Check `/usage` for your plan's usage data or the session cost estimate.
  Compare representative runs with similar inputs before changing the
  default model, effort, or delegation policy. Use the relevant account
  reporting surface for billed totals.
  [Tracking Claude Code costs](https://code.claude.com/docs/en/costs#track-your-costs)
