# Changelog

## 0.3.4

- Check the installed `rules/` directory and files with the same trust check as
  kit modules (owner, write bits, type, links, directory descriptors). The policy
  hash, the prompts and the sandbox `/rules` mount use the same validated bytes.
- Recover and record protected findings from reviewer output when the queue is
  stopped. If the output is cut off or recording fails, keep the pending review
  so the next start marks the job `interrupted` and does not rerun it.

## 0.3.3

- Require Bubblewrap for live work in both trust modes. Keep provider credentials
  in scoped brokers. Replace host-CLI profiles with registered broker clients.
- Pin the candidate tree before each required command and mount its committed
  files read-only in a fresh workspace. Signed checks also bind this direct run
  to an independent attestation. Configure matching `[loop.check_commands]`.
- Require exactly one top-level review verdict. Preserve valid protected findings
  when a response contains conflicting objects or invalid fields.
- Share package and module trust checks across the launcher and child processes,
  including the daemon. Validate load paths and permissions before importing.

## 0.3.2

- Installed files take their permissions from the Git file type (0644 or 0755), never from
  the work tree, and the installer and every launch reject a kit tree with symlinks or
  group or other write access. A symlinked install prefix is refused as documented.
- The wiki refuses symlinks inside its tree (`sources/`, manifest, map, pages), `external`
  skips linked files, and `--task-out` never overwrites a file. The wiki CI examples and the
  generated ingest task put `--dir` before the action.
- The real-CLI adapter accepts only an explicit `complete` verdict with a well-formed
  `findings` array and `resolved` object. Anything else becomes a `partial` receipt that
  keeps its protected findings. Unisolated profiles pass `--unsandboxed` so Codex no longer
  fails on its own Bubblewrap.
- Local mode binds repairs and required checks to the candidate: a worker that moves HEAD is
  rejected, and a required check must start on the exact candidate commit and leave HEAD and
  tracked files unchanged. Required checks get an empty `HOME`. `trust = "signed"` with
  `isolation = "none"` is rejected.
- State the supported scope plainly: README status, `docs/known-issues.md` and the fitness
  and loop docs say that local mode is unisolated and that a CI job defined inside the
  checked repository is advisory.

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
