# Persona review

Use this workflow when a person needs to choose between plausible designs.
Write a brief with the goal, constraints, audience, and the concrete output
each alternative should provide.

Configure independently invocable agent commands as argv arrays. Every
invocation receives a UTF-8 prompt on stdin, runs with a fresh temporary
working directory, and returns the artifact on stdout. Use commands on
`PATH` or absolute paths. Run trusted commands with the runtime's own
permissions and credentials. The working directory separates the files
created by each invocation.

```toml
[alternatives]
compact = ["claude", "-p", "--model", "sonnet", "--tools", ""]
guided = ["claude", "-p", "--model", "opus", "--tools", ""]

[personas]
compact = "A returning operator checking status between other tasks"
guided = "A first-time contributor completing setup without assistance"

[review]
use = ["claude", "-p", "--model", "sonnet", "--tools", ""]
adversarial = ["claude", "-p", "--model", "opus", "--tools", ""]
synthesis = ["claude", "-p", "--model", "sonnet", "--tools", ""]
```

```sh
python3 -m reviews.persona --config ./personas.toml \
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
