import fcntl
import json
import os
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from stat import S_IMODE


def read_records(path: Path) -> list[dict]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    records = []
    for number, line in enumerate(content.split("\n"), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSON in {path} at line {number}") from error
        if not isinstance(record, dict):
            raise ValueError(f"Expected an object in {path} at line {number}")
        records.append(record)
    return records


def transact(path: Path, new_record: Callable[[list[dict]], dict | None]) -> dict | None:
    """Read and optionally write one record while holding the shared lock."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = read_records(path)
        record = new_record(records)
        if record is None:
            return None

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, encoding="utf-8", mode="w", delete=False
            ) as output:
                temp_path = Path(output.name)
                if path.exists():
                    os.fchmod(output.fileno(), S_IMODE(path.stat().st_mode))
                records.append(record)
                for row in records:
                    output.write(json.dumps(row, sort_keys=True) + "\n")
                output.flush()
                os.fsync(output.fileno())
            temp_path.replace(path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return record


def nonempty(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Value must contain text")
    return cleaned


def iso_date(value: str) -> str:
    try:
        canonical = date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError("Date must be a calendar date in YYYY-MM-DD form") from error
    if value != canonical:
        raise ValueError("Date must be a calendar date in YYYY-MM-DD form")
    return canonical
