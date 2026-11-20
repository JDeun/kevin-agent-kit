---
status: superseded
date: 2026-11-20
superseded-by: signed-candidate-evidence
---

# Prepare isolated candidates for human integration

The kit runs code from repositories and from agent outputs. Shared Git refs,
home credentials, and persistent queue state belong to the operator. The v0
loop coupled these resources to execution and updated a target automatically
after a local check.

The v0.1 coordinator uses user-owned profiles outside Git checkouts and
Linux Bubblewrap for each runtime role. Verification runs with no credentials.
A separate attestor receives the run identity and captured observations.
The coordinator prepares a candidate in private Git storage and offers a
bundle only while the recorded evidence is current. The repository owner
integrates it using the project's existing workflow.

A private home directory alone would still allow reads through host absolute
paths. A compare-and-swap update of a shared branch would still race a human
checkout and would bypass parts of the repository approval workflow. The
chosen boundary moves both concerns out of the worker's runtime.

The live loop now depends on Bubblewrap and the operator's check wrappers.
Hosted model calls need a scoped broker. Applying a candidate requires an
explicit owner action. See [loop operation](../loop.md).
