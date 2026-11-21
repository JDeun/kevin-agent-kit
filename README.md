# kevin-agent-kit

A local harness that coordinates coding agents from implementation through review to a verified candidate
bundle. The kit never merges: the repository owner applies the candidate.

> one human holds the decision gate; an agent fleet does the rest

> **Experimental v0.3.4.** Live reviews, repairs and checks require working Linux
> Bubblewrap namespaces. Model credentials stay in an operator-run broker. Each
> required check receives a fresh read-only tree pinned to the candidate commit.
> The fixture demo runs on machines without Bubblewrap. See [known issues](docs/known-issues.md).

[한국어](docs/ko/README.md) · [Repository](https://github.com/JDeun/kevin-agent-kit.git)

People set direction, choose among reviewed designs, and authorize shipping and
cleanup. The crew organizes the work, reviews the proposed changes, repairs
issues in isolated worktrees, and gathers verification evidence.

## Start on a new computer

### 1. Install the CLI and user rules

Install Git and Python 3.11 or later at `/usr/bin/python3` for the packaged CLI.
The launcher uses isolated Python startup. Optional packages for installed
commands must be available to the system interpreter. Run `kevin-agent-kit doctor` to
check namespace support. Signed evidence also requires OpenSSL. Use a trusted checkout of this
kit. The installer defaults to `~/.local` and refuses a prefix, `bin` or `share` that another
account can write or that is a symlink.

```sh
./scripts/install.sh
export PATH="$HOME/.local/bin:$PATH"
kevin-agent-kit --version
kevin-agent-kit doctor
kevin-agent-kit demo
kevin-agent-kit init --global --dry-run
kevin-agent-kit init --global
```

Persist the `PATH` setting in the shell that launches Claude Code. The demo
uses bundled fixtures and a temporary Git repository. The global installer
copies the installed rule text into a managed block in `~/.claude/CLAUDE.md`
and `~/.codex/AGENTS.md`. Each block records the installation ID, kit version,
and content hash. Rerun `init --global` to update an unedited snapshot after
approving a new version. An ownership or content conflict stops the operation
before writing either file. Review an older link or snapshot block before
updating it with `init --global --adopt-rules-sha256 SHA256`. Use the exact
digest reported for the reviewed block and repeat the option for distinct
digests. See [global rule ownership](docs/adr/global-rule-ownership.md).
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
for independent review and required checks. Approve each evidence adapter's
bundle scope and retention in the profile.
Repair is optional and needs both a worker command and an approved path scope.

For unsigned evidence, `kevin-agent-kit init --agent codex` (or `claude`) drafts
a profile for a broker client you supply. Register its executable and scoped socket,
then review the detected check command. Both trust modes enforce the same sandbox.
When upgrading, stop the old daemon and replace host-CLI profiles. Signed profiles
also need a direct command for each attested check under `[loop.check_commands]`.

The profile lives under `${XDG_CONFIG_HOME:-$HOME/.config}/kevin-agent-kit/profiles/`.
See [profile and wrapper setup](docs/loop.md).

### 3. Run the loop

Use a local source branch with a committed change. For another profile, pass
`--config /absolute/path/to/profile.toml` before the subcommand.
For a new feature or architecture change, first commit the
[design document](docs/design-template.md) on its own and run
`kevin-agent-kit design-review BRANCH DESIGN_SHA --design PATH`. Continue after
the architect and SRE adapters approve the design.

```sh
kevin-agent-kit start
kevin-agent-kit enqueue feature/example "$(git rev-parse refs/heads/feature/example)" --kind feature
kevin-agent-kit status
```

Look for a `candidate` job ID. After reviewing the evidence,
`kevin-agent-kit candidate JOB_ID --output /absolute/path/candidate.bundle`
exports the verified commit with its reachable Git history to a local file.
Apply it through your repository's normal
approval and Git workflow. State, queue, and logs live under
`${XDG_STATE_HOME:-$HOME/.local/state}/kevin-agent-kit/`.

The loop holds a branch for redesign when a finding class repeats across two
review rounds. See the [queue guide](docs/loop.md) for the design gate,
configuration switches, and how to resume after a hold.

### 4. Stop the loop or remove the global rules

```sh
kevin-agent-kit stop
kevin-agent-kit init --global --uninstall --dry-run
kevin-agent-kit init --global --uninstall
```

The uninstall command removes the kit's intact, recorded blocks. To remove the Claude Code
plugin, use `/plugin uninstall kevin-agent-kit@kevin-agent-kit`.

For development in a trusted source checkout, run `python3 -m core.loop demo`. Use `./scripts/build.sh` to produce a standalone artifact.
The build packages the committed `HEAD` tree and refuses a working tree that differs from it
(`--allow-dirty` packages working-tree bytes for development). The installed launcher refuses to
run from a directory another account can write. Scan the generated directory with the source.
`./scripts/rename.sh NEW` changes the CLI and plugin names at build time.
Use the generated `bin/kevin-agent-kit` launcher for the packaged runtime. See the [architecture](docs/architecture.md),
[hosting adapters](docs/hosting.md), and [known issues](docs/known-issues.md).

- [Worker rules](agents/worker-rules.md) cover branch scope and resource ownership.
- [Persona review](reviews/persona/README.md) produces alternative designs and a decision brief.
- [Screenshot verification](verify/README.md) supplies running-app evidence for a PR or MR.
- [Guards](docs/guards.md) and [ledgers](docs/ledgers.md) describe the available checks and records.
- [Architecture fitness](docs/fitness.md) shows how to run `kevin-agent-kit fitness` and ratchet known violations in CI.
- [Knowledge wiki](docs/wiki.md) keeps research as immutable sources, synthesis pages and a map, with `kevin-agent-kit wiki` and a loop landing check.
- [Observability](docs/observability.md) defines the loop's status metrics and shows how to verify SLOs after rollout.
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
