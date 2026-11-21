# Stop integrity and install trust

## Canonical design

Two contracts replace the point fixes of 0.3.2 through 0.3.4. This document is
authoritative for both. The [execution and receipt contract](candidate-execution.md)
keeps the sandbox, check input and receipt format.

### Protected finding journal

A reviewer run is one **attempt**. The coordinator opens the attempt before it
starts the reviewer and owns three records:

- `review_attempts` holds one row per attempt: the request identity, an
  `outcome` and a `closed_at` stamp. The row is open until both are set.
- `finding_journal` holds one row per protected finding (safety, honesty,
  security) of an attempt. It is append-only. Database triggers reject `UPDATE`
  and `DELETE` on the journal, `DELETE` on an attempt and `UPDATE` of a closed
  attempt. The store never issues a replacing insert on these tables.
- `review_output` holds the raw reviewer output of an open attempt.

**Journal first.** While the reviewer runs, the coordinator reads its standard
output as it grows and scans it with one incremental scanner. Each protected
finding is written in its own committed transaction the moment its JSON object
closes, together with the output read so far. The same scanner reads the final
output once more when the reviewer ends, so a runner that reports output only
at the end reaches the same journal.

**Close is a separate marker.** An attempt is *settled* only when all three hold:

1. the coordinator read the entire output (no read failure, within the 1 MiB limit);
2. the output contains no JSON object at all, or is exactly one complete JSON
   document, and every finding object in it could be stored;
3. the transaction that writes the remaining findings and the close marker committed.

A settled attempt closes as `complete` (validated receipt), `rejected` (the
reviewer failed or its receipt was invalid) or `stopped` (the queue stopped
it). Closing drops the raw output and keeps its SHA-256.

**Anything else is held.** Output cut by a stop, a timeout or the size limit,
an unreadable stream, text around or inside the JSON document, or a failed
write leaves the attempt open. A stop or timeout whose output still meets the
three conditions closes normally. Balanced brackets are not enough: a stray
quote in surrounding prose can hide a structure from a token scan, so only a
whole-document parse settles output that contains an object. The coordinator
stores the raw output and a reason in the open row and moves the job to
`interrupted`. If the output ends inside a finding object whose fields are
complete, the scanner closes that object and journals the finding as well.

**An unreadable finding becomes an obligation.** When a finding object was
printed but cannot be stored (cut inside a value, or invalid without being a
valid ordinary finding), the coordinator journals a stand-in protected finding
with the printed text. The next review must resolve it explicitly, so neither
`resume` nor a successor can drop it. A coordinator that dies leaves the attempt open with the
findings and output it had already committed, and the next start moves the
job to `interrupted`.

**No path discards a pending attempt.** No statement deletes an attempt or a
journal row. A job with an open attempt is never dispatched. The only exits
from `interrupted` are operator actions:

- `resume JOB_ID` closes the open attempts as `resumed`, keeps their output
  and returns the job to review. The next review receives every journaled
  finding as a prior protected finding and must resolve each explicitly.
- `enqueue BRANCH SHA --inherit-from JOB_ID` moves the work to a successor.
  An interrupted source becomes `superseded`, but only while the successor is
  still waiting for review.

**Inheritance is not tied to one phase.** A job inherits the protected
findings of every other job on its branch whose last state left them open
(every phase except `candidate`, `included`, `waiting` and `deferred`) and of
its `--inherit-from` parent, transitively. `--inherit-from` accepts any job.
A protected finding therefore stays open on a branch until a reviewer of the
current SHA resolves it.

Design reviews use the same scanner. Each finding, stand-ins included, is saved
into the branch's design report as it appears. Unsettled output is kept in that
report and carried into later reports of the branch (the last three).

Alternatives rejected: parsing only after exit (the 0.3.4 design, which loses
what a stop cuts off), and treating a stop as retryable when zero findings
were recovered (which cannot tell "none printed" from "none readable").

### Install trust snapshot

