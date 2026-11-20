---
status: accepted
date: 2026-11-20
---

# Approve evidence data scope per adapter

[Signed candidate evidence](signed-candidate-evidence.md) binds a source to
an exact Git candidate. An unqualified candidate bundle also carries old
repository objects outside the review scope.

Require the trusted profile to approve Git data scope and retention for each
observer and named check. An incremental bundle excludes the histories of
the approved base and source and requires them at the broker. A source
that needs a self-contained bundle requires explicit approval for full
history. The signed request names the approved scope, prerequisites, digest,
and retention period. The operator's broker applies the retention policy.

The local candidate export remains self-contained so the repository owner
can import it independently. See [signed evidence sources](../loop.md#signed-evidence-sources).
