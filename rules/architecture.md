# Architecture quality

- Review the canonical design, failure modes, rollback, latency and memory
  budgets, and test plan with architect and SRE personas before implementation.
  Early review catches assumptions that are expensive to change in code.
- Classify findings by failure mechanism. Pause automatic repairs and revisit
  the design when a class repeats across the configured number of rounds.
  Recurrence across revisions signals a change in scope.
- Check architecture and performance constraints in CI and shrink the known
  violation baseline as debt is fixed. This preserves progress across changes.
- Define service objectives and inspect them after deployment. Track the
  loop's queue, review rounds, escalations and merge failures so stalls can be
  diagnosed with evidence.
