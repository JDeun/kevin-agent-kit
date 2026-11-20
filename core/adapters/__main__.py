"""Command line entry point for the hosting adapters."""

import argparse
import json
from pathlib import Path
import sys

from . import AdapterError, HostingAdapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m core.adapters", description=__doc__, allow_abbrev=False,
    )
    parser.add_argument("provider", choices=("github", "gitlab"))
    parser.add_argument("--repo", required=True, metavar="PATH", help="repository path, including owner or group")
    parser.add_argument("--host", help="hostname override for a self-managed instance")
    operations = parser.add_subparsers(dest="operation", required=True)

    create = operations.add_parser("create", allow_abbrev=False, help="create a pull or merge request")
    create.add_argument("--title", required=True)
    create.add_argument("--head", required=True)
    create.add_argument("--base", required=True)
    create.add_argument("--body-file", type=Path, required=True)

    for name, description in (
        ("update-body", "replace the description"),
        ("comment", "post a top-level comment"),
        ("pipeline-status", "read head checks or the head pipeline"),
    ):
        operation = operations.add_parser(name, allow_abbrev=False, help=description)
        operation.add_argument("--number", type=int, required=True)
        if name != "pipeline-status":
            operation.add_argument("--body-file", type=Path, required=True)

    gate = operations.add_parser("pipeline-gate", allow_abbrev=False, help="require exact successful CI evidence")
    gate.add_argument("--number", required=True, type=int)
    gate.add_argument("--sha", required=True)
    gate.add_argument("--target", required=True)
    gate.add_argument("--base-sha", required=True)
    gate.add_argument("--run-id", required=True, type=int, help="GitHub check suite or GitLab pipeline ID")
    gate.add_argument("--source", required=True, help="GitHub app slug or GitLab pipeline source")
    gate.add_argument("--require", required=True, action="append", metavar="CHECK")

    args = vars(parser.parse_args(argv))
    provider = args.pop("provider")
    repo = args.pop("repo")
    host = args.pop("host")
    operation = args.pop("operation")
    try:
        adapter = HostingAdapter(provider, repo, host)
        method = {
            "create": adapter.create,
            "update-body": adapter.update_body,
            "comment": adapter.comment,
            "pipeline-status": adapter.pipeline_status,
            "pipeline-gate": adapter.pipeline_gate,
        }[operation]
        result = method(**args)
    except AdapterError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
