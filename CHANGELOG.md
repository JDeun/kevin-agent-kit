# Changelog

## 0.2.0

- Require design-only architect and SRE review before feature and architecture
  implementation enters the review loop. Add a profile switch and a design template.
- Hold branches for redesign when the same finding class recurs in consecutive
  review rounds. Record the evidence and expose loop counters in status.
- Add architecture fitness checks, a shrinking violation baseline, Python
  static and load test examples, and GitHub Actions and GitLab CI templates.
- Document service objectives and post-deployment verification.

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
