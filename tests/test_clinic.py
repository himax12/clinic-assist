from pathlib import Path

import pytest

from clinic_assist.clinic import Clinic
from clinic_assist.data import ClinicData

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture
def clinic():
    data = ClinicData.load(DATA)
    data.appointments = []
    return Clinic(data)


def bind(clinic, contact="555-0102", name="Robert Chen", dob=None):
    clinic.begin_message(dict(id="unseen-1", from_name=name, from_contact=contact,
        body="Help please", channel="sms", received_at="2026-11-09T09:00:00-06:00", stated_dob=dob))
    clinic.triage("routine", ["admin"])


def test_wrong_dob_does_not_disclose_patient(clinic):
    bind(clinic, "unknown@example.com", dob="1971-11-20")
    assert clinic.find_patient() == {"verified": False, "patient_id": None}
    with pytest.raises(PermissionError):
        clinic.list_medications()


def test_name_dob_and_caregiver_verify_from_source(clinic):
    bind(clinic, "unknown@example.com", dob="1971-11-02")
    assert clinic.find_patient()["patient_id"] == "P002"
    bind(clinic, "555-0177", "Claire Ellis")
    assert clinic.find_patient()["patient_id"] == "P017"


def test_arbitrary_claimed_name_does_not_override_contact(clinic):
    bind(clinic, "555-0888", "Grace Park")
    assert clinic.find_patient()["patient_id"] is None


def test_refill_eligibility_and_no_prescription_mutation(clinic):
    bind(clinic)
    before = clinic.data.medications[1].copy()
    assert clinic.check_refill("lisinopril")["status"] == "eligible"
    request = clinic.prepare_refill("lisinopril")
    assert request["status"] == "pending_provider_approval"
    assert clinic.data.medications[1] == before
    assert clinic.prepare_refill("lisinopril")["id"] == request["id"]


@pytest.mark.parametrize("contact,name,drug,status", [
    ("555-0106", "Marcus Bell", "Xanax", "controlled"),
    ("555-0104", "Thomas Wright", "metformin", "needs_visit"),
    ("555-0108", "George Adams", "amlodipine", "no_refills"),
    ("555-0115", "Fatima Hassan", "omeprazole", "too_early"),
])
def test_ineligible_refills_cannot_be_prepared(clinic, contact, name, drug, status):
    bind(clinic, contact, name)
    assert clinic.check_refill(drug)["status"] == status
    with pytest.raises(ValueError):
        clinic.prepare_refill(drug)
    assert clinic.refill_requests == []


def test_refill_window_and_calendar_year_boundary(clinic):
    bind(clinic)
    med = clinic.data.medications[1]
    med.update(last_filled="2026-10-17", days_supply=30)
    assert clinic.check_refill("lisinopril")["status"] == "eligible"
    med["last_filled"] = "2026-10-18"
    assert clinic.check_refill("lisinopril")["earliest_request_date"] == "2026-11-10"
    p = next(p for p in clinic.data.patients if p["id"] == "P002")
    p["last_visit"] = "2025-11-09"
    med["last_filled"] = "2026-10-17"
    assert clinic.check_refill("lisinopril")["status"] == "eligible"
    p["last_visit"] = "2025-11-08"
    assert clinic.check_refill("lisinopril")["status"] == "needs_visit"


@pytest.mark.parametrize("provider,day,start", [
    ("OKAFOR", "2026-11-18", "14:00"),
    ("PATEL", "2026-11-26", "14:00"),
    ("PATEL", "2026-11-14", "14:00"),
    ("PATEL", "2026-11-10", "11:40"),
    ("PATEL", "2026-11-10", "16:40"),
    ("PATEL", "2026-11-10", "09:10"),
])
def test_invalid_slots_never_mutate(clinic, provider, day, start):
    bind(clinic)
    with pytest.raises(ValueError):
        clinic.book(provider, day, start, "annual_physical")
    assert clinic.data.appointments == []


def test_shared_state_prevents_double_book_and_cancel_releases_slot(clinic):
    bind(clinic)
    appointment = clinic.book("PATEL", "2026-11-10", "10:00", "annual_physical")
    with pytest.raises(ValueError, match="overlap"):
        clinic.book("PATEL", "2026-11-10", "10:20", "follow_up")
    clinic.cancel(appointment["id"])
    assert clinic.book("PATEL", "2026-11-10", "10:20", "follow_up")["status"] == "booked"


def test_failed_reschedule_preserves_original(clinic):
    bind(clinic)
    appt = clinic.book("PATEL", "2026-11-10", "10:00", "follow_up")
    with pytest.raises(ValueError):
        clinic.reschedule(appt["id"], "OKAFOR", "2026-11-18", "10:00")
    assert clinic.list_appointments()[0]["id"] == appt["id"]
    assert clinic.list_appointments()[0]["status"] == "booked"


def test_cannot_cancel_another_patient_appointment(clinic):
    bind(clinic)
    appt = clinic.book("PATEL", "2026-11-10", "10:00", "follow_up")
    bind(clinic, "555-0101", "Linda Morales")
    with pytest.raises(PermissionError):
        clinic.cancel(appt["id"])


def test_unverified_emergency_escalates_and_cannot_downgrade_or_book(clinic):
    bind(clinic, "unknown", "Unknown")
    clinic.triage("emergency", ["clinical"])
    assert clinic.routes[-1]["queue"] == "clinician_urgent"
    assert clinic.find_patient()["patient_id"] is None
    with pytest.raises(ValueError):
        clinic.triage("routine", ["admin"])
    with pytest.raises(PermissionError):
        clinic.book("PATEL", "2026-11-10", "10:00", "follow_up")
    assert clinic.draft_reply("Take extra medication", "English")["reply_draft"] == "Call 911 or go to the nearest emergency department. The clinic has been alerted."


def test_new_message_cannot_inherit_verification(clinic):
    bind(clinic)
    assert clinic.find_patient()["verified"]
    bind(clinic, "unknown", "Stranger")
    with pytest.raises(PermissionError):
        clinic.list_appointments()


def test_existing_pending_refill_is_acknowledged_for_new_message(clinic):
    bind(clinic)
    request = clinic.prepare_refill("lisinopril")
    bind(clinic)
    clinic.message.id = "unseen-2"
    assert clinic.prepare_refill("lisinopril")["id"] == request["id"]
    audit = clinic.audit()
    assert any(e["kind"] == "prepare_refill_for_approval" for e in audit["events"])
    assert audit["routes"][0]["queue"] == "provider_refills"
