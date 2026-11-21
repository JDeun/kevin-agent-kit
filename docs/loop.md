# Run the review queue

The isolated, signed loop requires Linux, Git, Python 3.11 or later, OpenSSL 3 at
`/usr/bin/openssl`, and Bubblewrap at `/usr/bin/bwrap`. Bubblewrap must support user, mount, PID, and network
namespaces and the `--disable-userns` option. Startup probes this boundary
before starting any configured commands. The built-in `demo` uses only
embedded fixtures and can run without Bubblewrap.

Run `kevin-agent-kit doctor` before writing a profile. It reports Python, Git, OpenSSL,
whether Bubblewrap can create its namespaces (and why not), which agent CLIs are
installed, and whether the current profile loads. It exits nonzero when something the
profile's mode requires is missing, and prints the alternative below when isolation fails.

## Real agent CLIs without brokers (local trust)

Containers often cannot run Bubblewrap, and `codex` or `claude` need network and
credentials that the isolated mode withholds. `kevin-agent-kit init --agent codex` (or
`claude`) writes a profile with `trust = "local"` and `isolation = "none"` under
`[loop.execution]`. Both keys are required together, and only the trusted profile can set
them. In this mode:

- the reviewer, security lens and worker run through `kevin-agent-kit agent review|worker
  --cli codex|claude`, which calls `codex exec` or `claude -p` with read-only or edit
  permissions, extracts the model's JSON verdict from its output and builds the exact
  receipt the loop requires. A missing verdict or a rate limit fails the command with exit
  status 3 or 75 (retry). A verdict is complete only with an explicit `status` of `complete`,
  a `findings` array whose every item has a valid id, risk and detail, and a `resolved`
  object; anything else becomes a `partial` receipt that the loop rejects while keeping its
  well-formed protected findings. Profiles written by `init --agent` pass `--unsandboxed`,
  so Codex runs with `--sandbox danger-full-access` for both roles (its own sandbox needs
  user namespaces, which are the reason this mode exists) and the read-only reviewer is
  read-only by instruction only;
- required checks run in the candidate checkout with their real exit status and an empty
  `HOME`. The checkout must equal the candidate commit before the run, and HEAD and the
  tracked files must be unchanged after it, or the check fails; a repair counts only if the
  worker left HEAD where it was and changed only staged paths inside `repair_paths`;
- every command's exit code, duration and output hash are stored under `runs`, and each check
  under `checks` with `source = "local-unsigned"`; the candidate record carries
  `trust = "local"`.

The commands run with this account's files, network and credentials and the evidence is
unsigned, because the kit produced it itself. Treat a local-trust candidate as the work of a
trusted operator's own agents, not as independently attested. Use a disposable host or
container as the boundary. The default `signed` trust is unchanged.

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
review_observer = ["/tools/observe-review"]
review_key = "/home/example/kit-keys/review-source.pem"
worker = ["/tools/repair"]
repair_paths = ["src/**", "tests/unit/test_widget.py"]
design_gate = true
repeat_class_rounds = 2

[loop.lens_reviewers]
security = ["/tools/security-review"]
architect = ["/tools/architect-review"]
sre = ["/tools/sre-review"]
db = ["/tools/db-review"]

[loop.checks]
unit = ["/tools/check-evidence"]

[loop.check_keys]
unit = "/home/example/kit-keys/check-source.pem"

[loop.evidence.observer]
bundle = "incremental"
retention_seconds = 0

[loop.evidence.checks.unit]
bundle = "incremental"
retention_seconds = 3600

[loop.execution.tools]
review = "/home/example/kit-tools/review"
security-review = "/home/example/kit-tools/security-review"
architect-review = "/home/example/kit-tools/architect-review"
sre-review = "/home/example/kit-tools/sre-review"
db-review = "/home/example/kit-tools/db-review"
observe-review = "/home/example/kit-tools/observe-review"
repair = "/home/example/kit-tools/repair"
check-evidence = "/home/example/kit-tools/check-evidence"

