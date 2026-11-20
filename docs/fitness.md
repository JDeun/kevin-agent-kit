# Architecture fitness tests

A fitness test encodes an architecture constraint so CI can verify it on every
change. Use one stable identity per violation class, file and subject. Include
an occurrence count so an extra instance in the same file fails. Keep line
numbers out of the identity so unrelated edits preserve baseline entries.

## Run static checks and ratchet debt

Copy [the Python configuration example](examples/fitness/fitness.toml) into
the target repository as `fitness.toml`. Choose its source root, exclusions,
path rules and blocking APIs according to the repository's architecture.

```sh
kevin-agent-kit fitness scan --config fitness.toml
kevin-agent-kit fitness check --config fitness.toml \
  --baseline fitness-baseline.json --target origin/main
```

In a trusted target commit, seed `fitness-baseline.json` with an approved
`scan` result or `[]` for a clean repository. The checker reads the target's
baseline from Git and the proposed file from the candidate checkout. A
missing baseline in the target means zero known violations. Every proposed
count must be at most its target count. Every observed occurrence needs an
entry in the proposed baseline. Delete or reduce the entry in the same change
that fixes the corresponding code. New violations, baseline growth, and
entries left behind after fixes all fail with exit code 1. Invalid input
or unavailable history exits 2. The report lists the affected path, rule,
subject and occurrence count.

The Python example checks imports and from-imports for configured dotted
module prefixes by source path. It resolves straightforward Python import
aliases and detects direct calls to configured APIs in async functions.
The synchronous body of a nested function has its own scope. Calls made
while evaluating its defaults or decorators still run in the enclosing scope.
Complement these checks with load tests and review for dynamic dispatch,
indirect blocking helpers and CPU-bound work.

For another language, have its checker emit a JSON array with the same
`rule`, `path`, `subject`, and positive integer `count` fields. Use
`--findings path/to/findings.json` for each analyzer result. The generic
comparison accepts results with or without the Python checker. Have CI run
the analyzers so the inputs come from the candidate under test. Use distinct
rule names for independent analyzers.

## Exercise a latency budget

Copy [the Python load template](examples/fitness/latency-budget.py) and replace
`exercise()` with a representative request or operation. Run it with the design's
workload size, concurrency, warm-up and p95 limit. The template reports sample
count, errors, throughput and observed p95. A failed request or a missed
latency budget fails the run. Run performance gates on a stable runner and
record the workload, input and environment with the result.

```sh
python3 docs/examples/fitness/latency-budget.py \
  --requests 200 --concurrency 8 --p95-ms 200
```

## CI setup

Use the [GitHub Actions](examples/fitness/github-actions.yml) or
[GitLab CI](examples/fitness/gitlab-ci.yml) example. Set the GitHub repository
variables `FITNESS_KIT_REPOSITORY` (the public kit's owner and repository) and
`FITNESS_KIT_REVISION` (the approved full commit SHA). For GitLab use
`FITNESS_RUNNER_IMAGE` to select an approved image with the same kit on PATH.
Set up the target baseline and policy first. Require the
job before merge. Both examples pass `--policy-from-target`, which reads
`fitness.toml` from the trusted target commit, so a candidate cannot widen its
own exclusions or drop a forbidden boundary. Changing the policy then needs a
change that lands on the target first (a separate reviewed change). Review
changes to the CI definition and analyzer as architecture policy changes, and
keep their approved versions outside the candidate checkout if the job provides
an independently signed required check.

The GitHub example uses the pull request merge result and its target commit.
See [GitHub pull request events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request).
The GitLab example fetches from the merge request's target project, including
for fork pipelines. The runner needs read access to that repository. A merged
results pipeline pins the baseline to its target SHA and checks the integration;
the example runs only for merged-result pipelines and fails when the target SHA
is missing, instead of falling back to a moving `FETCH_HEAD`.
See [GitLab merge request pipelines](https://docs.gitlab.com/ci/pipelines/merge_request_pipelines/).
