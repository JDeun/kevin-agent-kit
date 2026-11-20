import argparse
import json
import sys
from pathlib import Path

from .migrations import check_migrations
from .pins import get_decision
from .store import iso_date, nonempty, transact


def record_decision(args: argparse.Namespace) -> int:
    decision = {
        "id": nonempty(args.id), "date": iso_date(args.date), "decided_by": nonempty(args.decided_by),
        "source": nonempty(args.source), "summary": nonempty(args.summary),
    }

    def add(records: list[dict]) -> dict:
        if any(row.get("id") == decision["id"] for row in records):
            raise ValueError(f"Decision {decision['id']!r} already exists")
        return decision

    print(json.dumps(transact(args.ledger, add), sort_keys=True))
    return 0


def show_decision(args: argparse.Namespace) -> int:
    print(json.dumps(get_decision(args.ledger, args.id), sort_keys=True))
    return 0


def reserve(args: argparse.Namespace) -> int:
    resource = nonempty(args.resource)
    if args.after < 0:
        raise ValueError("--after must be zero or higher")

    def add(records: list[dict]) -> dict:
        highest = args.after
        for row in records:
            if row.get("resource") == resource:
                number = row.get("ordinal")
                if type(number) is not int or number < 1:
                    raise ValueError(f"Invalid recorded ordinal for {resource!r}")
                highest = max(highest, number)
        return {"resource": resource, "ordinal": highest + 1}

    print(transact(args.ledger, add)["ordinal"])
    return 0


def run_migration_check(args: argparse.Namespace) -> int:
    issues = check_migrations(args.repo, args.target, args.head, args.directory, args.pattern)
    for issue in issues:
        print(issue, file=sys.stderr)
    return int(bool(issues))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage decisions and shared ordinal reservations.")
    groups = parser.add_subparsers(dest="group", required=True)
    decision = groups.add_parser("decision").add_subparsers(dest="action", required=True)
    for name, handler in (("record", record_decision), ("show", show_decision)):
        command = decision.add_parser(name)
        command.add_argument("--ledger", type=Path, default=Path("decisions.jsonl"))
        command.add_argument("--id", required=True)
        if name == "record":
            for field in ("date", "decided-by", "source", "summary"):
                command.add_argument(f"--{field}", required=True)
        command.set_defaults(handler=handler)

    ordinal = groups.add_parser("ordinal").add_subparsers(dest="action", required=True)
    command = ordinal.add_parser("reserve", help="allocate the next number for a resource")
    command.add_argument("--ledger", type=Path, required=True)
    command.add_argument("--resource", required=True)
    command.add_argument("--after", type=int, default=0)
    command.set_defaults(handler=reserve)

    command = ordinal.add_parser("check-migrations", help="validate migration numbers against a Git target")
    command.add_argument("--repo", type=Path, default=Path("."))
    command.add_argument("--target", required=True)
    command.add_argument("--head", default="HEAD")
    command.add_argument("--directory", default="migrations")
    command.add_argument("--pattern", default="*.sql")
    command.set_defaults(handler=run_migration_check)

    args = parser.parse_args()
    try:
        return args.handler(args)
    except (OSError, ValueError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    sys.exit(main())
