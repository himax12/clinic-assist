import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from clinic_assist.contracts import MessageResult
from clinic_assist.data import ClinicData

DATA = Path(__file__).resolve().parents[1] / "data"


def result(**overrides):
    return dict(id="X1", patient_id=None, intents=["admin"], urgency="routine",
                actions=["answer_directly"], routed_to=[], tool_calls=[],
                reply_draft="Hours are 9 to 5.") | overrides


def test_runtime_data_and_fixed_clock():
    data = ClinicData.load(DATA)
    assert data.today.isoformat() == "2026-11-09"
    assert data.timezone.key == "America/Chicago"
    assert len(data.patients) == 25
    assert len(data.appointments) == 170
    assert not hasattr(data, "labels")


def test_unknown_patient_reference_is_rejected(tmp_path):
    for name in ("clinic", "patients", "medications", "appointments"):
        value = json.loads((DATA / f"{name}.json").read_text())
        if name == "medications":
            value[0]["patient_id"] = "MISSING"
        (tmp_path / f"{name}.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="patient"):
        ClinicData.load(tmp_path)


@pytest.mark.parametrize("changes", [
    {"actions": ["approve_refill"]}, {"urgency": "low"},
    {"routed_to": ["unknown"]}, {"id": ""}, {"extra_field": True},
    {"intents": ["medication_refill"]},
])
def test_invalid_result_rejected(changes):
    with pytest.raises(ValidationError):
        MessageResult.model_validate(result(**changes))


def test_required_fields_and_strict_patient_identifier():
    value = result()
    del value["reply_draft"]
    with pytest.raises(ValidationError):
        MessageResult.model_validate(value)
    with pytest.raises(ValidationError):
        MessageResult.model_validate(result(patient_id="invented"))
