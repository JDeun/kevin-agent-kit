import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from core.ledgers.store import iso_date, nonempty, read_records, transact

from .security import scan


CHECKS = ("lint", "test", "ci")


def record_mistake(args: argparse.Namespace) -> int:
    row = {
        "key": nonempty(args.key), "evidence": nonempty(args.evidence),
        "summary": nonempty(args.summary), "check": args.check,
        "date": iso_date(args.date),
    }

    def add(records: list[dict]) -> dict | None:
        if any(
            (record.get("key"), record.get("evidence")) == (row["key"], row["evidence"])
            for record in records
        ):
            return None
        return row

    print(json.dumps({"recorded": transact(args.ledger, add) is not None}))
    return 0


def candidates(args: argparse.Namespace) -> int:
    seen = defaultdict(dict)
    for row in read_records(args.ledger):
        key, evidence, check = row.get("key"), row.get("evidence"), row.get("check")
        if (
            not isinstance(key, str) or not nonempty(key)
            or not isinstance(evidence, str) or not nonempty(evidence)
            or check not in CHECKS
        ):
            raise ValueError("A mistake record needs a key, evidence and a valid check type")
        seen[key][evidence] = check
    matches = [
        {
            "key": key, "occurrences": len(events),
            "checks": sorted(set(events.values())), "evidence": sorted(events),
        }
        for key, events in sorted(seen.items())
        if len(events) >= args.minimum
    ]
    print(json.dumps(matches, sort_keys=True))
    return 0


def agent_sec(args: argparse.Namespace) -> int:
    try:
        findings = scan(args.root, args.paths)
    except OSError as error:
        raise ValueError("Unable to read a requested scan path") from error
    print(json.dumps(findings, sort_keys=True))
    return int(bool(findings))


def minimum_count(value: str) -> int:
    count = int(value)
    if count < 2:
        raise argparse.ArgumentTypeError("Must be at least two")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Review repeat mistakes and scan agent inputs.")
    groups = parser.add_subparsers(dest="group", required=True)
    mistake = groups.add_parser("mistake").add_subparsers(dest="action", required=True)
    command = mistake.add_parser("record")
    command.add_argument("--ledger", type=Path, default=Path("mistakes.jsonl"))
    command.add_argument("--key", required=True)
    command.add_argument("--summary", required=True)
    command.add_argument("--evidence", required=True)
    command.add_argument("--check", choices=CHECKS, required=True)
    command.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat())
    command.set_defaults(handler=record_mistake)

    command = mistake.add_parser("candidates")
    command.add_argument("--ledger", type=Path, default=Path("mistakes.jsonl"))
    command.add_argument("--minimum", type=minimum_count, default=2)
    command.set_defaults(handler=candidates)

    security = groups.add_parser("agent-sec").add_subparsers(required=True)
    command = security.add_parser("scan")
    command.add_argument("--root", type=Path, default=Path("."))
    command.add_argument("paths", type=Path, nargs="*")
    command.set_defaults(handler=agent_sec)

    args = parser.parse_args()
    try:
        return args.handler(args)
    except (OSError, ValueError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    sys.exit(main())
