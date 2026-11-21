# Architecture

kevin-agent-kit runs a local coordinator for a Git repository. The operator
approves an executable profile outside the checkout. The coordinator prepares
a candidate, and the project owner decides whether to apply and publish it.
See the [signed candidate evidence decision](adr/signed-candidate-evidence.md).
There is no merge train: the kit never updates the target branch. It exports a verified
candidate bundle, and the owner fetches and merges it with the repository's normal workflow.

```mermaid
flowchart TD
    Brief["Design brief"] --> Alternatives["Independent artifacts"]
    Alternatives --> Use["Use review"]
    Alternatives --> Adversarial["Adversarial review"]
    Use --> Summary["Synthesis"]
    Adversarial --> Summary
    Summary --> Choice{"Human design choice"}
    Choice --> Design["Commit design and run architect and SRE review"]
    Design -->|Approved| Branch["Implement and enqueue exact head"]
    Branch --> Queue["Private local queue"]
    Queue --> Admission{"Design evidence required and present?"}
    Admission -->|Design needed| Design
    Admission -->|Approved or exempt| Snapshot["Candidate from pinned source and target"]
    Snapshot --> Review["Independent review per lens"]
    Review -->|Repair within approved scope| Worker["Worker in a private checkout"]
    Worker --> Review
    Review -->|Repeated finding class| Forest["Hold branch for redesign"]
    Forest --> Design
    Review -->|Protected finding at cap| Blocked["Block and retain evidence"]
    Review -->|Ordinary finding at cap| Record["Human decision record"]
    Record -->|Revise| Queue
    Review -->|Clean review| Verify["Required commands on fresh read-only trees"]
    Verify -->|Signed mode| Attest["Independent check attestation"]
    Attest -->|Current evidence| Bundle
    Verify -->|Unsigned mode| Bundle["Exportable candidate bundle"]
    Bundle --> Release{"Repository owner approval"}
```

## Execution and state

Each agent or check runs under a role with its own filesystem, process, and
network namespace. The coordinator owns the state directory and imports only
the approved Git objects into private repositories. Approved scoped repairs
remain in the candidate history. Review and verification of a repair use a
clean checkout of its committed content. Each required command gets a separate read-only
input with a tree ID fixed before execution. The signing adapter receives a scoped
bundle and the direct run evidence. See the [execution contract](design/candidate-execution.md).

The coordinator records the repository, target, source head, merge base,
candidate, policy, and exact execution evidence. A ref or policy change
invalidates the previous result. A human can inspect the evidence and export
the candidate bundle while it is current.

See [operating the loop](loop.md) for profiles and command contracts. Follow the
[worker rules](../agents/worker-rules.md) and use the [ledgers](ledgers.md) for
decisions and resource ownership. The [hosting adapters](hosting.md) and
[screenshot guide](../verify/README.md) cover publishing evidence.
See [observability](observability.md) for queue metrics and rollout objectives.
