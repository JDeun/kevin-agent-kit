# Hosting adapters

Run `python3 -m core.adapters` from the repository root with Python 3.11 or later. Install and authenticate `gh` for GitHub or `glab` for GitLab. GitHub pipeline status uses `gh api --paginate --slurp`. The CLI account needs access to the target repository and permission for the operation. [GitHub CLI](https://cli.github.com/manual/gh_api) · [GitLab CLI](https://docs.gitlab.com/cli/api/)

`--repo` takes an explicit repository path such as `team/widgets` or a GitLab nested path such as `team/tools/widgets`. The defaults are `github.com` and `gitlab.com`. Set `--host` before the operation for a self-managed instance.

## Write a change request or comment

```sh
python3 -m core.adapters github --repo team/widgets create \
  --title "Document widget setup" --head docs/widgets --base main --body-file review.md
python3 -m core.adapters github --repo team/widgets update-body --number 42 --body-file review.md

python3 -m core.adapters gitlab --host gitlab.example.com --repo team/tools/widgets create \
  --title "Document widget setup" --head docs/widgets --base main --body-file review.md
printf '%s' 'I checked the setup steps.' > comment.md
python3 -m core.adapters gitlab --host gitlab.example.com --repo team/tools/widgets comment \
  --number 42 --body-file comment.md
```

Each body comes from a UTF-8 file. The adapter passes a JSON request to the provider CLI over standard input and reads the saved body back independently. A line beginning with a slash outside ordinary fenced code is rejected as a possible quick action. Put literal slash commands in inline code or a top-level fenced code block. Fence recognition is conservative for descriptions containing HTML. For GitLab comments, use LF line endings and omit trailing spaces or line breaks so the provider can store the file exactly. [GitLab quick actions](https://docs.gitlab.com/user/project/quick_actions/) · [GitLab note body normalization](https://docs.gitlab.com/api/notes/#note-body-normalization)

On success, create and update return a JSON object with the change `number` and `url`. Comment returns the change `number` and `comment_id`. If a write or its verification fails, check the target before retrying. The server may already have applied the write, and retrying create or comment can produce a duplicate.

## Read pipeline status

```sh
python3 -m core.adapters github --repo team/widgets pipeline-status --number 42
python3 -m core.adapters gitlab --repo team/tools/widgets pipeline-status --number 42
```

The result includes the change `number`, commit `sha`, and `status`. GitHub combines the head commit's reported check runs and commit statuses into `failed`, `pending`, `unknown`, or `success`, in that order of precedence. A commit with no checks or statuses reports `unknown`. The result covers the checks visible to the CLI account on that repository and commit. GitLab returns the merge request's head pipeline status and `pipeline_id`, or `unknown` with a null `pipeline_id` when the merge request has no head pipeline. A successful status lookup exits zero even when the checks or pipeline failed. [GitHub check runs](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference) · [GitHub combined status](https://docs.github.com/en/rest/commits/statuses#get-the-combined-status-for-a-specific-reference) · [GitLab merge requests](https://docs.gitlab.com/api/merge_requests/#get-single-merge-request)

## Gate the exact published result

Use `pipeline-gate` for an approval decision. Obtain the exact source SHA
and the current target SHA independently. Provide the approved source and
run ID, and repeat `--require` for each named mandatory check.

```sh
kevin-agent-kit hosting github --repo team/widgets pipeline-gate --number 42 \
  --sha "$candidate_sha" --target main --base-sha "$base_sha" \
  --run-id "$suite_id" --source github-actions --require unit --require integration
```

GitHub expects a check suite ID and app slug. GitLab expects the head
pipeline ID and pipeline source, such as `merge_request_event`. The gate
requires one successful result per required name and re-reads the change
and target afterward. Missing, ambiguous, neutral, and skipped checks fail
the gate. Configure the named checks and source using your trusted hosting
policy. [GitHub check runs](https://docs.github.com/en/rest/checks/runs) ·
[GitLab pipelines](https://docs.gitlab.com/api/pipelines/) · [GitLab jobs](https://docs.gitlab.com/api/jobs/)
