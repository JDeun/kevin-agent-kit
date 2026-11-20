# Known issues in v0.1

Finding numbers refer to the v0.1 pre-release audit.

| Finding | Remaining work |
| --- | --- |
| M03, M07 | Retry retains SQLite evidence. Private logs omit command stderr and cap in-flight output, but state has no retention policy and arbitrary agent text can disclose secrets. Use approved wrappers with output filtering and keep evidence private. |
| M06 | Bubblewrap contains detached child processes. Disposable checkout files are discarded, including ignored scratch. A wrapper must return the evidence it needs to retain. Automatic collection of scratch artifacts is pending. |
| M08 | The optional security scan can omit unsupported or excluded files. Its report has no machine-readable completeness gate for required paths. |
| M09 | The sandbox withholds host refs, credentials, state, and outbound network. Standalone command guards still use static rules and do not validate resource ownership for every destructive operation. |
| M10 | Decision and acceptance records use operator-supplied attribution. Signed approval, approved values, and provenance checks are pending. |
| M11 | The resource ledger allocates ordinals. Runtime resource IDs, leases, and release conditions need a separate lifecycle. |
| M13 | A hosting write may have succeeded when read-back fails. Some responses provide an ID, but automatic reconciliation and idempotency keys are pending. |
| M14, M15 | Real model clients need operator-provided wrappers and a scoped broker. Installed session rules rely on the tool reading the user instructions. The local loop and persona workflow include the rules in their prompts. |
| M16 | Per-command time and output limits, per-loop concurrency, and a repair cap are available. Cross-project quotas, provider usage metering, and hard CPU, memory and disk quotas are pending. |
| L01 | Managed rule blocks support dry runs, upgrades, backups, and removal. The standalone CLI installer fails on an occupied prefix. Binary upgrade and rollback need a separate workflow. |

The review contract can require exact scope and explicit closure evidence.
Review quality depends on the trusted adapter and reviewer. For hosted CI,
the provider adapter checks an exact published candidate, base, run and
mandatory checks. The operator still approves which CI source and test
policy are authoritative.
