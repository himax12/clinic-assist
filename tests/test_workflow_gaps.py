"""Regressions for observed failures, plus unseen inputs for the same boundaries."""
import copy
from pathlib import Path

import pytest

from clinic_assist.clinic import Clinic
from clinic_assist.data import ClinicData

DATA=Path(__file__).resolve().parents[1]/'data'


def bound(body, intents=None, urgency='routine'):
    clinic=Clinic(ClinicData.load(DATA))
    clinic.begin_message(dict(id='workflow-regression',received_at='2026-11-09T10:00:00-06:00',
        channel='sms',from_name='Robert Chen',from_contact='555-0102',body=body))
    clinic.triage(urgency,intents or ['schedule'])
    return clinic


@pytest.mark.parametrize('body,requested,substitute',[
    ('I want an appointment Thursday at 2pm.','14:00','14:20'),
    ('Book me at 10:40 AM.','10:40','11:00'),
    ('Una cita a las 15:20, por favor.','15:20','15:40'),
    ('Please book 2:20 p.m.','14:20','14:40'),
])
def test_booking_cannot_substitute_an_explicit_time(body,requested,substitute):
    clinic=bound(body)
    clinic.data.appointments=[]
    with pytest.raises(ValueError,match='confirmation|requested time'):
        clinic.book('PATEL','2026-11-12',substitute,'follow_up')
    assert clinic.data.appointments==[]
    assert not any(e['kind']=='book_appointment' for e in clinic.events)
    assert clinic.book('PATEL','2026-11-12',requested,'follow_up')['start']==requested


def test_reschedule_time_rejection_leaves_original_untouched():
    clinic=bound('Move my appointment to Friday at 2pm.',['reschedule'])
    clinic.data.appointments=[dict(id='owned',patient_id='P002',provider='PATEL',date='2026-11-10',
        start='09:00',end='09:20',visit_type='follow_up',status='booked')]
    original=copy.deepcopy(clinic.data.appointments)
    with pytest.raises(ValueError,match='confirmation|requested time'):
        clinic.reschedule('owned','PATEL','2026-11-13','14:20')
    assert clinic.data.appointments==original


@pytest.mark.parametrize('body',[
    'Do not book at 2pm; I only want options.',
    'I can’t come at 2pm.',
    'No reserve una cita a las 14:00.',
    'I am unsure whether 2pm or 3pm works.',
])
def test_rejected_or_ambiguous_times_do_not_authorize_booking(body):
    clinic=bound(body);clinic.data.appointments=[]
    with pytest.raises(ValueError,match='clarification|confirmation'):
        clinic.book('PATEL','2026-11-12','14:00','follow_up')
    assert clinic.data.appointments==[]


def test_broad_morning_request_still_allows_matching_slot():
    clinic=bound('Book a follow-up Tuesday morning.');clinic.data.appointments=[]
    assert clinic.book('PATEL','2026-11-10','10:00','follow_up')['start']=='10:00'


def test_empty_leave_search_returns_real_following_options_and_reply():
    clinic=bound('Could I see Dr Okafor on November18? Please offer alternatives if away.')
    result=clinic.search_slots('OKAFOR','2026-11-18','2026-11-18','follow_up')
    assert result['slots']==[]
    assert result['alternatives']
    assert result['alternatives'][0]['date']=='2026-11-23'
    reply=clinic.finish_message(['offer_alternatives'])['reply_draft']
    assert '2026-11-23' in reply and 'OKAFOR' in reply
    assert not any(e['kind']=='book_appointment' for e in clinic.events)


def test_researching_a_fallback_date_cannot_manufacture_sender_confirmation():
    clinic=bound('Book with Okafor November18. Offer alternatives if away.')
    clinic.search_slots('OKAFOR','2026-11-18','2026-11-18','follow_up')
    clinic.search_slots('OKAFOR','2026-11-23','2026-11-23','follow_up')
    with pytest.raises(ValueError,match='confirmation'):
        clinic.book('OKAFOR','2026-11-23','09:20','follow_up')
    assert not any(e['kind']=='book_appointment' for e in clinic.events)
    clinic.begin_message(dict(id='confirmed-new-message',received_at='2026-11-09T10:00:00-06:00',
        channel='sms',from_name='Robert Chen',from_contact='555-0102',body='Book with Okafor November23 at9:20am.'))
    clinic.triage('routine',['schedule'])
    assert clinic.book('OKAFOR','2026-11-23','09:20','follow_up')['date']=='2026-11-23'


@pytest.mark.parametrize('body,start',[
    ('Book an appointment after 2pm.','14:20'),
    ('Una cita después de las 14:00.','14:20'),
    ('An appointment before 2pm.','13:20'),
])
def test_explicit_time_boundaries_still_allow_matching_slots(body,start):
    clinic=bound(body);clinic.data.appointments=[]
    assert clinic.book('PATEL','2026-11-12',start,'follow_up')['start']==start


def test_exhausted_search_cannot_claim_an_appointment_offer():
    clinic=bound('Please find an appointment.')
    for provider in clinic.data.clinic['providers']:
        provider['leave']=[['2026-11-09','2027-12-31']]
    result=clinic.search_slots('PATEL','2026-11-12','2026-11-12','follow_up')
    assert result['slots']==result['alternatives']==[]
    with pytest.raises(ValueError,match='available slots'):
        clinic.finish_message(['offer_alternatives'])
    clinic.route('front_desk','No slots within bounded search')
    assert clinic.finish_message(['route_to_front_desk'])['actions']==['route_to_front_desk']


def test_urgent_clinical_triage_records_handoff_without_another_tool_call():
    clinic=bound('Refill request and a separate clinical concern.',['refill','clinical'],'urgent')
    assert {r['queue'] for r in clinic.audit()['routes']}=={'clinician_urgent'}
    result=clinic.finish_message(['acknowledge'])
    assert 'route_to_clinician' in result['actions']
    assert 'routed to a clinician' in result['reply_draft']
    clinic.triage('urgent',['clinical'])
    assert len(clinic.audit()['routes'])==1


def test_urgent_administrative_request_does_not_create_clinical_route():
    clinic=bound('Please call about my bill today.',['billing'],'urgent')
    assert clinic.audit()['routes']==[]


def test_a_booked_search_result_cannot_be_advertised_as_still_available():
    clinic=bound('Book a follow-up.');clinic.data.appointments=[]
    slot=clinic.find_slots('PATEL','2026-11-12','2026-11-12','follow_up',limit=1)[0]
    clinic.book('PATEL',slot['date'],slot['start'],'follow_up')
    with pytest.raises(ValueError,match='available slots|search again'):
        clinic.finish_message(['offer_alternatives'])
