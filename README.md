# kevin-agent-kit

A local harness that coordinates coding agents from implementation through review and integration.

> one human holds the decision gate; an agent fleet does the rest

[한국어](docs/ko/README.md) · [Repository](https://github.com/JDeun/kevin-agent-kit.git)

People set direction, choose among reviewed designs, and authorize shipping and
cleanup. The crew organizes the work, reviews the proposed changes, repairs
issues in isolated worktrees, and gathers verification evidence.

## Start on a new computer

### 1. Install the CLI and user rules

Install Git and Python 3.11 or later. The live loop also needs OpenSSL and
Linux with working unprivileged Bubblewrap namespaces. Use a trusted checkout of this
kit. The installer defaults to `~/.local`.

```sh
./scripts/install.sh
export PATH="$HOME/.local/bin:$PATH"
kevin-agent-kit --version
kevin-agent-kit demo
kevin-agent-kit init --global --dry-run
kevin-agent-kit init --global
```

Persist the `PATH` setting in the shell that launches Claude Code. The demo
uses bundled fixtures and a temporary Git repository. The global installer
copies the installed rule text into a managed block in `~/.claude/CLAUDE.md`
and `~/.codex/AGENTS.md`. Each block records the kit version and content hash.
Rerun the installer to update the snapshot when you approve a new version.
The home and client paths must be owned by the current user, with no symlinks
or group or other write access. Updating existing files requires Linux
filesystem support for atomic exchange and metadata preservation.
It backs up each existing file it changes under the kit's XDG state directory.
Start a new session to load the rules.

In Claude Code, add the marketplace and select **User** scope when installing.

```text
/plugin marketplace add JDeun/kevin-agent-kit
/plugin install kevin-agent-kit@kevin-agent-kit
```

For a local checkout, use its absolute path in the marketplace add command.
You can also load its plugin directly with `claude --plugin-dir ./plugin`.
See the [Claude Code marketplace guide](https://code.claude.com/docs/en/plugin-marketplaces).

### 2. Draft a repository profile

In the repository you want the loop to review, run `kevin-agent-kit init`.
Open the printed TOML path and review the detected target and test command
suggestions. Configure dedicated review adapters and signed evidence sources
for independent review and required checks.
Repair is optional and needs both a worker command and an approved path scope.
The profile lives under `${XDG_CONFIG_HOME:-$HOME/.config}/kevin-agent-kit/profiles/`.
See [profile and wrapper setup](docs/loop.md).

### 3. Run the loop

Use a local source branch with a committed change. For another profile, pass
`--config /absolute/path/to/profile.toml` before the subcommand.

```sh
kevin-agent-kit start
kevin-agent-kit enqueue feature/example "$(git rev-parse refs/heads/feature/example)"
kevin-agent-kit status
```

Look for a `candidate` job ID. After reviewing the evidence,
`kevin-agent-kit candidate JOB_ID --output /absolute/path/candidate.bundle`
exports the verified commit. Apply it through your repository's normal
approval and Git workflow. State, queue, and logs live under
`${XDG_STATE_HOME:-$HOME/.local/state}/kevin-agent-kit/`.

### 4. Stop the loop or remove the global rules

```sh
kevin-agent-kit stop
kevin-agent-kit init --global --uninstall --dry-run
kevin-agent-kit init --global --uninstall
```

The uninstall command removes the managed blocks. To remove the Claude Code
plugin, use `/plugin uninstall kevin-agent-kit@kevin-agent-kit`.

From the kit checkout, use `python3 -m core.loop` in place of the installed
command. Use `./scripts/build.sh` to produce a standalone artifact.
The build reads tracked paths from the Git index and bytes from the checkout.
Build releases from a clean checkout and scan the generated directory with
the source.
`./scripts/rename.sh NEW` changes the CLI and plugin names at build time.
The Python module uses underscores, as in `python3 -m kevin_agent_kit` with
the build directory on `PYTHONPATH`. See the [architecture](docs/architecture.md),
[hosting adapters](docs/hosting.md), and [known issues](docs/known-issues.md).

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
