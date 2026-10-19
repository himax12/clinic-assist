"""Original message constraints must survive a mistaken model tool proposal."""
from datetime import date
from pathlib import Path

import pytest

from clinic_assist.clinic import Clinic
from clinic_assist.data import ClinicData


def bound(body, intents=None):
    c=Clinic(ClinicData.load(Path(__file__).resolve().parents[1]/'data'))
    c.data.appointments=[]
    c.begin_message(dict(id='unseen-consent',received_at='2026-11-09T09:00:00-06:00',
        channel='sms',from_name='Robert Chen',from_contact='555-0102',body=body))
    c.triage('routine',intents or ['schedule'])
    return c


@pytest.mark.parametrize('body',[
    'Do not book anything yet; please just show available options.',
    "Don't schedule it yet. I only want options.",
    'No reserve ninguna cita; solo quiero ver opciones.',
    'Please show me appointments, without booking anything.',
])
def test_explicit_withholding_of_consent_blocks_booking_without_numeric_time(body):
    c=bound(body)
    with pytest.raises(ValueError,match='consent|confirmation|permission'):
        c.book('KIM','2026-11-13','09:00','follow_up')
    assert c.data.appointments==[]


@pytest.mark.parametrize('provider,day,start',[
    ('PATEL','2026-11-12','14:00'),
    ('KIM','2026-11-13','14:00'),
    ('KIM','2026-11-12','14:20'),
])
def test_explicit_provider_date_and_time_all_match_before_mutation(provider,day,start):
    c=bound('Please book with Dr. Kim on Thursday November 12 at 2pm.')
    with pytest.raises(ValueError,match='requested|match'):
        c.book(provider,day,start,'follow_up')
    assert c.data.appointments==[]
    assert c.book('KIM','2026-11-12','14:00','follow_up')['provider']=='KIM'


@pytest.mark.parametrize('body,wrong_day,wrong_start,correct_day,correct_start',[
    ('Book next week Tuesday morning.','2026-11-20','15:00','2026-11-17','09:00'),
    ('Una cita el martes por la mañana la próxima semana.','2026-11-20','15:00','2026-11-17','09:00'),
    ('Book tomorrow afternoon.','2026-11-12','09:00','2026-11-10','13:00'),
    ('Book November18, please.','2026-11-23','09:00','2026-11-18','09:00'),
    ('Una cita el 18 de noviembre, por favor.','2026-11-23','09:00','2026-11-18','09:00'),
    ('Book on 2026-11-18.','2026-11-23','09:00','2026-11-18','09:00'),
])
def test_date_weekday_and_daypart_constraints(body,wrong_day,wrong_start,correct_day,correct_start):
    c=bound(body)
    with pytest.raises(ValueError,match='requested|match'):
        c.book('PATEL',wrong_day,wrong_start,'follow_up')
    assert c.book('PATEL',correct_day,correct_start,'follow_up')['date']==correct_day


def test_reschedule_validates_destination_not_original_day():
    c=bound('Move my appointment on Wednesday to Friday afternoon.',['reschedule'])
    c.data.appointments=[dict(id='owned',patient_id='P002',provider='KIM',date='2026-11-11',
        start='09:00',end='09:20',visit_type='follow_up',status='booked')]
    with pytest.raises(ValueError,match='requested|match'):
        c.reschedule('owned','KIM','2026-11-12','13:00')
    assert c._owned('owned')['date']=='2026-11-11'
    assert c.reschedule('owned','KIM','2026-11-13','13:00')['date']=='2026-11-13'


def test_no_cancel_consent_and_ambiguous_owned_bookings_are_unchanged():
    c=bound('Do not cancel my appointments; just show them.',['cancel'])
    c.data.appointments=[dict(id='one',patient_id='P002',provider='KIM',date='2026-11-10',
        start='09:00',end='09:20',visit_type='follow_up',status='booked')]
    with pytest.raises(ValueError,match='consent|permission'):
        c.cancel('one')
    assert c._owned('one')['status']=='booked'


def test_original_appointment_is_not_arbitrarily_cancelled_when_two_match():
    c=bound('Please cancel my appointment tomorrow.',['cancel'])
    c.data.appointments=[dict(id=id,patient_id='P002',provider='KIM',date='2026-11-10',
        start=start,end=end,visit_type='follow_up',status='booked')
        for id,start,end in [('one','09:00','09:20'),('two','10:00','10:20')]]
    with pytest.raises(ValueError,match='clarif|ambiguous'):
        c.cancel('one')
    assert all(a['status']=='booked' for a in c.data.appointments)


def test_explicit_cancel_time_selects_one_of_two_owned_bookings():
    c=bound('Please cancel my appointment tomorrow at 10am.',['cancel'])
    c.data.appointments=[dict(id=id,patient_id='P002',provider='KIM',date='2026-11-10',
        start=start,end=end,visit_type='follow_up',status='booked')
        for id,start,end in [('one','09:00','09:20'),('two','10:00','10:20')]]
    with pytest.raises(ValueError,match='requested|match'):
        c.cancel('one')
    assert c.cancel('two')['status']=='cancelled'


