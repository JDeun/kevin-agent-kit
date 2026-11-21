---
status: accepted
date: 2026-11-21
---

# Isolate candidate execution independently of evidence signing

An exit code can establish that a command ran, but a writable checkout and a
shared account cannot establish what input it read or protect model credentials.
Before-and-after comparisons also omit intermediate changes and ignored files.

Require an OS-enforced boundary for live commands in both trust modes. Run each
required check against a new, read-only input and bind its tree ID before
launch. Keep credentials in a separate broker. The cost is a namespace-capable
host, disposable disk space and migration of host-CLI profiles. Signed sources
retain their separate attestation and approved data scope.

See the [execution and receipt contract](../design/candidate-execution.md).
