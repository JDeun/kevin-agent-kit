# Architecture quality controls

## Canonical design

The loop's trusted local profile sets policy and the operator declares the task
type. The design gate uses a design-only Git revision and two distinct persona
review adapters. The approval records the exact design blob, ancestry, branch
and policy. The first implementation must descend from the approved revision
and carry the same document into the candidate. The existing signed candidate
review and check stages still apply.

Review adapters assign stable finding classes. The coordinator intersects the
class sets from consecutive completed review rounds in one repair attempt.
Escalation records the repeated classes and holds subsequent revisions of
that branch until a fresh design review. Fitness checks compare counted
violations against the proposed baseline and the trusted target's baseline.

## Failure modes and rollback

An absent, incomplete, rejected or stale design leaves the job pending design.
An adapter or candidate failure preserves the queue and existing protected
findings. A new design-only revision can recover a held branch. A project can
turn off initial design admission in its profile and restart the daemon.
Already escalated branches still need a fresh review. The release adds queue
schema objects in place. Back up the stopped queue before changing kit versions,
and restore the corresponding backup if rolling back executable and state.

## Latency budget

Gate checks use local Git and SQLite before adapters run. Design review takes
at most two configured adapter invocations with the existing per-command
timeout. Repeat detection adds one completed-round record and bounded set
intersection per round. Measure queue wait separately from adapter execution.
Each adopter records its service latency objectives and load in its own design.

## Memory budget

The design Markdown is capped at 64 KiB. Design personas use the existing
review memory admission limits. Repeat detection retains one class set per
configured round and uses the existing bounded number of active jobs.
Fitness scans Python source files one at a time. Each adopter sets peak
memory limits for its own workloads.

## Test plan

Exercise design-only approval, rejection, changed or absent designs, and the
ordinary queue in isolated Git repositories. Exercise repeated and differing
review classes, branch hold, merge failure reporting, and baseline additions
and removals. Run packaging, demo, shell lint and file and Git history scans
before applying the local release tag.
