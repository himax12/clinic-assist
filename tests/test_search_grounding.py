"""An unrelated or narrow empty query cannot stand in for the sender's request."""
from pathlib import Path

import pytest

from clinic_assist.clinic import Clinic
from clinic_assist.data import ClinicData

ROOT = Path(__file__).resolve().parents[1]


def inbox(body):
    clinic = Clinic(ClinicData.load(ROOT / 'data'))
    clinic.begin_message(dict(id='search-grounding', channel='sms', from_name='Robert Chen',
        from_contact='555-0102', body=body, received_at='2026-11-09T09:00:00-06:00'))
    clinic.triage('routine', ['schedule'])
    return clinic


def test_unrelated_empty_sunday_query_cannot_exempt_tuesday_booking():
    clinic = inbox('Please book an appointment with Patel Tuesday morning.')
    assert not clinic.search_slots('PATEL', '2026-11-15', '2026-11-15', 'follow_up')['slots']
    assert clinic.search_slots('PATEL', '2026-11-10', '2026-11-10', 'follow_up')['slots']
    with pytest.raises(ValueError, match='book'):
        clinic.finish_message(['offer_appointment'])


def test_narrow_empty_query_cannot_exempt_broad_morning_request():
    clinic = inbox('Please book an appointment with Patel Tuesday morning.')
    clinic.data.appointments.append(dict(id='occupied', patient_id='P003', provider='PATEL',
        date='2026-11-10', start='09:00', end='09:20', visit_type='follow_up', status='booked'))
    assert not clinic.search_slots('PATEL', '2026-11-10', '2026-11-10', 'follow_up',
                                  '09:00', '09:20')['slots']
    clinic.search_slots('PATEL', '2026-11-10', '2026-11-10', 'follow_up')
    with pytest.raises(ValueError, match='book'):
        clinic.finish_message(['offer_appointment'])


def test_provider_leave_for_whole_requested_week_permits_actual_alternatives():
    clinic = inbox('Please book a follow-up with Okafor next week.')
    result = clinic.search_slots('OKAFOR', '2026-11-16', '2026-11-22', 'follow_up')
    assert not result['slots'] and result['alternatives']
    assert clinic.finish_message(['offer_alternatives'])['actions'] == ['offer_alternatives']


def test_exact_occupied_request_permits_narrow_search_confirmation_barrier():
    clinic = inbox('Please book Patel on November 10 at 2 PM.')
    clinic.data.appointments.append(dict(id='occupied', patient_id='P003', provider='PATEL',
        date='2026-11-10', start='14:00', end='14:20', visit_type='follow_up', status='booked'))
    result = clinic.search_slots('PATEL', '2026-11-10', '2026-11-10', 'follow_up', '14:00', '14:20')
    assert not result['slots'] and result['alternatives']
    assert clinic.finish_message(['offer_alternatives'])['actions'] == ['offer_alternatives']


def test_unrelated_empty_query_does_not_block_later_matching_booking():
    clinic = inbox('Please book an appointment with Patel Tuesday morning.')
    clinic.search_slots('PATEL', '2026-11-15', '2026-11-15', 'follow_up')
    slot = clinic.search_slots('PATEL', '2026-11-10', '2026-11-10', 'follow_up')['slots'][0]
    appointment = clinic.book(slot['provider'], slot['date'], slot['start'], slot['visit_type'])
    assert appointment['status'] == 'booked'
    assert clinic.finish_message(['book_appointment'])['actions'] == ['book_appointment']


def test_wrong_provider_empty_query_cannot_exempt_requested_provider():
    clinic = inbox('Please book an appointment with Patel next week.')
    clinic.search_slots('OKAFOR', '2026-11-16', '2026-11-22', 'follow_up')
    clinic.search_slots('PATEL', '2026-11-16', '2026-11-22', 'follow_up')
    with pytest.raises(ValueError, match='book'):
        clinic.finish_message(['offer_appointment'])
