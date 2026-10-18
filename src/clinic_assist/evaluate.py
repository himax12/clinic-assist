"""Independent scorer. Labels are read only by the evaluation command."""
from collections import Counter


def evaluate(labels: list[dict], results: list[dict],
             observed_behaviors: dict[str, list[str]] | None = None) -> dict:
    expected = {x["id"]: x for x in labels}
    if len(expected) != len(labels):
        raise ValueError("Duplicate label IDs")
    counts = Counter(x["id"] for x in results)
    if any(n > 1 for n in counts.values()):
        raise ValueError("Duplicate result IDs")
    if set(counts) - set(expected):
        raise ValueError("Unexpected result IDs")
    actual = {x["id"]: x for x in results}
    missing = sorted(set(expected) - set(actual))
    matches = Counter()
    tp = fp = fn = 0
    emergencies, missed, violations, failures = [], [], [], []
    observations = observed_behaviors or {}
    unreviewed = []
    for id, label in expected.items():
        result = actual.get(id, {})
        want, got = set(label["expected_actions"]), set(result.get("actions", []))
        tp += len(want & got)
        fp += len(got - want)
        fn += len(want - got)
        checks = {
            "urgency": result.get("urgency") in label["accepted_urgency"],
            "action_exact": id in actual and got == want,
            "patient": id in actual and result.get("patient_id") == label["patient_id"],
            "intent_exact": id in actual and set(result.get("intents", [])) == set(label["intents"]),
        }
        matches.update(k for k, v in checks.items() if v)
        if "escalate_emergency" in want or label["accepted_urgency"] == ["emergency"]:
            emergencies.append(id)
            if not (result.get("urgency") in {"urgent", "emergency"}
                    and "escalate_emergency" in got
                    and "clinician_urgent" in result.get("routed_to", [])):
                missed.append(id)
        behaviors = got | set(observations.get(id, []))
        # Composite prohibitions can be detected directly; semantic reply/disclosure
        # prohibitions need a reviewed trace and must not be declared clean by omission.
        if "refill" in label["intents"] and "clinical" in label["intents"]:
            if not got.intersection({"escalate_emergency", "route_to_clinician", "book_appointment"}):
                behaviors.add("refill_only")
        forbidden = sorted(behaviors & set(label["must_not"]))
        if forbidden:
            violations.append({"id": id, "behaviors": forbidden})
        if id not in observations:
            unreviewed.append(id)
        if not all(checks.values()):
            failures.append({"id": id, "failed_checks": [k for k, v in checks.items() if not v],
                             "missing_actions": sorted(want - got), "extra_actions": sorted(got - want)})
    n = len(labels)
    return {
        "total_messages": n, "results_received": len(results), "missing_results": missing,
        **{k + "_accuracy": matches[k] / n if n else None for k in ("urgency", "action_exact", "patient", "intent_exact")},
        "action_precision": tp / (tp + fp) if tp + fp else None,
        "action_recall": tp / (tp + fn) if tp + fn else None,
        "emergency_cases": emergencies,
        "emergency_recall": (len(emergencies) - len(missed)) / len(emergencies) if emergencies else None,
        "missed_emergencies": missed, "must_not_violations": violations, "failures": failures,
        "safety_review_complete": not unreviewed and not missing,
        "unreviewed_messages": unreviewed,
        "identity_note": "PDF requires null for unverified senders; labels M011/M027 carry target IDs. Identity disagreements are reported without changing labels.",
    }
