"""JSON and finding validation shared by the model adapter and receipt reader."""

import json
import re
from dataclasses import dataclass

RISKS = ("safety", "honesty", "security", "other")
BLOCKING = frozenset(RISKS) - {"other"}
FINDING_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z")
FINDING_CLASS = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate JSON field")
    return fields


def invalid_constant(_):
    raise ValueError("invalid JSON constant")


def review_object(raw):
    verdict_objects = []

    def collect(pairs):
        fields = unique_fields(pairs)
        if "findings" in fields:
            verdict_objects.append(fields)
        return fields

    try:
        value = json.loads(raw, object_pairs_hook=collect, parse_constant=invalid_constant)
    except (ValueError, RecursionError):
        raise ValueError("review must be one unambiguous JSON object") from None
    if not isinstance(value, dict) or "findings" not in value:
        raise ValueError("expected one top-level review with findings")
    # Resolution keys are finding IDs. Even "findings" is a valid ID here.
    for obj in verdict_objects:
        if obj is value or (obj is value.get("resolved") and nonempty(obj["findings"])):
            continue
        raise ValueError("unexpected nested review with findings")
    return value


def finding(item):
    if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
            or not FINDING_ID.fullmatch(item["id"]) or item.get("risk") not in RISKS
            or not nonempty(item.get("detail"))):
        raise ValueError("invalid review finding")
    return {key: item[key] for key in ("id", "risk", "detail")}


def valid_class(category):
    return isinstance(category, str) and len(category) <= 80 and FINDING_CLASS.fullmatch(category)


def findings_by_id(items, require_class):
    if not isinstance(items, list):
        raise ValueError("findings must be an array")
    report = {}
    for item in items:
        entry = finding(item)
        if entry["id"] in report:
            raise ValueError("duplicate review finding")
        if require_class or "finding_class" in item:
            if not valid_class(item.get("finding_class")):
                raise ValueError("review findings require a valid finding_class")
            entry["finding_class"] = item["finding_class"]
        report[entry["id"]] = entry
    return report


# Quoted text is one token, so braces or a quoted example in a detail cannot
# introduce a new finding.
# Consume an unterminated string through EOF as well. Starting another match
# at each escaped quote would repeatedly traverse the same malformed suffix.
TOKENS = re.compile(r'"(?:[^"\\]++|\\[\s\S])*+(?:"|\\?\Z)|[][{}]')
ARRAY_START = re.compile(r'[ \t\r\n]*:[ \t\r\n]*(?=\[)')


@dataclass(slots=True)
class Container:
    findings_array: bool
    finding_object: bool
    parts: list[str]


def recovered_objects(raw):
    """Decode each container once, validating descendants even in a truncated document."""
    stack = []
    findings_array_at = None
    cursor = 0
    for match in TOKENS.finditer(raw):
        token = match[0]
        if token.startswith('"'):
            after_key = ARRAY_START.match(raw, match.end())
            if after_key and json_field_is_findings(token):
                findings_array_at = after_key.end()
            continue
        if stack:
            stack[-1].parts.append(raw[cursor:match.start()])
        cursor = match.end()
        if token in ("{", "["):
            is_finding = token == "{" and bool(stack) and stack[-1].findings_array
            stack.append(Container(token == "[" and match.start() == findings_array_at, is_finding, [token]))
        elif stack:
            frame = stack.pop()
            frame.parts.append(token)
            # The parent needs only the type and validity of this child. All
            # protected finding fields are strings. Replacing a consumed child
            # keeps work linear for findings arrays nested inside one another.
            try:
                value = json.loads("".join(frame.parts), object_pairs_hook=unique_fields,
                                   parse_constant=invalid_constant)
            except ValueError:
                replacement = "invalid"
            else:
                replacement = "{}" if isinstance(value, dict) else "[]"
                if frame.finding_object:
                    yield value
            if stack:
                stack[-1].parts.append(replacement)


def salvage(raw):
    """Keep valid protected fields, including entries before the truncation point."""
    kept = []
    for item in recovered_objects(raw):
        try:
            entry = finding(item)
        except ValueError:
            continue
        if entry["risk"] in BLOCKING:
            if valid_class(item.get("finding_class")):
                entry["finding_class"] = item["finding_class"]
            kept.append(entry)
    return kept


def json_field_is_findings(token):
    try:
        return json.loads(token) == "findings"
    except ValueError:
        return False
