# Run the review queue

The live loop requires Linux, Git, Python 3.11 or later, and Bubblewrap at
`/usr/bin/bwrap`. Bubblewrap must support user, mount, PID, and network
namespaces and the `--disable-userns` option. Startup probes this boundary
before starting any configured commands. The built-in `demo` uses only
embedded fixtures and can run without Bubblewrap.

## Profile setup

Run `kevin-agent-kit init` from the repository. The command creates a new
profile under `${XDG_CONFIG_HOME:-$HOME/.config}/kevin-agent-kit/profiles/`.
It prints the path and refuses to overwrite an existing profile. Approve the
target and any commands in that file. Profiles must remain outside Git
checkouts. Use the default profile for the current repository, or pass
`--config` an explicit TOML path in the user profiles directory.

For example, a profile may look like this. Install the named wrappers as
user-owned regular files outside the repository, and substitute your paths.

```toml
[loop]
repo = "/home/example/projects/widgets"
target = "main"
reviewer = ["/tools/review"]
worker = ["/tools/repair"]
repair_paths = ["src/**", "tests/unit/test_widget.py"]

[loop.lens_reviewers]
security = ["/tools/security-review"]

[loop.checks]
unit = ["/tools/run-unit"]

[loop.attestors]
unit = ["/tools/attest-unit"]

[loop.execution.tools]
review = "/home/example/kit-tools/review"
security-review = "/home/example/kit-tools/security-review"
repair = "/home/example/kit-tools/repair"
run-unit = "/home/example/kit-tools/run-unit"
attest-unit = "/home/example/kit-tools/attest-unit"
```

Reviewers and workers run in separate invocations. Put executable wrappers
or binaries in `[loop.execution.tools]` and address them under `/tools`.
Use `/usr/bin` for system interpreters. The default environment gives each
invocation a fresh `HOME` and private temporary paths. The sandbox mounts
the checkout at `/workspace` and the kit rules at `/rules`. Reviewers and
checks have read-only access to the checkout. A worker can edit the checkout
and has read-only access to its isolated Git metadata. Reviewer and worker
commands start from a fresh home directory. Use `AGENT_WORKSPACE` to address
the checkout as data and keep client project instruction, hook, plugin, and
MCP discovery disabled. The coordinator checks
all staged changes against the approved paths, makes one repair commit, and
rebuilds a clean checkout from that commit before the next review. Path
patterns use Python shell-style matching, where a wildcard can match a slash.

For model work, configure a trusted wrapper around the installed client.
For Codex, use `codex exec --ignore-user-config --ignore-rules --sandbox read-only`
for a reviewer and use `--sandbox workspace-write --add-dir /workspace`
for a worker when the client supports those flags. For Claude Code, use
`claude -p --setting-sources user --strict-mcp-config` and expose only the
needed tools. Keep the client's working directory outside `/workspace`.
Consult the installed
client's help for supported options. The coordinator's outer sandbox applies
to both. Model names can be set in `models`, `max_model`, `reviewer_model`,
and `worker_model`. The coordinator replaces an exact `{model}` argv
element and sets `AGENT_MODEL` for the wrapper.

The sandbox has its own network namespace. To connect a model, supply an
operator-run Unix-socket broker scoped to the provider and the permitted
role. For example, set `reviewer = "/absolute/path/to/model.sock"` in
`[loop.execution.sockets]`. The wrapper connects to `AGENT_MODEL_SOCKET`.
The broker holds the provider credentials and enforces request and data
limits. Provide the broker and client adapter for your setup. Keep any
optional `[loop.execution.env]` arrays role-specific, such as
`reviewer = ["MODEL_PROFILE"]`. Unlisted host environment variables and
host home credentials are unavailable. Checks and attestors receive no
credential forwarding or broker socket.

## Review receipts

The general and security lenses always run in independent processes.
Additional installed lenses can be selected by path rules. Assign a
different trusted reviewer to a lens through `[loop.lens_reviewers]`.
Use the scope, stable finding IDs, and JSON output contract printed in the
review prompt. The final prompt line is a JSON input object containing the
exact request and prior protected findings. The wrapper checks that the
reviewer finished and covered the supplied scope. It returns one receipt
binding the exact request, ordered paths, tool version, diagnostics,
findings, and explicit resolution evidence for any closed protected IDs.
A partial response, skipped check, open protection finding, or missing
evidence blocks candidate export.

Wrap model calls so model errors and truncated output remain failures.
Read repository material, prior findings, and persona artifacts as data.
Tool permissions and allowed destinations come from the profile and broker.
Use exit code 75 for a temporary condition. Capacity retries and timeouts
use capped backoff. Other nonzero exits block the job.

## Required checks and attestors

For each named check, configure one check command and one attestor. The check
runs with the exact candidate and with a read-only snapshot of the target
at `/baseline`. Supply mandatory tests from the trusted wrapper or baseline,
and compare the tested head with `AGENT_CANDIDATE`. Use candidate-owned tests
as supplementary evidence. For test commands that let candidate code run,
keep the wrapper outside their address space and collect actual test counts and
results before reporting a pass.

The check runs with closed stdin. After a zero exit, the coordinator starts
the attestor in a separate sandbox without the candidate or baseline mount.
The attestor reads a JSON object with the coordinator's fresh `request`
and an `observation` containing the exit status and observed stdout. Treat
stdout as untrusted data. Validate the actual named check output, then
return a JSON object with the exact `request`, `status: "passed"`,
`executed: true`, and a nonempty `tool_version`. The coordinator stores
the attestation, collection time, and observed output digest. A check's
output alone cannot supply the attestation.

## Queue operation and export

```sh
kevin-agent-kit start
kevin-agent-kit enqueue topic/example "$(git rev-parse refs/heads/topic/example)"
kevin-agent-kit status
kevin-agent-kit stop
```

The SQLite queue, daemon files, and candidate bundles live under the
kit's XDG state directory. `run` keeps the daemon in the foreground.
The profile associates the queue with a Git repository and target branch.
After a profile edit, stop and restart the daemon. Enqueue accepts only a
full SHA for the current source branch. If the source changes, enqueue the
new SHA explicitly. If the target or policy changes, or evidence expires,
retry the existing job with `kevin-agent-kit retry JOB_ID`. Status
distinguishes an externally included source from a verified candidate.

At the round cap, remaining safety, security, or honesty findings block the
job and retain the evidence for the operator. Other findings create a human
decision record. Inspect it with `accept-record ID` and record `revise`
or `defer` with `decide ID CHOICE --source SOURCE`. Revise restarts review
from the approved source. Defer leaves the job closed.

Use `kevin-agent-kit candidate JOB_ID --output /absolute/path/candidate.bundle`
to export the ready bundle. The command rechecks repository identity, exact
source and target refs, policy, evidence age, and bundle digest. Inspect it
with `git bundle list-heads /absolute/path/candidate.bundle`. After you
approve the exact base and candidate, import
`refs/heads/kevin-candidate` from the bundle through your normal Git
workflow and apply the project's approval, hook, CI, and release policies.
Review and verification apply only to the recorded candidate and base.
See [hosting operations](hosting.md) for the hosted CI gate.