Every install input that shapes policy is read once, through one function,
into one **snapshot**: `project.toml`, every file of the kit packages (code,
review lenses, prompts, the trust bootstrap) and every file under `rules/`.

- **One trust function.** The standalone bootstrap `guards/agent-sec` owns the
  descriptor-based checks. It walks from `/` with `O_NOFOLLOW` directory
  descriptors and requires each object to be a directory or a singly linked
  regular file, owned by the user or root, without group or other write
  access. The loop's path helpers re-export these functions. No second
  path-based check exists.
- **One read.** The bytes returned by that walk are the snapshot. Its digest
  is SHA-256 over every relative name and content, so renaming a rule changes
  it.
- **One consumer contract.** The launcher imports kit modules from the
  snapshot bytes, never from the files again. The coordinator, `doctor`, the
  prompts, the sandbox `/rules` mount and the global-rules installer read
  defaults, lenses and rules from the same snapshot. A process started by the
  kit takes its own snapshot and reports its policy ID to the parent.
- **User approval.** A profile records the snapshot it approves as
  `install_sha256`. `init` writes the current digest. Loading a profile whose
  value differs from the running snapshot fails, so `start`, `run`, `enqueue`
  and `candidate` refuse to proceed, and so does `persona-review` for its own
  profile. `doctor` reports the installed and the approved digest and fails.
  After reviewing an upgrade the user runs `init --approve-install`. `stop`
  does not need approval. Commands that use no profile (`demo`, `guard`,
  `fitness`, `wiki`, `init --global`) run on any install that passes the trust
  walk.
- **Policy ID.** The policy ID is the snapshot digest, the profile bytes, the
  registered tools and sockets and the evidence keys.

An untrusted object fails the walk itself, so an installed launcher exits
before it imports any kit code.

Alternative rejected: adding `project.toml` to the list of checked paths (the
0.3.4 pattern). Each such list left the next unlisted input unchecked.

## Failure modes and rollback

- A journal or close write fails: the attempt stays open and the job becomes
  `interrupted`. Detect it in `status` (`pending_review`) and read the retained
  output with `evidence JOB_ID`.
- A reviewer wraps its document in prose or a code fence and then fails: the
  job is `interrupted` instead of `blocked` or retried. The cost is one
  `resume`; the unsafe direction, closing a cut review, is excluded.
- The trust walk fails while a daemon is running: the launcher refuses every
  command, `stop` included. Restore the reported path or end the daemon with
  SIGTERM, which it handles as a stop.
- An upgrade changes the snapshot: every profile refuses to load until
  `init --approve-install`. `doctor` names both digests.
- Rollback: stop the daemon and reinstall 0.3.4. Its queue schema lacks the
  journal tables, so start it with a new state directory. The 0.3.5 store
  migrates a 0.3.4 queue in place and keeps its observations and pending
  requests. Remove `install_sha256` from profiles used with 0.3.4.

## Latency budget

Scanning is linear in the output and bounded by the 1 MiB output limit. The
coordinator polls reviewer output every 50 ms, so a completed finding is
committed within about 100 ms plus one SQLite transaction. The snapshot reads
the kit packages and rules once per process (under 1 MiB in total); measure it
with `doctor`, which must stay under one second on a local disk.

## Memory budget

One attempt keeps at most 1 MiB of decoded output in memory and the same
amount in the queue until it closes. The snapshot keeps one copy of the kit
sources and rules per process. Concurrency is bounded by `max_jobs`.

## Test plan

Regression tests replay both 0.3.4 release-check scenarios: a stop with a
finding cut before its closing brace, a stop past the size limit with a
complete finding, a stray quote before a cut document, a finding cut inside a
value, the real supervisor cancelling a partial document, the same
SHA and a successor SHA after an interruption, a group-writable `project.toml`,
a changed default under an unchanged profile, and a renamed rule file. Further
tests assert that the journal rejects deletes, that a job with an open attempt
is not dispatched, that `resume` and `--inherit-from` carry findings, that a
0.3.4 queue migrates, and that the launcher imports only snapshot bytes.
