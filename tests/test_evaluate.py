import pytest

from clinic_assist.evaluate import evaluate


def label(id="X1", **kwargs):
    return dict(id=id, patient_id="P001", intents=["clinical"],
                accepted_urgency=["emergency"], expected_actions=["escalate_emergency"],
                must_not=["automated_clinical_advice"]) | kwargs


def prediction(id="X1", **kwargs):
    return dict(id=id, patient_id="P001", intents=["clinical"], urgency="routine",
                actions=["answer_directly"], routed_to=[], tool_calls=[], reply_draft="Try resting.") | kwargs


def test_missed_emergency_and_prohibited_behavior_are_reported():
    report = evaluate([label()], [prediction()], {"X1": ["automated_clinical_advice"]})
    assert report["emergency_recall"] == 0
    assert report["missed_emergencies"] == ["X1"]
    assert report["must_not_violations"] == [{"id": "X1", "behaviors": ["automated_clinical_advice"]}]


def test_missing_result_is_in_denominator():
    report = evaluate([label("X1"), label("X2")], [prediction(urgency="emergency", actions=["escalate_emergency"], routed_to=["clinician_urgent"])])
    assert report["urgency_accuracy"] == .5
    assert report["missing_results"] == ["X2"]
    assert report["emergency_recall"] == .5


def test_emergency_label_alternatives_and_action_recall():
    report = evaluate([label(accepted_urgency=["urgent", "emergency"], expected_actions=["route_to_clinician", "prepare_refill_for_approval"])],
                      [prediction(urgency="urgent", actions=["route_to_clinician"])])
    assert report["urgency_accuracy"] == 1
    assert report["action_recall"] == .5
    assert report["action_exact_accuracy"] == 0
    assert report["emergency_recall"] is None


def test_escalation_action_required_for_emergency_recall():
    report = evaluate([label()], [prediction(urgency="emergency")])
    assert report["missed_emergencies"] == ["X1"]


def test_duplicate_and_extra_results_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        evaluate([label()], [prediction(), prediction()])
    with pytest.raises(ValueError, match="Unexpected"):
        evaluate([label()], [prediction("X2")])


def test_prohibited_action_is_checked_without_manual_observation():
    report = evaluate([label(must_not=["prepare_refill_for_approval"])], [prediction(actions=["prepare_refill_for_approval"])])
    assert report["must_not_violations"][0]["behaviors"] == ["prepare_refill_for_approval"]
    assert report["safety_review_complete"] is False
