---
status: accepted
date: 2026-11-20
---

# Bind global rule changes to local ownership records

The CLI inserts rule snapshots in the human's Claude and Codex files. The
same text can occur in a personal example, and the user can edit a snapshot
in place. The CLI needs evidence that a block still matches its own write
before removing or replacing it.

Use project-specific marker pairs and an installation ID for each block.
Store the full-block digest for each client in the kit's private
`~/.local/state/<kit>/rule-ownership/` directory. The command checks this
record before changing either target. The record accepts the old and new
states during an atomic file exchange so an interrupted invocation can
recognize either state. Existing backups retain their XDG location.

Earlier installs have no ownership record. The operator can inspect each
legacy block and explicitly approve its exact digest via
`--adopt-rules-sha256`. An edited owned block stops setup. Copy personal
additions outside the block and restore the recorded version before trying
again. Renamed kit builds manage their own blocks.
