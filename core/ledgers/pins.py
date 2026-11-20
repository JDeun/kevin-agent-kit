from pathlib import Path

from .store import iso_date, read_records


def get_decision(ledger: str | Path, decision_id: str) -> dict:
    matches = [record for record in read_records(Path(ledger)) if record.get("id") == decision_id]
    if len(matches) != 1:
        raise ValueError(f"Expected one decision for {decision_id!r}, found {len(matches)}")
    decision = matches[0]
    for key in ("date", "decided_by", "source", "summary"):
        if not isinstance(decision.get(key), str) or not decision[key].strip():
            raise ValueError(f"Decision {decision_id!r} requires {key}")
    iso_date(decision["date"])
    return decision


def assert_decision_pin(actual, expected, *, ledger: str | Path, decision_id: str) -> None:
    """Check behavior against an attributed decision from a pytest test."""
    try:
        decision = get_decision(ledger, decision_id)
    except (OSError, ValueError) as error:
        raise AssertionError(f"Decision pin {decision_id!r}: {error}") from error
    if actual != expected:
        raise AssertionError(
            f"Changing decision {decision_id} requires a human decision "
            f"({decision['date']}, {decision['decided_by']}, "
            f"{decision['source']}): expected {expected!r}, got {actual!r}"
        )
