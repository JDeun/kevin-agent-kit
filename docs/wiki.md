# Knowledge wiki

Research scattered across scratch files, issue notes and design documents gets
repeated, and nobody knows what the team currently believes. The wiki keeps
raw sources immutable, keeps one synthesis page per topic, and keeps a map.
It lives in the repository (`docs/research` by default), so it passes the same
review as code.

```text
docs/research/
  README.md          map: one link per synthesis page
  AGENTS.md          maintenance rules for agents
  <topic>.md         synthesis page with front matter
  sources/
    manifest.json    sha256 of every ingested source
    2026-01-31-notes.md
```

A synthesis page starts with `title`, `status` (`current`, `stale` or
`superseded`), `updated` (`YYYY-MM-DD`) and `sources`, a list of files under
`sources/`.

## Commands

```sh
kevin-agent-kit wiki init
kevin-agent-kit wiki ingest ./interview-notes.md --task-out /tmp/ingest-task.md
kevin-agent-kit wiki lint --task-out /tmp/lint-task.md
kevin-agent-kit wiki external https://example.com/org/repo.wiki.git
```

Pass `--dir` before the action to use another directory. All commands run in
the current checkout unless you pass `--root`.

- `init` creates the map, the rules and `sources/` and never overwrites
  an existing file.
- `ingest` copies the file to `sources/YYYY-MM-DD-slug.ext`, records its digest
  and prints a task for a worker agent: update the related synthesis pages and
  the map, and mark contradictions with a `> CONFLICT:` block instead of
  choosing a side. Sources are never replaced.
- `lint` runs deterministic checks and exits 1 on errors. It verifies front
  matter, that each page cites ingested sources, relative links, that sources
  still match their recorded digest, and that every page is linked from the
  map. Warnings cover unresolved conflicts and sources no page cites;
  `--strict` makes them errors. `--task-out` writes a second task for a
  reviewing agent that looks for contradictions, claims newer sources made
  stale and sentences without support. The agent reports; a person applies fixes.
- `external` clones a GitHub or GitLab wiki shallowly into a temporary
  directory and reports broken links and unlinked pages. It never writes to
  the hosted wiki. Apply its findings by hand.

## Loop integration

Workers read the map and the relevant pages before research or design (see
`rules/research.md`). Enqueue a research branch with `--kind research`. When
`wiki_landing = true` in the profile (the default), the loop holds such a
branch in `wiki-needed` until its diff edits a synthesis page under
`wiki_dir`; the map or a new source alone does not count. Change the directory
with `wiki_dir`. Set `wiki_landing = false` to disable the check. After a fix,
run `kevin-agent-kit retry JOB_ID`. Add `wiki lint` to CI with the examples
for [GitHub Actions](examples/wiki/github-actions.yml) or
[GitLab CI](examples/wiki/gitlab-ci.yml).
