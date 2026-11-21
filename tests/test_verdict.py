"""Both reviewer boundaries reject ambiguous output and retain protected evidence."""

import json

import pytest

from core import agents
from core.loop.review import review_receipt

REQUEST = {"paths": ["a.txt"], "finding_class_required": True}
PROTECTED = {"id": "s1", "risk": "safety", "detail": "Fixture A.", "finding_class": "scope"}
SECOND = {**PROTECTED, "id": "s2", "risk": "honesty"}
CLEAN = {"status": "complete", "findings": [], "resolved": {}}


def rejected(text, boundary):
    if boundary == "model":
        partial = agents.review_verdict(text, REQUEST, "fixture")
        assert partial["status"] == "partial" and partial["diagnostics"] and partial["resolved"] == {}
        return partial["findings"]
    with pytest.raises(ValueError) as rejected:
        review_receipt(text, REQUEST, [])
    return rejected.value.salvaged


@pytest.mark.parametrize("boundary", ["model", "runner"])
@pytest.mark.parametrize("shape", ["adjacent", "wrapped", "nested", "duplicate", "truncated", "prose", "fenced",
                                   "nonfinite", "list-risk", "object-risk", "null-class", "invalid-detail",
                                   "invalid-diagnostics", "missing-required-class"])
def test_rejection_preserves_valid_protected_findings(boundary, shape):
    clean = CLEAN
    if boundary == "runner":
        clean = {**CLEAN, "request": REQUEST, "tool_version": "fixture", "reviewed_paths": ["a.txt"], "diagnostics": []}
    first = {**clean, "findings": [PROTECTED]}
    second = {**clean, "findings": [SECOND]}
    text = {
        "adjacent": json.dumps(first) + "\n" + json.dumps(second),
        "wrapped": json.dumps([first]),
        "nested": json.dumps({**clean, "example": first}),
        "duplicate": json.dumps(first)[:-1] + ', "findings":[]}',
        "truncated": json.dumps(first).split('"resolved"', 1)[0],
        "prose": "review follows\n" + json.dumps(first),
        "fenced": "```json\n" + json.dumps(first) + "\n```",
        "nonfinite": json.dumps({**first, "score": float("nan")}),
        "list-risk": json.dumps({**first, "findings": [{**SECOND, "risk": []}, PROTECTED]}),
        "object-risk": json.dumps({**first, "findings": [{**SECOND, "risk": {}}, PROTECTED]}),
        "null-class": json.dumps({**first, "findings": [PROTECTED, {**SECOND, "risk": "other", "finding_class": None}]}),
        "invalid-detail": json.dumps({**first, "findings": [PROTECTED, {**SECOND, "detail": {}}]}),
        "invalid-diagnostics": json.dumps({**first, "diagnostics": ["Review cut short."]}),
        "missing-required-class": json.dumps({**first, "findings": [PROTECTED, {"id": "x", "risk": "other", "detail": "d"}]}),
    }[shape]
    kept = rejected(text, boundary)
    assert kept == ([PROTECTED, SECOND] if shape == "adjacent" else [PROTECTED])


@pytest.mark.parametrize("extra", [{"diagnostics": []}, {"request": REQUEST}, {"tool_version": "model"},
                                   {"reviewed_paths": ["a.txt"]}, {"extra": "text"}])
def test_model_verdict_rejects_fields_outside_its_schema(extra):
    assert rejected(json.dumps({**CLEAN, **extra, "findings": [PROTECTED]}), "model") == [PROTECTED]


def test_protected_finding_named_findings_can_be_resolved():
    resolved = {"findings": "Removed the fixture from the changed path."}
    verdict = json.dumps({**CLEAN, "resolved": resolved})
    receipt = agents.review_verdict(verdict, REQUEST, "fixture")
    assert receipt["status"] == "complete"
    prior = [{**PROTECTED, "id": "findings"}]
    assert review_receipt(json.dumps(receipt), REQUEST, prior)["resolved"] == resolved


def test_recovery_decodes_nested_finding_data_in_proportion_to_its_size(monkeypatch):
    from core.verdict import salvage

    # Model diagnostics sometimes repeat earlier review objects inside one another.
    text = json.dumps({"findings": [PROTECTED]})
    for _ in range(24):
        text = '{"findings":[{"earlier":' + text + '}]}'
    raw = "incomplete review\n" + text
    loads = json.loads
    decoded_chars = 0

    def counted(text, **options):
        nonlocal decoded_chars
        decoded_chars += len(text)
        return loads(text, **options)

    monkeypatch.setattr(json, "loads", counted)
    assert salvage(raw) == [PROTECTED]
    assert decoded_chars <= 3 * len(raw)
