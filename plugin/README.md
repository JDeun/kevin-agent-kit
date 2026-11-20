# kevin-agent-kit for Claude Code

Queue committed work for independent review and keep human decisions in the main session.
Install the kit command on `PATH` and start the coordinator with your repository's TOML
configuration. Load this rendered directory with `claude --plugin-dir`.

Use `/kevin_agent_kit:run-dispatch` for a source branch and commit, and
`/kevin_agent_kit:run-status` to read progress. The decide skill asks the maintainer to choose
what happens to unresolved findings. The persona-review skill gathers design evidence before
asking for a choice.
