import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from clinic_assist.agent import Agent
from clinic_assist.clinic import Clinic
from clinic_assist.contracts import Message
from clinic_assist.data import ClinicData
from test_agent import LocalMCP, Responses, response, tool, message, final

ROOT = Path(__file__).resolve().parents[1]


def clinic():
    instance = Clinic(ClinicData.load(ROOT / 'data'))
    instance.begin_message(message())
    instance.triage('routine', ['refill'])
    return instance


def test_refill_needs_visit_requires_a_supporting_check():
    instance = clinic()
    assert instance.check_refill('lisinopril')['status'] == 'eligible'
    with pytest.raises(ValueError, match='needs.visit'):
        instance.draft_actions(['refill_needs_visit'])


def test_message_rejects_naive_timestamp():
    with pytest.raises(ValidationError):
        Message.model_validate(message() | {'received_at':'2026-11-09T09:00:00'})


def test_reply_uses_latest_slot_search():
    instance = clinic()
    instance._record('slot_search', slots=[{'date':'2026-11-10','start':'09:00','end':'09:20','provider':'PATEL'}])
    instance._record('slot_search', slots=[{'date':'2026-11-12','start':'14:00','end':'14:20','provider':'KIM'}])
    draft = instance.draft_actions(['offer_alternatives'])['reply_draft']
    assert '2026-11-12' in draft
    assert '2026-11-10' not in draft


def test_terminal_tool_completes_without_another_model_turn():
    async def scenario():
        api = Responses([
            response([tool('triage_message', {'urgency':'routine','intents':['refill']})]),
            response([tool('prepare_refill_for_approval', {'medication':'lisinopril'})]),
            response([tool('finish_message', {'actions':['prepare_refill_for_approval']})]),
        ])
        outcome = await Agent(LocalMCP(), SimpleNamespace(responses=api), 'test').process(message())
        assert outcome['error'] is None
        assert len(api.requests) == 3
        assert outcome['result']['actions'] == ['prepare_refill_for_approval']
        assert all('draft_reply' not in {t['name'] for t in request['tools']} for request in api.requests)
        assert 'finish_message' in outcome['result']['tool_calls']
    asyncio.run(scenario())


@pytest.mark.parametrize('emergency', [False, True])
def test_selected_spanish_language_survives_host_finalization(emergency):
    async def scenario():
        urgency = 'emergency' if emergency else 'routine'
        api = Responses([response([tool('triage_message', {'urgency':urgency,'intents':['clinical' if emergency else 'admin'],
            'response_language':'Spanish'})])]+([] if emergency else [response(text=final(actions=['acknowledge'],intents=['admin'],routed_to=[]))]))
        incoming = message() | {'body':'Me cuesta respirar.' if emergency else 'Gracias por su ayuda.'}
        outcome = await Agent(LocalMCP(), SimpleNamespace(responses=api), 'test').process(incoming)
        assert outcome['error'] is None
        assert ('Llame al 911' if emergency else 'Gracias') in outcome['result']['reply_draft']
        assert outcome['audit']['response_language'] == 'Spanish'
    asyncio.run(scenario())


