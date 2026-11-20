# kevin-agent-kit

## Start here

Read [the README](README.md) for the local demo and the project purpose.
Read [the worker rules](agents/worker-rules.md) before writing code, opening
reviews, or launching local services.

## Stack

The loop and bundled command-line tools use Python 3.11+ and Git.
Build and install use shell scripts. Playwright is an optional dependency
for running-app screenshots.

## Where to look

- [Architecture](docs/architecture.md) describes the coordinator, branch queue,
  and candidate bundle export. The kit does not merge; the repository owner applies
  a candidate.
- [Hosting](docs/hosting.md) describes GitHub and GitLab adapters.
- [Guards](docs/guards.md) describes the command and change checks.
- [Ledgers](docs/ledgers.md) describes how choices and resources are recorded.
- [Persona review](reviews/persona/README.md) describes the design review inputs
  and evidence.
- [Verification](verify/README.md) describes screenshot capture and review evidence.
- [Cost and context](docs/cost.md) describes budget and session controls.

## Build, run, and test

```sh
python3 -m core.loop demo
python3 -m pytest -q
./scripts/build.sh
```

With pytest-xdist installed, use at most two workers (`-n 2`).
Use an isolated Git test repository for integration checks against stateful
commands. Follow the worker rules for any local service the test starts.

## Working agreements

- Scope each change to its assignment and use a dedicated worktree for code
  changes. Read the target checkout's instructions before modifying it.
- Let reproducible checks settle what they can. Send design, release, and
  cleanup choices that require authorization to the human with evidence.
- Read back the exact commit, verification output, and any published review
  evidence before reporting completion. Keep user-visible entry points
  documented in the English and Korean READMEs.
