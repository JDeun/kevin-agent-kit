# Candidate execution and evidence

## Canonical design

The coordinator alone owns Git objects, queue state and receipts. Every live
candidate-facing command starts inside a fresh Bubblewrap namespace. Its
filesystem contains approved system tools, the disposable workspace and
private scratch space. The host home, network, inherited environment and
model credentials stay outside. Model adapters start in an empty directory
and may call an operator-provided, role-specific broker. The broker owns
authentication. Profiles that request host execution or credential forwarding
fail before dispatch. Missing namespace support stops execution.

Each mandatory command gets a fresh tree materialized from the candidate's
Git objects. The coordinator resolves its tree ID before dispatch and binds
that ID into the request and receipt. Only committed file contents and modes
are mounted read-only at the check's working directory. Git administration
data and scratch files from other invocations stay outside. Tests needing
temporary output use the private scratch directory. Signed evidence additionally
requires an independent source, scoped bundle and matching signed request.
The attestor has its own process and cannot lend its socket to a check.

A model review is one top-level JSON object. An ambiguous, nested or malformed
verdict yields a partial receipt. Valid protected findings survive rejection
and require an explicit resolution. Only a validated complete receipt can
advance the candidate. A failed or stopped reviewer can still emit valid protected
findings. The [finding journal](stop-integrity-and-install-trust.md) records them
as they appear and decides when an attempt may close.

Every generated launcher and internal module restart embeds the same trusted
bootstrap. Trust starts outside the modules being loaded. The bootstrap reads
the install once into the [install snapshot](stop-integrity-and-install-trust.md)
and every later consumer uses those bytes.

## Failure modes and rollback

A failed check, missing isolation or partial review prevents export. An owner
can stop the coordinator, retain its receipts and reinstall a previous release
for inspection. Before resuming a queue, update the trusted profile and collect
new evidence for the exact candidate under the new policy.

## Latency budget

The existing command timeout bounds each adapter invocation. For a job with
multiple required checks, measure elapsed time per check including input
materialization. Large trees add a full read and copy for each invocation.

## Memory budget

Each job materializes one check tree at a time and spools Git blob data to
private disk. Memory grows with the path list and symlink targets. Command
output retained for a review is bounded to 1 MiB. Admission uses the configured
job count and stage memory thresholds.

## Test plan

Use synthetic commits, model output and local package fixtures. Assert that a
check sees only its pinned tree, credentials have no forwarding path, and an
unavailable boundary refuses dispatch. Verify malformed verdicts retain
protected entries. Change package permissions and links and verify refusal
before import through both launcher and internal restart. Exercise the real
namespace when supported and report that coverage separately.