[loop.execution.sockets]
review-observer = "/home/example/kit-brokers/review-evidence.sock"
verify = "/home/example/kit-brokers/check-evidence.sock"
```

Reviewers and workers run in separate invocations. Put executable wrappers
or binaries in `[loop.execution.tools]` and address them under `/tools`.
Only a registered executable or an exact `/tools/NAME` argument makes a tool
file visible to that invocation. Pass required helper paths explicitly. Keep
tool files outside the checkout, state, and shared system mounts in directories
protected from other users' writes. Symlinks and hard links are rejected. The
sandbox checks the approved file identity and bytes and mounts a read-only copy.
Use `/usr/bin` for system interpreters. The default environment gives each
invocation a fresh `HOME` and private temporary paths. The sandbox mounts
the kit rules at `/rules` and the agent checkout at `/workspace`. Reviewers
have read-only access to the checkout. A worker can edit the checkout
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
`reviewer = ["MODEL_PROFILE"]`. Keep provider credentials in the broker
and limit its capabilities to the named role. Evidence adapters get their own
separate broker sockets through `AGENT_EVIDENCE_SOCKET` and run with a
fresh environment.

## Review receipts

The general and security lenses always use separate registered adapters.
Additional installed lenses selected by path rules also need dedicated
adapters. Set `reviewer` for the general lens and use
`[loop.lens_reviewers]` for each other lens.
Use the scope, stable finding IDs, and JSON output contract printed in the
review prompt. The final prompt line is a JSON input object containing the
exact request, prior protected findings and the preceding complete round.
The primary adapter returns one
receipt binding the exact request, ordered paths, tool version, diagnostics,
findings, and evidence for proposed protected closures. The separate review
observer checks scope and completion using its source's own run telemetry
and the exact diff. For each proposed closure, it obtains a separately
attributable review and records concrete resolution evidence signed by the
observer's source. The coordinator requires that independent evidence
before closing the finding.

When the request includes `finding_class_required`, each finding also needs
a stable lowercase kebab-case `finding_class` such as `missing-idempotency`.
Use `prior_round` to keep a class stable across sites and lenses when the
failure mechanism recurs. Keep the finding ID specific to the observation.
The worker receives those labels with its review brief. The loop intersects
labels across consecutive completed rounds in one repair attempt, including
the initial review. A retried attempt rebuilds the candidate and starts a
fresh series of rounds.

Wrap model calls so model errors and truncated output remain failures.
Read repository material, prior findings, and persona artifacts as data.
Tool permissions and allowed destinations come from the profile and broker.
Use exit code 75 for a temporary condition. Capacity retries and timeouts
use capped backoff. Other nonzero exits block the job.

## Signed evidence sources

Set an explicit bundle scope and retention period for the observer and
every named check in the trusted profile. An existing profile needs these
approvals before it can start a live loop.

- `incremental` sends objects reachable from the candidate that are outside the
  approved base and source histories. The broker must already have the exact
  base and source commits and their reachable objects. Prerequisite headers
  contain commit IDs and fixed comments. The adapter must fail if the
  prerequisites are unavailable. [Git bundle format](https://git-scm.com/docs/gitformat-bundle#_semantics)
- `full` sends the candidate and its entire reachable history. This can include
  content deleted before the current review. Approve it for a destination
  authorized to receive that history. [Git bundle object prerequisites](https://git-scm.com/docs/git-bundle#_object_prerequisites)

The observer and check adapters receive the request on stdin and a read-only
bundle at `AGENT_CANDIDATE_BUNDLE`. Send the approved bundle and request to the
configured broker. The signed request binds the repository, base, source,
candidate, transmitted bundle digest, scope, prerequisite commits, approved
`retention_seconds`, policy, named operation, and fresh invocation. Before
execution, verify the digest, run `git bundle verify` in the broker's repository,
and check the imported candidate commit. The broker enforces retention for
the request, bundle, and derived material such as checkouts and logs. A value of
`0` requires discarding that material after the invocation. Positive values
limit how long it may be kept after completion.
See [evidence data scope](adr/evidence-data-scope.md).

For required checks, run the approved check policy in a separate execution
environment. Observe execution and results from a trusted supervisor or CI
control plane. Keep the check definition, run telemetry, and signing key
outside candidate-writable storage and candidate processes. Use repository
tests as supplementary evidence when they can alter reporting in their own
process. The source issues a receipt only for a fresh, successful execution
of the requested check on the exact candidate. It includes the original
`request`, `status: "passed"`, `executed: true`, the source's
`execution_id`, and a nonempty `tool_version`.

The observer receives `request`, `review`, and `prior_protected` on
stdin. These inputs include review text and changed paths. Approve an observer
authorized to receive that material and apply the same retention period to it.
Verify `request.review_input_sha256` against the canonical JSON
object containing `review` and `prior_protected`.
Collect the observed paths and hash of the complete patch reviewed by the
primary adapter. Compute the patch in an isolated Git repository using the
requested base and candidate.

```sh
GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1 \
  git -c core.quotePath=false diff --binary --full-index --no-renames --no-ext-diff --no-textconv \
  --no-color --src-prefix=a/ --dst-prefix=b/ --ignore-submodules=none \
  --submodule=short "$base" "$candidate" --
