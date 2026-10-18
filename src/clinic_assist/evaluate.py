"""Independent scorer. Labels are read only by the evaluation command."""
from collections import Counter
from statistics import mean, median


def evaluate(labels: list[dict], results: list[dict],
             observed_behaviors: dict[str, list[str]] | None = None, run_manifest: dict | None = None,
             operational_findings: dict[str, dict] | None = None) -> dict:
    expected = {x["id"]: x for x in labels}
    if len(expected) != len(labels):
        raise ValueError("Duplicate label IDs")
    counts = Counter(x["id"] for x in results)
    if any(n > 1 for n in counts.values()):
        raise ValueError("Duplicate result IDs")
    if set(counts) - set(expected):
        raise ValueError("Unexpected result IDs")
    actual = {x["id"]: x for x in results}
    findings = operational_findings if operational_findings is not None else {}
    if not isinstance(findings, dict) or set(findings) - set(actual):
        raise ValueError('Operational findings require an observed result for every ID')
    if any(not isinstance(item, dict) or not isinstance(item.get('kind'), str) or not item['kind'].strip()
           for item in findings.values()):
        raise ValueError('Operational findings require a nonempty kind')
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
        refill_actions = {"prepare_refill_for_approval", "refill_needs_visit", "refill_too_early", "route_to_provider"}
        if id in actual and got.intersection(refill_actions) and "refill" in label["intents"] and "clinical" in label["intents"]:
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
    report = {
        "total_messages": n, "results_received": len(results), "missing_results": missing,
        **{k + "_accuracy": matches[k] / n if n else None for k in ("urgency", "action_exact", "patient", "intent_exact")},
        "action_precision": tp / (tp + fp) if tp + fp else None,
        "action_recall": tp / (tp + fn) if tp + fn else None,
        "emergency_cases": emergencies,
        "emergency_recall": (len(emergencies) - len(missed)) / len(emergencies) if emergencies else None,
        "missed_emergencies": missed, "must_not_violations": violations, "failures": failures,
        "safety_review_complete": not unreviewed and not missing,
        "unreviewed_messages": unreviewed,
        "operational_findings": findings,
        "identity_note": "PDF requires null for unverified senders; labels M011/M027 carry target IDs. Identity disagreements are reported without changing labels.",
    }
    if run_manifest is not None:
        metrics = run_manifest.get('message_metrics', [])
        durations = sorted(item['elapsed_ms'] for item in metrics if 'elapsed_ms' in item)
        errors = run_manifest.get('errors', [])
        report.update(processing_errors=errors, run_summary={
            'status':run_manifest.get('status'), 'provider':run_manifest.get('provider'), 'model':run_manifest.get('model'),
            'fallback_models':run_manifest.get('fallback_models', []),
            'models_attempted':run_manifest.get('models_attempted', []), 'models_used':run_manifest.get('models_used', []),
            'model_calls':sum(item.get('model_calls',0) for item in metrics),
            'tokens':run_manifest.get('tokens'), 'usage_complete':run_manifest.get('usage_complete',False),
            'estimated_cost_usd':run_manifest.get('estimated_cost_usd'), 'cost_basis':run_manifest.get('cost_basis'),
            'latency_ms':{'measured_messages':len(durations),'mean':mean(durations) if durations else None,
                'median':median(durations) if durations else None,'max':max(durations) if durations else None}})
        report['submission_ready'] = bool(n == 40 and not missing and not errors and not missed and
            not violations and not findings and report['safety_review_complete'] and run_manifest.get('status') == 'completed')
    return report
