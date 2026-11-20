# kevin-agent-kit

A local harness that coordinates coding agents from implementation through review and integration.

> one human holds the decision gate; an agent fleet does the rest

[한국어](docs/ko/README.md) · [Repository](https://github.com/JDeun/kevin-agent-kit.git)

People set direction, choose among reviewed designs, and authorize shipping and
cleanup. The crew organizes the work, reviews the proposed changes, repairs
issues in isolated worktrees, and gathers verification evidence.

## Try the dry run

Install Git and Python 3.11 or later, then run the demo from the source checkout.

```sh
python3 -m core.loop demo
```

The demo uses a temporary Git repository and local stand-ins for the review
and worker commands. It needs no model credentials or hosting account.

Build and install from the checkout to use the standalone command.

```sh
./scripts/build.sh
./scripts/install.sh
kevin-agent-kit demo
```

Load the rendered Claude Code plugin with
`claude --plugin-dir ./dist/kevin-agent-kit/plugin`. Add the installed command's
directory to `PATH` before invoking the plugin.
Use `./scripts/rename.sh NEW` before building to change `[project].name` in
`project.toml`. The build uses that name for the CLI and rendered documentation.
It replaces hyphens with underscores for the Python package and Claude Code
command namespace. With the build directory on `PYTHONPATH`, run the package
as `python3 -m kevin_agent_kit`.

## Run it on a repository

Set the repository, target branch, and reviewer, worker, and verification
commands in your project's [TOML configuration](docs/loop.md). Start the
loop, enqueue the local branch and exact commit to review, then check its
state.

```sh
kevin-agent-kit --config ./project.toml start
kevin-agent-kit --config ./project.toml enqueue feature/example "$(git rev-parse feature/example)"
kevin-agent-kit --config ./project.toml status
```

When finished, stop the loop with `kevin-agent-kit --config ./project.toml stop`.

Use `python3 -m core.loop` in place of the installed command from the source
checkout. See the [architecture](docs/architecture.md) for the loop and
decision points, and the [hosting adapters](docs/hosting.md) for GitHub and
GitLab integration.

- [Worker rules](agents/worker-rules.md) cover branch scope and resource ownership.
- [Persona review](reviews/persona/README.md) produces alternative designs and a decision brief.
- [Screenshot verification](verify/README.md) supplies running-app evidence for a PR or MR.
- [Guards](docs/guards.md) and [ledgers](docs/ledgers.md) describe the available checks and records.
- [Cost and context](docs/cost.md) covers agent model choice and session usage.

## Inspiration

| Source | Idea we took |
| --- | --- |
| [Lauren Tan, All Code Is Slop (Except Mine), AI Engineer](https://ai.engineer/talks/O_zcc6Xl12g-all-code-is-slop-except-mine) | Encode repeatable review rules in checks and collect evidence from the running app. |
| [pstack in cursor/plugins](https://github.com/cursor/plugins/blob/main/pstack/README.md) | Prepare independent design alternatives and challenge them before asking the human to choose. |
| [ECC](https://github.com/affaan-m/ECC) | Package reusable agent rules and tools alongside the workflow. |
| [AgentShield](https://github.com/affaan-m/agentshield) | Combine deterministic checks with independent perspectives and a synthesis of concrete findings. |

The implementation and documentation here were written from scratch. The
sources above informed the design.
