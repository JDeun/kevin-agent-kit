# Known issues in v0.3.1

Finding numbers refer to the pre-release audits (v0.1 `M`, `N`, `H`, `L`; v0.2 `V`).
v0.3.1 fixed the high-severity items V01 (protected findings now carry across a forest
redesign), V02 (CI examples read the fitness policy from the trusted target) and N11
(installer and launcher check path ownership and writability; failure cleanup is bound to
the directory this run created), and these mediums: H01, H11, M07 (daemon log), M08, V05,
V06, V07, V09, V11 and V12. What remains:

| Finding | Remaining work |
| --- | --- |
| H02 | The signed evidence does not carry an external policy revision, so a source that changes its checks behind the same key and socket does not invalidate existing candidates. |
| H04, M15 | Real model clients start in the candidate checkout. A wrapper that reloads project rules or follows instructions in candidate text can be influenced by it; the kit cannot prove which rules a client loaded. |
| M03, M07 | Retry retains SQLite evidence. State has no retention policy and arbitrary agent text can disclose secrets. Keep evidence private. |
| M06 | A disposable checkout's ignored scratch files are discarded. A wrapper must return the evidence it needs to retain. |
| M09 | The sandbox withholds host refs, credentials, state and outbound network. Standalone command guards still use static rules and do not validate resource ownership for every destructive operation. |
| M10 | Decision and acceptance records use operator-supplied attribution. Signed approval and approved-value checks are pending. |
| M11 | The resource ledger allocates ordinals. Runtime resource IDs, leases and release conditions need a separate lifecycle. |
| M13 | A hosting write may have succeeded when read-back fails. Some responses give an ID, but automatic reconciliation and idempotency keys are pending. |
| M14 | The isolated mode needs operator-provided wrappers and a scoped broker. `init --agent` provides a working real-CLI path in `trust = "local"`, which is unisolated and unsigned. It is tested against stand-in `codex` and `claude` executables, not against live model services. |
| M16 | Per-command time and output limits, per-loop concurrency and a repair cap exist. Cross-project quotas, provider metering and hard CPU, memory and disk quotas are pending. |
| N04 | Credentials forwarded to a role can reach its output. Redaction covers exact matches of eight or more characters in some outputs. |
| N05, N06 | Global-rule backups restore file bytes but not both files atomically, and an uninstall leaves empty instruction files and backups with no retention limit. |
| N10 | Stored evidence keeps verified fields and a key fingerprint, not the original signature envelope. |
| N12 | One model socket can serve several roles, and read-only system mounts are broader than the registered tools. |
| N13 | Ancestors of a custom state path are not pinned, and SQLite reopens its checked file by path. |
| V03 | Design re-reviews do not receive earlier findings and keep one row per branch. |
| V04 | A design approval stays valid while its ancestry holds, even if integration assumptions changed. |
| V08, V10 | The fitness scan reopens files by name after checking them and has no file-count or byte limit. The latency template has no per-call timeout or open-loop arrival rate. |
| L01 | The CLI has no upgrade, rollback or uninstall command. The installer fails on an occupied prefix. |
| local merge train | Not provided and not planned. The kit exports a verified candidate bundle and never updates the target branch. |

The review contract requires independently signed coverage and protected closure
evidence. Review quality depends on the approved review source. For hosted CI, the
provider adapter checks an exact published candidate, base, run and mandatory checks. The
operator still approves which CI source and test policy are authoritative.
