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
TOKENS = re.compile(r'"(?:[^"\\]++|\\[\s\S])*+(?:(")|\\?\Z)|[][{}]')
ARRAY_START = re.compile(r'[ \t\r\n]*:[ \t\r\n]*(?=\[)')
# After a key, more input may still turn the following text into an array start.
UNDECIDED = re.compile(r'[ \t\r\n]*(?::[ \t\r\n]*)?\Z')


@dataclass(slots=True)
class Container:
    findings_array: bool
    finding_object: bool
    parts: list[str]
    start: int = 0


def protected_entry(item):
    """The stored form of a valid safety, honesty or security finding, or None."""
    try:
        entry = finding(item)
    except ValueError:
        return None
    if entry["risk"] not in BLOCKING:
        return None
    if valid_class(item.get("finding_class")):
        entry["finding_class"] = item["finding_class"]
    return entry


def decode(text):
    return json.loads(text, object_pairs_hook=unique_fields, parse_constant=invalid_constant)


class Scanner:
    """Recover protected findings from reviewer output while it is still arriving.

    Each container is decoded once, validating descendants even in a truncated
    document. `feed` returns the findings completed by the new text. After
    `finish`, `settled` tells whether the recovered list is known to be whole:
    the output holds no JSON object at all, or it is exactly one complete JSON
    document. Quote parity in surrounding prose can hide a structure from the
    token scan, so balanced brackets alone never settle an output. `unread`
    holds the text of each finding object that was printed but could not be
    stored: one that is invalid, or one the output ended inside.
    """

    def __init__(self):
        self.text = ""
        self.settled = False
        self.unread = []
        self._position = 0
        self._cursor = 0
        self._stack = []
        self._findings_array_at = None

    def feed(self, text):
        self.text += text
        return self._advance(final=False)

    def finish(self):
        found = self._advance(final=True)
        body = self.text.strip()
        self.settled = "{" not in body or whole_document(body)
        entry = None
        if self._stack and self._stack[-1].finding_object and not self._open_string:
            # The output stopped inside a finding whose fields are already complete.
            frame = self._stack[-1]
            rest = self.text[self._cursor:].rstrip(" \t\r\n").removesuffix(",")
            try:
                entry = protected_entry(decode("".join(frame.parts) + rest + "}"))
            except ValueError:
                pass
        if entry:
            found.append(entry)
        else:
            self.unread += [self.text[frame.start:] for frame in self._stack if frame.finding_object][:1]
        return found

    def _advance(self, final):
        raw, stack, found = self.text, self._stack, []
        self._open_string = False
        for match in TOKENS.finditer(raw, self._position):
            token = match[0]
            if token.startswith('"'):
                if match[1] is None:
                    # Unterminated: wait for the rest, or report it at the end.
                    self._open_string = True
                    break
                after_key = ARRAY_START.match(raw, match.end())
                if not after_key and not final and UNDECIDED.match(raw, match.end()):
                    break
                if after_key and json_field_is_findings(token):
                    self._findings_array_at = after_key.end()
                self._position = match.end()
                continue
            self._position = match.end()
            if stack:
                stack[-1].parts.append(raw[self._cursor:match.start()])
            self._cursor = match.end()
            if token in ("{", "["):
                is_finding = token == "{" and bool(stack) and stack[-1].findings_array
                stack.append(Container(token == "[" and match.start() == self._findings_array_at,
                                       is_finding, [token], match.start()))
            elif stack:
                frame = stack.pop()
                frame.parts.append(token)
                # The parent needs only the type and validity of this child. All
                # protected finding fields are strings. Replacing a consumed child
                # keeps work linear for findings arrays nested inside one another.
                try:
                    value = decode("".join(frame.parts))
                except ValueError:
                    value, replacement = None, "invalid"
                else:
                    replacement = "{}" if isinstance(value, dict) else "[]"
                if frame.finding_object:
                    if entry := protected_entry(value):
                        found.append(entry)
                    elif not isinstance(value, dict) or value.get("risk") != "other":
                        # Not a valid ordinary finding either: it may have been a protected one.
                        self.unread.append(raw[frame.start:match.end()])
                if stack:
                    stack[-1].parts.append(replacement)
        return found


def whole_document(text):
    try:
        json.loads(text)
    except (ValueError, RecursionError):
        return False
    return True


def scan(raw):
    """Return (protected findings, settled) for one complete reading of reviewer output."""
    scanner = Scanner()
    found = scanner.feed(raw)
    found += scanner.finish()
    return found, scanner.settled


def salvage(raw):
    """Keep valid protected fields, including entries before the truncation point."""
    return scan(raw)[0]


def json_field_is_findings(token):
    try:
        return json.loads(token) == "findings"
    except ValueError:
        return False
