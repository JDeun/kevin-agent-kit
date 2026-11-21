# Changelog

## 0.3.7

A finding is identified by its journal item, not by the name the reviewer gave it.

- The lineage re-check compares findings by source job, lens and a hash of the
  finding's content (with the immutable journal row kept for display). A parent
  that records the same ID for a different finding, or a finding whose ID
  collides with one of the child's own, is no longer treated as already
  reviewed: the child must resolve it, and a candidate that never saw it is
  sent back to review. An inherited finding whose ID collides is offered to the
  reviewer as `ID~<hash>`.
- Export is held while any other job on the same lineage (its branch or a
  parent's branch) is in `review` or `interrupted`.
- A candidate prepared by 0.3.6 has the old lineage record and must be
  retried.

## 0.3.6

Inheritance across branches is a live dependency, not a snapshot taken at dispatch.

- A job that names `--inherit-from` waits while any job up its inheritance
  chain is still in `review` or `interrupted`, so it is never dispatched before
  its parent's protected findings are final.
- A candidate records the lineage findings its review saw. Before a job is
  stored as a candidate, in `status`, and on every export, the coordinator
  reads the parents' and the branch's journals again. A finding recorded
  since, or a parent back in review, sends the job back to review or makes
  the candidate stale. A candidate prepared by 0.3.5 has no lineage record and
  must be retried.
- Starting the coordinator re-reads the stored output of every open review
  attempt, so a finding printed before a forced kill is journaled (or becomes an
  unreadable-finding obligation) before `resume` can close the attempt.
- The command supervisor now ends its process group when the coordinator dies
  instead of waiting forever, so a killed coordinator leaves no orphan session.
- `docs/known-issues.md`: correct the interrupted-review wording and the
  `--inherit-from` note.

## 0.3.5

Redesign of stop integrity and install trust. See
`docs/design/stop-integrity-and-install-trust.md`.

- Journal each protected finding the moment its JSON object closes in the
  reviewer's output, in an append-only table. A review attempt closes only
  with a separate marker, written when the whole output was read, parsed
  as one complete JSON document and was recorded. Output cut by a stop,
  timeout or size limit, text around the document or a failed write keeps the
  attempt open with the raw output and moves the job to `interrupted`. A
  finding that was printed but cannot be stored is journaled as a stand-in the
  next review must resolve. No statement deletes an attempt or a journal row.
- Add `resume JOB_ID` and `evidence JOB_ID`. A resumed job and every later SHA
  on the branch inherit the journaled findings. `--inherit-from` accepts any
  job, follows its sources and marks an interrupted source `superseded`.
- Read `project.toml`, the kit modules and `rules/` once, through one
  descriptor-based trust check, into one snapshot. The launcher imports
  modules from those bytes, and defaults, prompts, the `/rules` mount and the
  policy ID use the same snapshot. The digest covers file names.
- **Upgrade step:** a profile now records the snapshot it approves as
  `install_sha256`. Run `init --approve-install` for each existing profile
  after reviewing the install. Until then the profile does not load and
  `doctor` reports the mismatch. `stop` still works. A persona-review
  profile needs a top-level `install_sha256` as well.
- The 0.3.4 queue is migrated in place. The second path-based trust check,
  the per-path policy hash and the pending-review deletion paths are removed.

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
