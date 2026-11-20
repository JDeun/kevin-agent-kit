# kevin-agent-kit for Claude Code

Install the kit CLI on `PATH`, then add and install the plugin in **User** scope.

```text
/plugin marketplace add JDeun/kevin-agent-kit
/plugin install kevin-agent-kit@kevin-agent-kit
```

For a local checkout, use its absolute path when adding the marketplace or
load the plugin with `claude --plugin-dir ./plugin`.

Use `/kevin-agent-kit:run-dispatch` for a source branch and exact commit,
and `/kevin-agent-kit:run-status` to read progress. The decide skill asks
the maintainer to choose what happens to unresolved findings. The persona
review skill gathers design evidence before asking for a choice.
