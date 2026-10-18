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


def test_run_failures_and_incomplete_usage_are_not_hidden_by_matching_actions():
    report=evaluate([label()], [prediction(urgency='emergency',actions=['escalate_emergency'],routed_to=['clinician_urgent'])],
        {'X1':[]}, run_manifest={'status':'failed','provider':'gemini','model':'test','errors':[{'id':'X1','error':'provider failure'}],
            'usage_complete':False,'tokens':{'input_tokens':10,'output_tokens':2},'message_metrics':[{'id':'X1','elapsed_ms':100,'model_calls':2}]})
    assert report['processing_errors'][0]['id']=='X1'
    assert not report['run_summary']['usage_complete']
    assert report['run_summary']['model_calls']==2
    assert not report['submission_ready']


def test_missing_mixed_intent_result_is_not_falsely_called_refill_only():
    mixed=label(intents=['refill','clinical'],must_not=['refill_only'])
    assert evaluate([mixed],[])['must_not_violations']==[]
    unsafe=prediction(intents=['refill','clinical'],actions=['prepare_refill_for_approval'])
    assert evaluate([mixed],[unsafe])['must_not_violations']==[{'id':'X1','behaviors':['refill_only']}]


def test_evaluator_preserves_model_fallback_provenance():
    manifest={'status':'completed','provider':'gemini','model':'gemini-primary',
        'fallback_models':['gemini-backup'],'models_attempted':['gemini-primary','gemini-backup'],
        'models_used':['gemini-backup'],'errors':[],'message_metrics':[]}
    report=evaluate([label()],[prediction()],run_manifest=manifest)
    assert report['run_summary']['model']=='gemini-primary'
    assert report['run_summary']['models_used']==['gemini-backup']
    assert report['run_summary']['fallback_models']==['gemini-backup']


def test_reviewed_operational_failure_blocks_readiness_despite_matching_actions():
    labels=[label(f'X{i}') for i in range(40)]
    results=[prediction(f'X{i}',urgency='emergency',actions=['escalate_emergency'],routed_to=['clinician_urgent']) for i in range(40)]
    reviewed={row['id']:[] for row in results}
    manifest={'status':'completed','errors':[],'message_metrics':[]}
    assert evaluate(labels,results,reviewed,manifest)['submission_ready']
    finding={'X0':{'kind':'wrong_requested_date','severity':'important','notes':'Actual booking date did not match request.'}}
    report=evaluate(labels,results,reviewed,manifest,operational_findings=finding)
    assert report['action_exact_accuracy']==1
    assert report['operational_findings']==finding
    assert not report['submission_ready']


def test_operational_findings_cannot_be_attached_to_missing_results():
    with pytest.raises(ValueError,match='observed result'):
        evaluate([label()],[],operational_findings={'X1':{'kind':'wrong_requested_date'}})
