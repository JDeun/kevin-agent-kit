"""Convert one broker client's model verdict into a coordinator receipt."""

from ..verdict import FINDING_ID, findings_by_id, nonempty, review_object, salvage


def review_verdict(text, request, tool):
    receipt = {"request": request, "tool_version": tool, "reviewed_paths": request["paths"]}
    try:
        verdict = review_object(text)
        if set(verdict) != {"status", "findings", "resolved"} or verdict["status"] != "complete":
            raise ValueError("expected a complete model verdict with exactly status, findings and resolved")
        items = findings_by_id(verdict["findings"], request.get("finding_class_required"))
        resolved = verdict.get("resolved")
        if not isinstance(resolved, dict) or any(
                not FINDING_ID.fullmatch(key) or not nonempty(value) for key, value in resolved.items()):
            raise ValueError("resolved must map finding IDs to nonempty evidence")
    except ValueError as error:
        return {**receipt, "status": "partial", "diagnostics": [str(error)], "findings": salvage(text), "resolved": {}}
    return {**receipt, "status": "complete", "diagnostics": [], "findings": list(items.values()), "resolved": resolved}
