# Known issues in v0.3.2

**Status: experimental.** The kit is not yet a security boundary. Read the supported scope first.

## Supported scope

- **Local trust mode** (`init --agent codex|claude`) is unisolated and unsigned. The model CLIs and
  required checks run with your account's files, network and credentials. The kit narrows what it
  can (required checks get an empty `HOME`; a repair or check that moves HEAD or edits tracked files
  on the candidate is rejected; Codex runs with `--sandbox danger-full-access` because its own
  sandbox needs user namespaces), but a candidate's project rules and review text can still influence
  a model client, and a check can still read any file this account can. Use a disposable host or
  container and only candidates you trust. This is the support boundary, not a roadmap item.
- **Isolated, signed mode** needs Linux Bubblewrap, OpenSSL and external signing brokers. It was not
  exercised end to end on a real host for this release.
- **CI examples** (fitness, wiki) are advisory when defined inside the repository they check, because
  the candidate can edit that definition. They are a gate only when the approved definition is
  enforced from outside the candidate (see `docs/fitness.md`). The kit cannot verify that.
- Real-model verification covered the Codex CLI in local mode. Claude CLI, remote CI runs and external
  brokers were not exercised. A failed Codex or Claude call reports only its exit status to the
  loop; the cause is on the adapter's stderr.

Finding numbers refer to the pre-release audits (v0.1 `M`, `N`, `H`, `L`; v0.2 `V`; v0.3.1 `R`, `W`).
v0.3.2 fixed N11 (permissions come from the Git file type and the tree is checked at install and
launch), W01 (symlinks in the wiki), R01 (strict review verdicts), R02 (repair and check bound to
the candidate commit), R04 (Codex sandbox in unisolated mode) and, within the supported scope above,
R03 and V02. Still open and tracked:

| Finding | Remaining work |
| --- | --- |
| H02 | The signed evidence does not carry an external policy revision, so a source that changes its checks behind the same key and socket does not invalidate existing candidates. |
| H04, M15, R03 | Real model clients start in the candidate checkout. A wrapper that reloads project rules or follows instructions in candidate text can be influenced by it; the kit cannot prove which rules a client loaded, and in local mode the same account's files and model credentials stay reachable. Not fixed; see the supported scope. |
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

Other open items from the v0.3.1 check: a concurrent wiki `ingest` can lose a manifest entry; the
research-landing check reads only the changed path names; `retry` after a new commit retries the old
SHA and goes stale (enqueue the new SHA instead); a new design branch needs `--inherit-from` to carry
protected findings; the status count treats a running job as runnable.
