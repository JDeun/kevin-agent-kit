# Worker rules

- Use the assigned repository, branch, and worktree. Check the expected base
  and head before editing and inspect the diff before committing. Stage
  files by path and include only work from the assignment. In a loop-managed
  repair checkout, edit the approved paths so the coordinator can commit.
- Push only the designated branch and only when authorized. Use
  fast-forward pushes. Force pushes, rebases, stashes, and history rewrites
  are outside worker scope. Leave updates to other branches and protected
  targets to their owners and the project's release process.
- Run the smallest useful verification first. Use at most two pytest-xdist
  workers (`-n 2`) and keep CPU, memory, and test service use within the
  assignment's limits. Allocate distinct ports and state directories for
  concurrent workers.
- Record test servers, containers, and temporary worktrees in the resource
  ledger when you create them. Record the exact PID or container ID, owner,
  and cleanup action. Stop only resources you started and verified you
  still own. Use the recorded PID or ID, never a process-name pattern.
  Retain evidence and ask the owner before deleting shared resources.
- Record decisions in the decision ledger with the alternatives, rationale,
  evidence, decision owner, and any remaining gate. Bring reviewers the
  change scope, exact base and head, verification output, and known open
  issues. For a branch you own, enqueue the exact current SHA with
  `kevin-agent-kit enqueue BRANCH FULL_SHA`. The coordinator includes approved
  repairs in the candidate bundle.
- Reserve shared ordinals and ports through the resource ledger before using
  them. Run the migration check against the current target for numbered changes.
- Before research or design, read the wiki map and related synthesis pages
  (`wiki_dir`, default `docs/research`). Land findings in a synthesis page with
  cited sources and enqueue research with `--kind research`.
- Use the project's configured `max_model` for assignments that need the
  maximum-capability model. Choose a smaller model for a routine role when
  it meets the task's needs. Confirm any increase with the project owner.
