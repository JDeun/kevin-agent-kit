---
status: accepted
date: 2026-11-20
---

# Require independent signed evidence for candidate preparation

The prior coordinator combined isolated candidate preparation with receipts
derived from primary reviewer output and candidate-run command results.
Separate processes protected the queue and host but provided the attestor
with the same untrusted reporting data.

The coordinator keeps the private checkout, operator-approved profile,
Bubblewrap agents and explicit candidate export from
[the previous decision](isolated-candidate-preparation.md). It prepares a
read-only bundle and requires Ed25519-signed evidence for review coverage,
protected finding closure, and mandatory checks. Each receipt binds the
approved source to the exact candidate and invocation. The review observer
and the check source use separate keys and broker sockets. The check source
observes the approved checks outside candidate control.

This adds OpenSSL and operator-run evidence brokers to the live queue.
The demo uses embedded fixtures. Profiles using the older local attestors
must configure signed sources before they can prepare candidates.
See [loop operation](../loop.md) for the adapter contract.
