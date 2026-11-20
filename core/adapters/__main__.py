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
        }[operation]
        result = method(**args)
    except AdapterError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
