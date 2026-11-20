# Persona review

Use this workflow when a person needs to choose between plausible designs.
Write a brief with the goal, constraints, audience, and the concrete output
each alternative should provide.

Save a private persona profile under the kit's XDG configuration
`profiles/` directory. Each command receives a UTF-8 prompt on stdin,
runs in a fresh Bubblewrap sandbox, and returns its artifact on stdout.
Use approved wrappers mounted under `/tools`. For model calls use a scoped
broker and add its socket to `[execution.sockets]` under `persona`. See the
[loop's execution setup](../../docs/loop.md).

```toml
[alternatives]
compact = ["/tools/persona-agent", "compact"]
guided = ["/tools/persona-agent", "guided"]

[personas]
compact = "A returning operator checking status between other tasks"
guided = "A first-time contributor completing setup without assistance"

[review]
use = ["/tools/persona-agent", "use"]
adversarial = ["/tools/persona-agent", "adversarial"]
synthesis = ["/tools/persona-agent", "synthesis"]

[execution.tools]
persona-agent = "/home/example/kit-tools/persona-agent"
```

```sh
kevin-agent-kit persona-review \
  --config "${XDG_CONFIG_HOME:-$HOME/.config}/kevin-agent-kit/profiles/personas.toml" \
  --brief ./brief.md --output ./persona-evidence
```

Each of the two or more alternatives receives the brief independently.
The command stores their concrete artifacts under `alternatives/`. Each persona trial
gets its own artifact as `artifact.md` in a fresh work directory and carries out a task.
For a runnable prototype, configure a trusted trial command with the permissions and
tools needed to run it. For a document or workflow, use representative input and record the
result. The adversarial reviewer checks artifacts and trial results for failures.
The synthesis process receives all of that evidence and returns a JSON
object with two strings, `synthesis` and `question`.

The command saves `use.md`, `adversarial.md`, `synthesis.md`, and
`question.md` in the new output directory. Read the evidence and present
the synthesized trade-offs with the decision question. The human's answer
determines which design moves forward. If a stage fails, its predecessors'
evidence remains available and the command exits with an error.
