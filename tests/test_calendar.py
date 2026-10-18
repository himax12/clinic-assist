"""Calendar grounding from clinic and owned-appointment dates, never host time."""
from datetime import date
import copy
from pathlib import Path

import pytest

from clinic_assist.clinic import Clinic
from clinic_assist.data import ClinicData

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('reference,next_start,next_end,tuesday',[
    ('2026-11-09','2026-11-16','2026-11-22','2026-11-17'),
    ('2026-12-31','2027-01-04','2027-01-10','2027-01-05'),
    ('2024-02-29','2024-03-04','2024-03-10','2024-03-05'),
])
def test_calendar_context_resolves_calendar_weeks(reference,next_start,next_end,tuesday):
    from clinic_assist.data import calendar_context
    context=calendar_context(date.fromisoformat(reference))
    assert context['reference_date']==reference
    assert context['next_week']['start_date']==next_start
    assert context['next_week']['end_date']==next_end
    assert context['next_week']['weekdays']['Tue']==tuesday


def test_public_policy_calendar_uses_configured_clinic_date_and_returns_copy():
    clinic=Clinic(ClinicData.load(ROOT/'data'))
    policy=clinic.public_policy()
    assert policy['date_context']['reference_date']=='2026-11-09'
    assert policy['date_context']['next_week']['weekdays']['Tue']=='2026-11-17'
    assert 'date_context' not in clinic.data.clinic
    policy['hours']['open']='00:00'
    assert clinic.data.clinic['hours']['open']=='09:00'


def test_appointment_following_week_is_relative_to_original_booking():
    clinic=Clinic(ClinicData.load(ROOT/'data'))
    clinic.begin_message(dict(id='calendar-regression',received_at='2026-11-09T10:18:00-06:00',
        channel='portal',from_name='Fatima Hassan',from_contact='fatima.hassan@example.com',body='Move my physical to the following week.'))
    appointments=clinic.list_appointments()
    physical=next(a for a in appointments if a['date']=='2026-11-16' and a['visit_type']=='annual_physical')
    assert physical['following_week']=={'start_date':'2026-11-23','end_date':'2026-11-29'}
    assert all('following_week' not in a for a in clinic.data.appointments)


@pytest.mark.parametrize('body',[
    'Move my annual physical to any morning the following week.',
    'Mueva mi cita a la semana siguiente, por favor.',
])
def test_explicit_following_week_rejects_wrong_reschedule_atomically(body):
    clinic=Clinic(ClinicData.load(ROOT/'data'))
    clinic.begin_message(dict(id='relative-date',received_at='2026-11-09T10:18:00-06:00',
        channel='portal',from_name='Fatima Hassan',from_contact='fatima.hassan@example.com',body=body))
    clinic.triage('routine',['reschedule'])
    original=copy.deepcopy(clinic._owned('A073'))
    with pytest.raises(ValueError,match='2026-11-23.*2026-11-29'):
        clinic.reschedule('A073','PATEL','2026-11-17','09:00')
    assert clinic._owned('A073')==original
    assert not any(e['kind']=='reschedule_appointment' for e in clinic.events)
    moved=clinic.reschedule('A073','PATEL','2026-11-23','09:00')
    assert moved['date']=='2026-11-23'
    assert moved['visit_type']=='annual_physical'


@pytest.mark.parametrize('body',[
    'Book a follow-up next week, preferably Tuesday morning.',
    'Una cita la próxima semana, por favor.',
])
def test_explicit_next_week_booking_rejects_this_week_before_mutation(body):
    clinic=Clinic(ClinicData.load(ROOT/'data'))
    clinic.data.appointments=[]
    clinic.begin_message(dict(id='relative-date',received_at='2026-11-09T10:18:00-06:00',
        channel='sms',from_name='Robert Chen',from_contact='555-0102',body=body))
    clinic.triage('routine',['schedule'])
    with pytest.raises(ValueError,match='2026-11-16.*2026-11-22'):
        clinic.book('PATEL','2026-11-10','09:00','follow_up')
    assert clinic.data.appointments==[]
    assert clinic.book('PATEL','2026-11-17','09:00','follow_up')['date']=='2026-11-17'


def test_following_week_guard_uses_selected_booking_in_another_month():
    clinic=Clinic(ClinicData.load(ROOT/'data'))
    clinic.data.appointments=[dict(id='original',patient_id='P002',provider='OKAFOR',date='2026-12-07',
        start='09:00',end='09:20',visit_type='follow_up',status='booked')]
    clinic.begin_message(dict(id='unseen-relative-date',received_at='2026-11-09T10:18:00-06:00',
        channel='sms',from_name='Robert Chen',from_contact='555-0102',body='Move it to the following week.'))
    clinic.triage('routine',['reschedule'])
    with pytest.raises(ValueError,match='2026-12-14.*2026-12-20'):
        clinic.reschedule('original','OKAFOR','2026-11-17','09:00')
    assert clinic.reschedule('original','OKAFOR','2026-12-14','09:00')['date']=='2026-12-14'


@pytest.mark.parametrize('body',[
    'Do not book next week; book this week instead.',
    'I cannot come next week.',
    "Next week doesn't work for me.",
    'Next week doesn’t work for me.',
    'I can’t come next week.',
    'No puedo ir la próxima semana.',
    'Move it to the following week or next week; I am unsure.',
])
def test_negative_or_conflicting_week_mentions_require_clarification(body):
    clinic=Clinic(ClinicData.load(ROOT/'data'))
    clinic.data.appointments=[]
    clinic.begin_message(dict(id='negative-week',received_at='2026-11-09T10:18:00-06:00',
        channel='sms',from_name='Robert Chen',from_contact='555-0102',body=body))
    clinic.triage('routine',['schedule'])
    for day in ('2026-11-10','2026-11-17'):
        with pytest.raises(ValueError,match='clarification'):
            clinic.book('PATEL',day,'09:00','follow_up')
    assert clinic.data.appointments==[]
