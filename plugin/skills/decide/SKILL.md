---
name: decide
description: Present and record a human choice for capped review findings.
---

Read the record with `kevin-agent-kit --config PATH accept-record ID`.
Use the interactive question tool to ask the human to choose **Revise** (repair and review again) or **Defer** (leave the branch unmerged).
Explain the expected consequence of each option using the record's findings. After the human answers, run
`kevin-agent-kit --config PATH decide ID CHOICE --source SOURCE` with the selected value and a human attribution.
