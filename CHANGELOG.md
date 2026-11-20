# Changelog

## 0.3.1

- Add `doctor`, which reports whether Bubblewrap, OpenSSL, Git and the agent CLIs work on
  this machine and what to do when isolation is unavailable.
- Add an opt-in unisolated mode (`isolation = "none"`) and `trust = "local"`, with
  `init --agent codex|claude` and a `core.agents` adapter that runs the real CLI as
  reviewer or worker and records exit codes, review results and required-check results.
- Document that the kit exports candidate bundles and has no merge train.
- Carry protected findings across a forest redesign (`enqueue --inherit-from`), scope repeat
  detection to the lens, keep protected findings from rejected receipts, and allow
  reclassifying an untouched queue entry.
- Read the fitness policy from the trusted target in the CI examples (`--policy-from-target`)
  and run the GitLab example on merged-result pipelines only.
- Package the committed `HEAD` tree, check owner and write access of install and launch
  paths, and bind failed-install cleanup to the directory it created.
- Report files the security scan could not inspect (`--require-complete`), validate design
  sections structurally, reject lens settings that never apply, re-check the daemon log,
  and add runnable-queue age to status.

## 0.3.0

- Add `wiki` for repository knowledge: `init`, `ingest`, `lint` with
  judgement-task generation, and read-only linting of hosted wikis. Sources are
  immutable and checked by digest.
- Hold `research` branches in `wiki-needed` until a synthesis page lands, with
  `wiki_landing` and `wiki_dir` profile settings. Add a research rule and
  wiki CI examples.

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
