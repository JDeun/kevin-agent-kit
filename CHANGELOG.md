# Changelog

## 0.1.0

- Added a Claude Code marketplace, managed global rules with backups and dry runs,
  and repository profile drafts under XDG configuration.
- Moved the queue, logs, default ledgers, and candidate bundles into private XDG
  state. Added exact-SHA queueing and explicit retry.
- Isolated agents, persona runs, and checks with Bubblewrap and role-based
  credential allowlists. Scoped worker repairs to approved paths.
- Bound review completion and required check attestations to the exact candidate
  and policy. Added a separate strict hosted CI gate.
- Changed the loop to export a verified candidate bundle for human integration.
  Build artifacts use tracked input paths.
