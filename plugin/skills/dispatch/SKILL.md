---
name: dispatch
description: Send an existing implementation branch into the review queue.
---

Find the source branch and its full Git commit ID in the configured repository.
Run `kevin-agent-kit --config PATH enqueue BRANCH SHA`, passing each value as a separate argument.
Report the branch and commit ID. The coordinator runs separately. Use the status skill for progress.
