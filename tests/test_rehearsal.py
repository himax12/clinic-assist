"""Unseen-message CLI rehearsal with a scripted model and real MCP subprocess.

This tests execution and policy boundaries, not Gemini's understanding.
"""
import asyncio
from argparse import Namespace
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace

from clinic_assist.cli import run
from test_agent import Responses, response, tool

ROOT=Path(__file__).resolve().parents[1]


def test_unseen_cli_rehearsal_with_real_mcp_and_scripted_model(tmp_path, monkeypatch):
    messages=[]
    for id, name, contact, body in [
        ('NEW-EMERGENCY','Unknown','unlisted','Tengo dolor de pecho y no puedo respirar.'),
        ('NEW-PRIVATE','Robert Chen','unlisted','Please show my medication list.'),
        ('NEW-MIXED','Robert Chen','555-0102','Please refill lisinopril. I have also been dizzy since yesterday.'),
        ('NEW-LEAVE','Robert Chen','555-0102','Book with OKAFOR November 18 at 10am. If unavailable, show alternatives.'),
    ]:
        messages.append(dict(id=id,from_name=name,from_contact=contact,body=body,
            channel='sms',received_at='2026-11-09T09:00:00-06:00'))
    source=tmp_path/'unseen.json'
    source.write_text(json.dumps(messages))
    api=Responses([
        response([tool('triage_message',{'urgency':'emergency','intents':['clinical'],'response_language':'Spanish'})]),
        response([tool('triage_message',{'urgency':'routine','intents':['records']})]),
        response([tool('find_patient',{})]),
        response([tool('finish_message',{'actions':['request_verification']})]),
        response([tool('triage_message',{'urgency':'urgent','intents':['refill','clinical']})]),
        response([tool('route_to_human',{'queue':'clinician_urgent','reason':'Dizziness with refill request'})]),
        response([tool('prepare_refill_for_approval',{'medication':'lisinopril'})]),
        response([tool('finish_message',{'actions':['prepare_refill_for_approval','route_to_clinician']})]),
        response([tool('triage_message',{'urgency':'routine','intents':['schedule']})]),
        response([tool('find_appointment_slots',{'provider':'OKAFOR','start_date':'2026-11-18','end_date':'2026-11-18','visit_type':'follow_up'})]),
        response([tool('find_appointment_slots',{'provider':'OKAFOR','start_date':'2026-11-23','end_date':'2026-11-23','visit_type':'follow_up'})]),
        response([tool('finish_message',{'actions':['offer_alternatives']})]),
    ])

    @asynccontextmanager
    async def client(*args, **kwargs):
        yield SimpleNamespace(responses=api)

    monkeypatch.setenv('GEMINI_API_KEY','offline-test-not-sent')
    monkeypatch.setattr('clinic_assist.providers.model_client',client)
    output=tmp_path/'rehearsal'
    args=Namespace(messages=source,data=ROOT/'data',output=output,limit=None,model='scripted-offline',provider='gemini',request_interval=0)
    assert asyncio.run(run(args))==0
    results=[json.loads(line) for line in (output/'results.jsonl').read_text().splitlines()]
    assert results[0]['urgency']=='emergency'
    assert results[0]['reply_draft'].startswith('Llame al 911')
    assert results[1]['patient_id'] is None
    assert results[1]['actions']==['request_verification']
    assert set(results[2]['actions'])=={'prepare_refill_for_approval','route_to_clinician'}
    assert 'book_appointment' not in results[3]['actions']
    assert '2026-11-23' in results[3]['reply_draft']
    assert '2026-11-18' not in results[3]['reply_draft']