def test_weekday_alone_does_not_authorize_another_week():
    c=bound('Please book this Friday afternoon.')
    with pytest.raises(ValueError,match='requested'):
        c.book('KIM','2026-11-20','13:00','follow_up')
    assert c.book('KIM','2026-11-13','13:00','follow_up')['date']=='2026-11-13'


def test_spanish_tomorrow_morning_retains_both_constraints():
    c=bound('Una cita mañana por la mañana, por favor.')
    with pytest.raises(ValueError,match='requested'):
        c.book('KIM','2026-11-11','09:00','follow_up')
    assert c.book('KIM','2026-11-10','09:00','follow_up')['date']=='2026-11-10'


def test_reschedule_cannot_change_a_different_owned_original():
    c=bound('Move my appointment on Wednesday to Friday afternoon.',['reschedule'])
    c.data.appointments=[dict(id=id,patient_id='P002',provider='KIM',date=day,
        start='09:00',end='09:20',visit_type='follow_up',status='booked')
        for id,day in [('wed','2026-11-11'),('thu','2026-11-12')]]
    with pytest.raises(ValueError,match='requested|clarif'):
        c.reschedule('thu','KIM','2026-11-13','13:00')
    assert c.reschedule('wed','KIM','2026-11-13','13:00')['date']=='2026-11-13'


def test_reschedule_original_ordinal_day_is_not_grounded_to_current_week():
    c=bound('Move my annual physical from Monday the 16th to any morning the following week.',['reschedule'])
    c.data.appointments=[dict(id='physical',patient_id='P002',provider='PATEL',date='2026-11-16',
        start='09:00',end='09:40',visit_type='annual_physical',status='booked')]
    assert c.reschedule('physical','PATEL','2026-11-23','09:00')['date']=='2026-11-23'


@pytest.mark.parametrize('body',[
    'I cannot attend Thursday. Please book another day.',
    'Please book with anybody except Dr. Kim.',
    'Please show me available appointments.',
    'I cannot attend 2026-11-12. Please book another day.',
    'I cannot attend afternoons. Please book an appointment.',
])
def test_negated_calendar_and_availability_inquiries_do_not_authorize_mutation(body):
    c=bound(body)
    with pytest.raises(ValueError,match='clarif|consent|requested'):
        c.book('KIM','2026-11-12','13:00','follow_up')
    assert not c.data.appointments


def test_plural_mornings_does_not_allow_an_afternoon_slot():
    c=bound('Book this week; mornings work best.')
    with pytest.raises(ValueError,match='requested'):
        c.book('KIM','2026-11-12','13:00','follow_up')
    assert c.book('KIM','2026-11-12','09:00','follow_up')['start']=='09:00'


def test_reschedule_requires_unique_original_even_without_a_date_reference():
    c=bound('Please reschedule my appointment to November 17.',['reschedule'])
    c.data.appointments=[dict(id=id,patient_id='P002',provider='PATEL',date=day,
        start='09:00',end='09:20',visit_type='follow_up',status='booked')
        for id,day in [('one','2026-11-10'),('two','2026-11-12')]]
    with pytest.raises(ValueError,match='clarif|ambiguous'):
        c.reschedule('two','PATEL','2026-11-17','09:00')
    assert c._owned('two')['date']=='2026-11-12'


def test_named_owned_booking_does_not_authorize_cancelling_another_owned_booking():
    c=bound('Cancel appointment TWO tomorrow.',['cancel'])
    c.data.appointments=[dict(id=id,patient_id='P002',provider='KIM',date='2026-11-10',
        start=start,end=end,visit_type='follow_up',status='booked')
        for id,start,end in [('ONE','09:00','09:20'),('TWO','10:00','10:20')]]
    with pytest.raises(ValueError,match='requested|clarif'):
        c.cancel('ONE')
    assert c.cancel('TWO')['status']=='cancelled'


def test_requested_physical_cannot_be_shortened_to_a_follow_up():
    c=bound('Please book an annual physical with Kim on Thursday morning.')
    with pytest.raises(ValueError,match='requested'):
        c.book('KIM','2026-11-12','09:00','follow_up')
    assert c.book('KIM','2026-11-12','09:00','annual_physical')['end']=='09:40'


def test_conditional_unavailability_does_not_negate_an_explicit_requested_time():
    c=bound('Book with Okafor November18 at 10am. If unavailable, show alternatives.')
    c.search_slots('OKAFOR','2026-11-18','2026-11-18','follow_up')
    assert c._unavailable_requests
    assert c.finish_message(['offer_alternatives'])['actions']==['offer_alternatives']


def test_time_without_space_after_at_is_still_enforced():
    c=bound('Book with Kim this Thursday at2pm.')
    with pytest.raises(ValueError,match='requested time'):
        c.book('KIM','2026-11-12','14:20','follow_up')
    assert c.book('KIM','2026-11-12','14:00','follow_up')['start']=='14:00'
