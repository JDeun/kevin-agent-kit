"""Language-neutral architecture fitness baseline."""

from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import PurePosixPath


def relative_path(value):
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or str(path) != value or "\x00" in value:
        raise ValueError(f"expected a normalized repository-relative path: {value!r}")
    return path


@dataclass(frozen=True, order=True)
class Violation:
    rule: str
    path: str
    subject: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in (self.rule, self.path, self.subject)):
            raise ValueError("violation rule, path and subject must be nonempty strings")
        relative_path(self.path)


def read_counts(contents):
    rows = json.loads(contents)
    if not isinstance(rows, list):
        raise ValueError("expected a JSON array of violation counts")
    counts = Counter()
    for row in rows:
        if not isinstance(row, dict) or row.keys() != {"rule", "path", "subject", "count"}:
            raise ValueError("each violation requires rule, path, subject and count")
        violation = Violation(row["rule"], row["path"], row["subject"])
        count = row["count"]
        if type(count) is not int or count < 1 or violation in counts:
            raise ValueError("violation counts must be positive and identities unique")
        counts[violation] = count
    return counts


def rows(counts):
    return [{**asdict(violation), "count": count} for violation, count in sorted(counts.items())]


def compare(observed, proposed, target):
    return {
        "new_violations": rows(observed - proposed),
        "baseline_growth": rows(proposed - target),
        "retired_violations": rows(proposed - observed),
    }
