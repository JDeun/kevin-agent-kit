# Review repeated mistakes and scan agent inputs

Run the commands from the kit root with Python 3.11 or later.

## Mistake records

Give incidents with the same cause a stable key. Use an evidence reference unique to the occurrence, such as a review comment or a failed run. Record a short summary and the kind of automated check that could have caught it.

```sh
python3 -m guards mistake record --key missing-export-schema \
  --evidence 'https://example.org/reviews/17' \
  --summary 'Build accepted an export with no schema' --check test
python3 -m guards mistake record --key missing-export-schema \
  --evidence 'https://example.org/reviews/25' \
  --summary 'A second export omitted its schema' --check ci
python3 -m guards mistake candidates
```

The default file is `mistakes.jsonl`. Each occurrence is recorded once per key and evidence reference, so retrying the same record is safe. The date defaults to the current UTC date and can be set with `--date`. The candidate list includes keys with at least two distinct references, with the reported check types and references. Use `--minimum` to raise that threshold.

Before promoting a candidate, check that the references show a common failure and that the proposed check would catch it reliably. Choose the narrowest check with enough context to detect the problem.

| Check | When it fits |
| --- | --- |
| Lint | The problem is visible in source or configuration without running it. |
| Test | A repeatable behavior or invariant can be checked using a representative input. |
| CI | Detection requires branch history, a built artifact, or a check across files or jobs. |

Keep intent, policy trade-offs, and judgments that require case-specific context in prose.
If a proposed check would rely on disputed labels or frequent exceptions, describe the
judgment and link to evidence until a stable invariant emerges.

## Agent security scan

```sh
./guards/agent-sec --root . core guards
python3 -m guards agent-sec scan --root . core guards
python3 -m guards agent-sec scan --root . --require-complete core guards
```

Pass files or directories under the root. With no paths the command scans the root. The static checks look for common credential patterns, sensitive literal assignments, destructive commands, downloaded scripts piped to a shell, and unrestricted values in known scope settings. The prompt check flags common cases where input, file contents, or tool output reach a Python prompt or message field. It also flags common template interpolations in prompt-like fields.

Use each finding's rule, relative path, and line to review the code. The JSON output omits matched values and source text. The scan reads UTF-8 text files of up to 2 MiB, skips common metadata and dependency directories, and skips symlinks during traversal. Review prompt findings in context, including who controls the input, which message role receives it, and which actions the receiving agent can take. The scan uses static heuristics and can miss indirect flows or unfamiliar settings.

Files it could not inspect (too large, binary, not UTF-8, a Python file that does not parse, a symlink, an excluded directory) are printed as a `coverage: incomplete` JSON object on stderr. An empty findings list therefore does not mean everything was covered; `--require-complete` makes the command exit 2 when anything was not inspected.

Exit code 0 means the scan found no matches. Exit code 1 means it reported findings. Exit code 2 means it could not complete the requested scan.