def test_legacy_json_cannot_bypass_final_action_validation():
    async def scenario():
        api = Responses([response([tool('triage_message', {'urgency':'routine','intents':['refill']})]),
            response(text=final(actions=['refill_needs_visit'],routed_to=[]))])
        outcome = await Agent(LocalMCP(), SimpleNamespace(responses=api), 'test').process(message())
        assert outcome['error'] is not None
        assert 'visit is needed' not in outcome['result']['reply_draft']
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['timeout', 'server'])
def test_request_retry_does_not_repeat_an_executed_refill(failure):
    import json
    import httpx
    from openai import AsyncOpenAI
    from clinic_assist.providers import GeminiAdapter
    from test_providers import completion
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) >= 3:
            if failure == 'server':
                return httpx.Response(503, json={'error': {'message': 'Temporary unavailable'}})
            raise httpx.ReadTimeout('controlled timeout', request=request)
        name, args = ('triage_message', {'urgency':'routine','intents':['refill']}) if len(calls)==1 else ('prepare_refill_for_approval', {'medication':'lisinopril'})
        return httpx.Response(200,json=completion({'role':'assistant','content':None,'tool_calls':[
            {'id':str(len(calls)), 'type':'function', 'function':{'name':name,'arguments':json.dumps(args)}}]}))

    async def sleep(delay):
        pass

    async def scenario():
        async with AsyncOpenAI(api_key='offline-test',max_retries=0,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
            adapter=GeminiAdapter(client,min_interval=0,sleep=sleep)
            outcome=await Agent(LocalMCP(),adapter,'test').process(message())
            assert outcome['provider_unavailable']
            assert len(calls)==4
            assert len([e for e in outcome['audit']['events'] if e['kind']=='prepare_refill_for_approval'])==1
            assert len(outcome['request_events'])==4
            assert outcome['usage']['input_tokens']==246
            assert outcome['metrics']['unknown_usage_attempts']==2
    asyncio.run(scenario())


def test_emergency_reclassification_retains_prior_mutation_as_a_reported_failure():
    async def scenario():
        api=Responses([
            response([tool('triage_message',{'urgency':'routine','intents':['refill']})]),
            response([tool('prepare_refill_for_approval',{'medication':'lisinopril'})]),
            response([tool('triage_message',{'urgency':'emergency','intents':['refill','clinical']})]),
        ])
        outcome=await Agent(LocalMCP(),SimpleNamespace(responses=api),'test').process(message())
        assert outcome['error'] is not None
        assert outcome['result']['urgency']=='emergency'
        assert set(outcome['result']['actions'])=={'escalate_emergency','prepare_refill_for_approval'}
        assert outcome['result']['reply_draft'].startswith('Call 911')
    asyncio.run(scenario())


def test_language_survives_retriage_but_not_a_new_message():
    instance=clinic()
    instance.triage('routine',['admin'],'Spanish')
    instance.triage('urgent',['clinical'])
    assert instance.audit()['response_language']=='Spanish'
    instance.begin_message(message())
    assert instance.audit()['response_language']=='English'

@pytest.mark.parametrize('fail_after_mutation',[False,True])
def test_model_fallback_keeps_tools_once_and_reports_selected_model(fail_after_mutation):
    import json
    import httpx
    from openai import AsyncOpenAI
    from clinic_assist.providers import GeminiAdapter
    from test_providers import completion
    requested=[]
    def handle(request):
        body=json.loads(request.content)
        requested.append(body['model'])
        if len(requested)==1 or (fail_after_mutation and len(requested)==4):
            return httpx.Response(429,json={'error':{'message':'Daily quota exhausted'}})
        name,args=[('triage_message',{'urgency':'routine','intents':['refill']}),
            ('prepare_refill_for_approval',{'medication':'lisinopril'}),
            ('finish_message',{'actions':['prepare_refill_for_approval']})][len(requested)-2]
        return httpx.Response(200,json=completion({'role':'assistant','content':None,'tool_calls':[
            {'id':str(len(requested)),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}))
    async def scenario():
        async with AsyncOpenAI(api_key='offline-test',max_retries=0,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
            adapter=GeminiAdapter(client,min_interval=0,fallback_models=['gemini-backup','gemini-third'])
            outcome=await Agent(LocalMCP(),adapter,'gemini-primary').process(message())
            assert outcome['model']=='gemini-backup'
            assert outcome['primary_model']=='gemini-primary'
            assert outcome['metrics']['models_attempted']==['gemini-primary','gemini-backup']
            assert bool(outcome['error'])==fail_after_mutation
            assert len([e for e in outcome['audit']['events'] if e['kind']=='prepare_refill_for_approval'])==1
            assert outcome['metrics']['model_calls']==4
            assert outcome['usage']['input_tokens']==(246 if fail_after_mutation else 369)
    asyncio.run(scenario())
    assert requested==['gemini-primary','gemini-backup','gemini-backup','gemini-backup']
