"""Calendar grounding from clinic and owned-appointment dates, never host time."""
from datetime import date
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
