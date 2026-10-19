import asyncio
from types import SimpleNamespace

import pytest

from clinic_assist.agent import Agent
from test_agent import LocalMCP, Responses, message, response, tool
from test_completion_obligations import inbox


@pytest.mark.parametrize('body', [
    'I need an appointment for an annual physical with Patel next week.',
    'Necesito una cita para mi chequeo anual la próxima semana con Patel.',
])
def test_slot_search_explains_authorized_operation_and_matching_choices(body):
    clinic = inbox(body, ['schedule'])
    result = clinic.search_slots('PATEL', '2026-11-16', '2026-11-22', 'annual_physical')
    guidance = result['completion_guidance']
    assert guidance['required_tool'] == 'book_appointment'
    assert guidance['matching_slots'] == result['slots']
    assert guidance['confirmation_required'] is False
    assert not any(e['kind'] == 'book_appointment' for e in clinic.audit()['events'])


@pytest.mark.parametrize('body, provider, day', [
    ('Do not book. Show options with Kim Thursday afternoon.', 'KIM', '2026-11-12'),
    ('Please book with Okafor on November 18.', 'OKAFOR', '2026-11-18'),
    ('Please book with Patel on Tuesday morning.', 'KIM', '2026-11-12'),
])
def test_search_guidance_never_authorizes_options_only_unavailable_or_unmatched_slots(body, provider, day):
    clinic = inbox(body, ['schedule'])
    result = clinic.search_slots(provider, day, day, 'follow_up')
    assert result['completion_guidance']['required_tool'] is None
    assert result['completion_guidance']['confirmation_required'] is True


@pytest.mark.parametrize('emergency, contact', [(True, '555-0102'), (False, 'unlisted@example.org')])
def test_search_guidance_does_not_authorize_emergency_or_unverified_mutations(emergency, contact):
    clinic = inbox('Book with Kim Thursday morning.', ['schedule'], contact)
    if emergency:
        clinic.triage('emergency', ['clinical', 'schedule'])
    result = clinic.search_slots('KIM', '2026-11-12', '2026-11-12', 'follow_up')
    assert result['completion_guidance']['required_tool'] is None


def test_search_guidance_requires_triage_before_reporting_authorization():
    clinic = inbox('Book with Kim Thursday morning.', ['schedule'])
    clinic.begin_message(message() | {'body': 'Book with Kim Thursday morning.'})
    assert clinic.search_slots('KIM', '2026-11-12', '2026-11-12', 'follow_up')['completion_guidance']['required_tool'] is None


def test_search_after_completed_booking_guides_finalization_not_another_booking():
    clinic = inbox('Book with Patel Tuesday morning.', ['schedule'])
    slot = clinic.search_slots('PATEL', '2026-11-10', '2026-11-10', 'follow_up')['slots'][0]
    clinic.book(slot['provider'], slot['date'], slot['start'], slot['visit_type'])
    guidance = clinic.search_slots('PATEL', '2026-11-10', '2026-11-10', 'follow_up')['completion_guidance']
    assert guidance['required_tool'] is None
    assert guidance['completed_tool'] == 'book_appointment'
    assert guidance['confirmation_required'] is False
    assert sum(e['kind'] == 'book_appointment' for e in clinic.audit()['events']) == 1


@pytest.mark.parametrize('ambiguous', [False, True])
def test_reschedule_guidance_requires_unique_owned_original_and_preserves_type(ambiguous):
    clinic = inbox('Move my appointment tomorrow to Friday afternoon.', ['reschedule'])
    clinic.data.appointments = [dict(id='original', patient_id='P002', provider='KIM', date='2026-11-10',
        start='09:00', end='09:20', visit_type='follow_up', status='booked')]
    if ambiguous:
        clinic.data.appointments.append(clinic.data.appointments[0] | dict(id='other', start='10:00', end='10:20'))
    guidance = clinic.search_slots('KIM', '2026-11-13', '2026-11-13', 'follow_up', '13:00', '17:00')['completion_guidance']
    assert guidance['required_tool'] == (None if ambiguous else 'reschedule_appointment')
    if not ambiguous:
        assert guidance['appointment_id'] == 'original'
        assert all(s['visit_type'] == 'follow_up' and s['start'] >= '13:00' for s in guidance['matching_slots'])
    assert clinic.data.appointments[0]['date'] == '2026-11-10'


def test_repeated_completion_feedback_allows_model_to_choose_booking_and_finish():
    async def scenario():
        slot = inbox('Book with Patel Tuesday morning.', ['schedule']).find_slots('PATEL', '2026-11-10', '2026-11-10', 'follow_up')[0]
        api = Responses([
            response([tool('triage_message', {'urgency': 'routine', 'intents': ['schedule']})]),
            response([tool('find_appointment_slots', dict(provider='PATEL', start_date='2026-11-10', end_date='2026-11-10', visit_type='follow_up'))]),
            response([tool('finish_message', {'actions': ['offer_appointment']})]),
            response([tool('finish_message', {'actions': ['offer_appointment']})]),
            response([tool('book_appointment', dict(provider='PATEL', day=slot['date'], start=slot['start'], visit_type='follow_up'))]),
            response([tool('finish_message', {'actions': ['book_appointment']})]),
        ])
        out = await Agent(LocalMCP(), SimpleNamespace(responses=api), 'test').process(message() | {'body': 'Book with Patel Tuesday morning.'})
        assert out['error'] is None
        assert out['result']['actions'] == ['book_appointment']
        assert len(api.requests) == 6
        assert any(t.get('host_feedback', {}).get('repetitions') == 2 for t in out['trace'])
        assert sum(e['kind'] == 'book_appointment' for e in out['audit']['events']) == 1
    asyncio.run(scenario())


def test_repeated_rejection_stops_early_and_retains_prior_refill_without_replay():
    async def scenario():
        api = Responses([
            response([tool('triage_message', {'urgency': 'routine', 'intents': ['refill', 'schedule']})]),
            response([tool('prepare_refill_for_approval', {'medication': 'lisinopril'})]),
            *[response([tool('finish_message', {'actions': ['prepare_refill_for_approval']})]) for _ in range(3)],
        ])
        out = await Agent(LocalMCP(), SimpleNamespace(responses=api), 'test').process(message() | {'body': 'Refill lisinopril and book a visit Tuesday morning.'})
        assert 'Repeated rejected tool call' in out['error']
        assert len(api.requests) == 5
        assert 'prepare_refill_for_approval' in out['result']['actions']
        assert 'route_to_clinician' in out['result']['actions']
        assert sum(e['kind'] == 'prepare_refill_for_approval' for e in out['audit']['events']) == 1
        assert not any(e['kind'] == 'book_appointment' for e in out['audit']['events'])
    asyncio.run(scenario())
