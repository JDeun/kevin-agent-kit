# Operational observability

Keep service-level objectives (SLOs) with the service's design or deployment
policy. Each objective needs an owner, a telemetry query, and an action when
the observed value breaches the limit.

## SLO definition template

| Signal | Measurement | Fill in for your service |
| --- | --- | --- |
| Latency p95 | End-to-end duration across eligible requests. Aggregate histogram buckets before calculating the percentile. | User operation, eligible outcomes, window, minimum sample count, p95 limit in milliseconds, dashboard/query |
| Error rate | Failed eligible requests divided by all eligible requests in the same window. Count timeouts and define the handling of client errors and retries. | Error criteria, denominator, window, minimum sample count, percentage limit, dashboard/query |
| Queue congestion | Age of the oldest runnable item and depth of runnable work. Record how delayed and in-flight work are treated. | Queue, aggregation, age and depth limits, sustained breach window, dashboard/query |

Record the expected rollout traffic, metrics ingestion lag, comparison
baseline, paging or rollback thresholds, and the person or automation with
rollback authority. Use the same definitions in alerts and rollout checks.

## Read the SLOs after deployment

1. Record the deployment version, cohort, region or queue, and start time.
   Verify that sample traffic and telemetry are attributed to that rollout.
2. Wait for the documented warm-up interval and telemetry lag. Evaluate full
   post-deployment windows with the agreed traffic minimum. Keep a previous
   stable window or control cohort available for comparison.
3. Query p95 latency, error rate, and queue congestion using the SLO's exact
   filters and units. Record window boundaries, counts, values, thresholds,
   and query links in the deployment evidence.
4. Pass when all objectives pass. Hold progression if a query fails, samples
   are insufficient, or telemetry is stale. On a sustained breach, follow the
   recorded abort or rollback procedure and verify the recovered version
   with the same queries. Treat the pre-deployment load check as separate
   evidence for the design's latency budget.

## Observe the review loop

Run `kevin-agent-kit status` against the project's profile and read its JSON
`metrics` object alongside job phases and evidence. A polling integration can
sample gauges and take deltas of cumulative counters per queue database.

| Key | Meaning |
| --- | --- |
| `queue_length` | Jobs in the review phase, including in-flight work and scheduled retries. Use `counts` for the design and redesign queues. |
| `review_rounds` | Completed review rounds recorded by v0.2 and later, including rounds in retried runs. Per-run evidence includes `rounds`. |
| `forest_escalations` | Jobs converted to redesign after recurring classes. The triggering evidence includes `repeated_classes`. |
| `merge_failures` | Candidate preparation failures grouped by `conflict`, `unrelated-histories`, `timeout`, or `merge-error`. A later retry preserves the earlier failure in this counter. |

Track these over time against the queue's expected throughput. Inspect
`jobs[].detail` and the exact run evidence when the queue grows, rounds per
job rise, or a failure reason becomes more frequent. Hosted merges and
deployment SLOs are measured at their respective sources.