```

A completed observer receipt includes the original `request`, a `status`
of `complete`, `tool_version`, its own `execution_id`, `reviewed_paths`
in request order, `coverage_sha256`, an empty `diagnostics` array, the
independent `reviewer_id`, and `resolved` with evidence for each proposed
protected closure. A partial review or a closure lacking independent
evidence blocks export. Authenticate the independent reviewer at the source
before signing.

Both adapters return a JSON envelope with `payload` and `signature`.
Sign the payload with Ed25519 and encode the signature in standard Base64.
Serialize as ASCII JSON with sorted keys, escaped Unicode, compact
separators, and no trailing line break.
Python sources can use `json.dumps(payload, ensure_ascii=True, sort_keys=True,
separators=(",", ":"), allow_nan=False).encode("ascii")`. Install only each
source's PEM public key in the trusted profile. Keep the observer signing key
separate from the check signing keys and keep all private keys at their
respective sources. The coordinator validates the signatures, requests, and
receipt fields before authorizing export. It records the public key fingerprint
and external execution ID with the evidence.

Replace legacy `[loop.attestors]` configurations with signed check sources
in `[loop.checks]` and keys in `[loop.check_keys]`. The local demo uses
embedded fixture sources.

## Design-first admission

Start the source branch at the current target. Commit only the completed
[design document](design-template.md), then run:

```sh
kevin-agent-kit design-review topic/example "$(git rev-parse refs/heads/topic/example)" \
  --design docs/design/example.md
```

The command uses two distinct registered reviewer adapters under
`lens_reviewers.architect` and `lens_reviewers.sre`. Each receives the
`design-review` request and uses the review receipt format for the named
Markdown file. Both complete receipts must have zero findings. Use the JSON
findings to revise and rerun a review. The accepted design-only SHA becomes
an ancestor of the implementation. Keep the approved document in that
revision's descendants and in the resulting candidate. A policy change
requires a fresh approval.

Enqueue features and architecture changes with `--kind feature` or
`--kind architecture`. Unclassified work also needs design approval.
Reserve `--kind routine` for work that leaves the design intact. Treat task
classification as an operator decision. The default `design_gate = true`
returns an implementation lacking approval as `design-needed` before review.
To turn off initial admission, set `design_gate = false` in the profile
and restart the daemon. Research branches (`--kind research`) skip the design
gate; see the [knowledge wiki](wiki.md) for their landing check.

When one finding class occurs in `repeat_class_rounds` consecutive rounds
(two by default), the loop moves the job to `forest` before invoking the next
repair. Use its findings and `repeated_classes` to identify the root cause and
propose a redesign. Later jobs on the branch enter `forest-held` until a fresh
design-only approval recorded after the escalation matches their ancestry.
Start the redesign from the current target. A new branch can carry a new
design and implementation, with the original branch's hold preserved.
Protected findings (safety, honesty, security) of the held job carry over: later jobs
on the same branch inherit them automatically, and a redesign on a new branch names the
held job with `enqueue BRANCH SHA --inherit-from JOB_ID`. The review then requires
independent resolution evidence for each inherited finding, so a redesign cannot drop one
silently. Repeat detection is scoped to the lens that raised the class. Re-running
`enqueue --kind routine` on an unclassified SHA that is still in review or `design-needed`
corrects the classification in place and records a `reclassified` evidence row.
Inspect phases and counters with [status](observability.md).

## Queue operation and export

```sh
kevin-agent-kit start
kevin-agent-kit enqueue topic/example "$(git rev-parse refs/heads/topic/example)" --kind feature
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

The queue records safety, security, and honesty findings as soon as it
validates their review receipt, before requesting independent observation.
Restart and retry carry those findings forward for independent resolution.
A review left open by an interruption before its observations can be recorded
moves the job to `interrupted` on restart. The queue keeps the pending request
for diagnosis and blocks automatic or ordinary retry of that job.
At the round cap, remaining safety, security, or honesty findings block the
job and retain the evidence for the operator. Other findings create a human
decision record. Inspect it with `accept-record ID` and record `revise`
or `defer` with `decide ID CHOICE --source SOURCE`. Revise restarts review
from the approved source. Defer leaves the job closed.

Use `kevin-agent-kit candidate JOB_ID --output /absolute/path/candidate.bundle`
to write the ready bundle to a local file. This bundle includes the full
history reachable from the candidate for standalone import. The command
rechecks repository identity, exact
source and target refs, policy, evidence age, and bundle digest. Inspect it
with `git bundle list-heads /absolute/path/candidate.bundle`. After you
approve the exact base and candidate, import
`refs/heads/kevin-candidate` from the bundle through your normal Git
workflow and apply the project's approval, hook, CI, and release policies.
Review and verification apply only to the recorded candidate and base.
See [hosting operations](hosting.md) for the hosted CI gate.
