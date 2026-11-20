# Run the review queue

The coordinator uses Git and Python 3.11+ in a POSIX environment. It runs as a detached
local process and periodically ingests complete lines from
`requests.log`, using `NEED-REVIEW BRANCH FULL_SHA`. Duplicate lines resolve to the same
branch-and-commit job. Use a trusted, private state directory outside the checkouts that the
workers edit.

```sh
kevin-agent-kit --config queue.toml start
kevin-agent-kit --config queue.toml enqueue topic/example "$(git rev-parse topic/example)"
kevin-agent-kit --config queue.toml status
kevin-agent-kit --config queue.toml stop
```

The equivalent source command is `python3 -m core.loop`. Call `run` for a foreground process
under your own supervisor. Detached logs and the SQLite ledger stay inside the configured state
directory. `stop` signals the instance over its private Unix socket. The coordinator cleans up
only the process groups it creates. Tasks should avoid detaching servers into new process groups.

## Configuration

Save trusted deployment overrides as a TOML file. Paths in `repo` and `state` are relative to
the override file. The bundled [project file](../project.toml) lists agent and queue defaults.

```toml
[loop]
repo = "./checkout"
state = "./queue-state"
target = "main"
verify = ["python3", "-m", "pytest", "-n", "2"]

[loop.memory.review]
max_anon_mib = 4096
min_available_mib = 512

[loop.memory.test]
max_anon_mib = 3072
min_available_mib = 1024
```

Reviewer and worker commands are argv arrays. Configure the reviewer for a model different
from the work agent when available. List approved model IDs in ascending capability order
under `models`, set the role IDs with `reviewer_model` and `worker_model`, and set `max_model`
to the highest allowed entry. The coordinator substitutes an argv element exactly equal to
`{model}` and passes the selected value as `AGENT_MODEL`. Wrappers must honor that selection.

A reviewer receives a lens prompt on stdin while checked out at the source commit. Its stdout
must be one JSON object containing `findings`, an array of `{"risk":"other","detail":"…"}`
objects. The allowed risks are `safety`, `honesty`, `security`, and `other`. An optional
`count` must match the array length. Security and database path globs add review lenses.
A repair agent receives the observations and worker rules on stdin and must commit its
changes in its detached checkout. Source branch updates require the old source SHA to match.

The review and test memory profiles use cgroup v2 anonymous bytes when an anonymous ceiling
is set and fall back to the configured minimum `MemAvailable` if cgroup data is absent.
Set both limits to zero to disable that profile on systems without either source.
The concurrency ceiling applies across all
stages. Capacity errors and exit code 75 use capped exponential backoff. Exhausted retries,
malformed reviewer output, merge conflicts and verification failures appear as blocked jobs.

## Human decisions

Safety, honesty and security findings keep the job in repair even at the normal round cap.
When only other findings remain at the cap, the coordinator writes an accept record.
`accept-record ID` shows the exact head and evidence. The human can choose `revise` to
start a fresh repair budget or `defer` to leave the source for later work.

```sh
kevin-agent-kit --config queue.toml decide 1 revise --source 'Maintainer review'
```

## Merge train

Only clean reviewed heads enter the train. Keep the target and any branch receiving repair
commits free of active Git checkouts. The train creates a temporary worktree at the current
target, merges the reviewed source, and runs the verifier in that candidate worktree. A
verifier can run local tests or wait for a CI run that is bound to `AGENT_CANDIDATE`.
For hosted CI, a trusted wrapper should submit or identify that exact candidate, require its
successful checks, and return exit 75 when temporarily unavailable. The hosting adapters can
read provider status. See [hosting operations](hosting.md).

The verifier also gets `AGENT_HEAD`, `AGENT_BASE`, `AGENT_BRANCH` and `AGENT_TARGET`. The
train serializes candidate checks and advances the local target in one Git ref transaction
that checks both source and target. A new source head goes back through review. A new
target forces the train to build and verify another candidate. Publish the local target
through your repository's own workflow.

[Codex CLI structured review example](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex)
