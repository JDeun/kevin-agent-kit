---
status: accepted
date: 2026-11-20
---

# Bind architecture gates to review evidence

## Context

Implementation review can approve individually correct changes while repeatedly
repairing the same design flaw. The kit needs source-independent controls
with evidence that survives daemon restarts.

## Decision

Bind design approval to a design-only Git revision and to the trusted review
policy. Use explicit task classification with design required by default.
Keep review findings and their classes attached to the exact candidate.
Use a distinct queue phase for redesign and keep the hold at the branch level.
Compare each violation identity and count with the Git target's baseline.
Record queue activity in the local database used by the status command.

## Consequences

Existing profiles inherit the design gate until the operator configures an
exemption or turns it off. Review adapters need to emit finding classes.
Maintainers review task classifications, fitness policy changes and the
initial debt baseline. Git ancestry and pointwise baseline comparison keep
older approvals and existing debt scoped to their original evidence.

## Alternatives

A document presence check could accept a design written alongside the code.
A total violation cap could trade one fixed violation for a new one elsewhere.
A free-text heuristic could treat different failure mechanisms as one class.

See [the architecture quality design](../design/architecture-quality.md).
