# Known issues in v0.3.3

## Supported scope

- Live reviews and checks require working Bubblewrap namespaces. The test host
  refused mount namespace creation. Broker clients, live models and external
  signing services need separate deployment verification.
- Each required command runs against a read-only snapshot. Use private temporary
  storage for build products. Relative links must resolve within the snapshot.
  A missing target, an unresolvable link chain or a submodule prevents dispatch.
- **CI examples** (fitness, wiki) are advisory when defined inside the repository they check, because
  the candidate can edit that definition. They are a gate only when the approved definition is
  enforced from outside the candidate (see `docs/fitness.md`). The kit cannot verify that.

The remaining audit items use the identifiers from the pre-release reports.

| Finding | Remaining work |
| --- | --- |
| H02 | The signed evidence does not carry an external policy revision, so a source that changes its checks behind the same key and socket does not invalidate existing candidates. |
| H04, M15 | Candidate text can influence a model's conclusions. The sandbox confines the process. Review quality also depends on the broker client and the approved review source. |
| M03, M07 | Retry retains SQLite evidence. State has no retention policy and arbitrary agent text can disclose secrets. Keep evidence private. |
| M06 | A disposable checkout's ignored scratch files are discarded. A wrapper must return the evidence it needs to retain. |
| M09 | The sandbox withholds host refs, credentials, state and outbound network. Standalone command guards still use static rules and do not validate resource ownership for every destructive operation. |
| M10 | Decision and acceptance records use operator-supplied attribution. Signed approval and approved-value checks are pending. |
| M11 | The resource ledger allocates ordinals. Runtime resource IDs, leases and release conditions need a separate lifecycle. |
| M13 | A hosting write may have succeeded when read-back fails. Some responses give an ID, but automatic reconciliation and idempotency keys are pending. |
| M14 | Operators supply the model broker and client. The kit cannot verify the provider's service or retention policy. |
| M16 | Per-command time and output limits, per-loop concurrency and a repair cap exist. Cross-project quotas, provider metering and hard CPU, memory and disk quotas are pending. |
| N05, N06 | Global-rule backups restore file bytes but not both files atomically, and an uninstall leaves empty instruction files and backups with no retention limit. |
| N10 | Stored evidence keeps verified fields and a key fingerprint, not the original signature envelope. |
| N12 | One model socket can serve several roles, and read-only system mounts are broader than the registered tools. |
| N13 | Ancestors of a custom state path are not pinned, and SQLite reopens its checked file by path. |
| V03 | The coordinator retains the current design review per branch. Long-term design-review history requires separate storage. |
| V04 | A design approval stays valid while its ancestry holds, even if integration assumptions changed. |
| V08, V10 | The fitness scan reopens files by name after checking them and has no file-count or byte limit. The latency template has no per-call timeout or open-loop arrival rate. |
| L01 | The CLI has no upgrade, rollback or uninstall command. The installer fails on an occupied prefix. |
| local merge train | Not provided and not planned. The kit exports a verified candidate bundle and never updates the target branch. |

The signed review contract requires independently signed coverage and protected closure
evidence. Review quality depends on the approved review source. For hosted CI, the
provider adapter checks an exact published candidate, base, run and mandatory checks. The
operator still approves which CI source and test policy are authoritative.

Other open items from the v0.3.1 check: a concurrent wiki `ingest` can lose a manifest entry; the
research-landing check reads only the changed path names; `retry` after a new commit retries the old
SHA and goes stale (enqueue the new SHA instead); a new design branch needs `--inherit-from` to carry
protected findings; the status count treats a running job as runnable.
