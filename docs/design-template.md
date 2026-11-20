# Design title

## Canonical design

State the decision, its goals, and which artifact is authoritative.
Describe the boundaries, contracts, invariants, and alternatives considered.

## Failure modes and rollback

Describe partial failures, how to detect them, and how to restore the previous
behavior and state. Specify who can trigger rollback and what proves recovery.

## Latency budget

Name the workload, measurement window, metric and limit. Identify the
expensive stages and the assumed traffic or concurrency.

## Memory budget

Specify the peak memory limit, measurement method, and input size and
concurrency assumptions.

## Test plan

Exercise contracts, failure handling, rollback and budgets. State how
deployment verification reads the service objectives.
