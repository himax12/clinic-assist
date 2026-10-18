import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI, RateLimitError

from clinic_assist.providers import GeminiAdapter


TOOLS = [{"type": "function", "name": "triage_message", "description": "Triage", "strict": False,
          "parameters": {"type": "object", "properties": {"urgency": {"type": "string"}}, "required": ["urgency"]}}]


def completion(message, **extra):
    return dict(id="gemini-response", object="chat.completion", created=1, model="gemini-test",
                choices=[{"index": 0, "finish_reason": "stop", "message": message}],
                usage={"prompt_tokens": 123, "completion_tokens": 24, "total_tokens": 147}) | extra


def test_gemini_tool_roundtrip_preserves_signature_and_usage():
    requests = []
    def handle(request):
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.url.path == "/v1beta/openai/chat/completions"
        assert request.headers["authorization"] == "Bearer gemini-test-key"
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body["tool_choice"] == {"type":"function", "function":{"name":"triage_message"}}
            assert body["tools"][0]["function"]["name"] == "triage_message"
            return httpx.Response(200, json=completion({"role":"assistant", "content":None, "tool_calls":[{
                "id":"c1", "type":"function", "function":{"name":"triage_message", "arguments":'{"urgency":"routine"}'},
                "extra_content":{"google":{"thought_signature":"signed-data"}}
            }]}))
        assistant = next(m for m in body["messages"] if m["role"] == "assistant")
        assert assistant["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "signed-data"
        assert body["messages"][-1] == {"role":"tool", "tool_call_id":"c1", "content":'{"urgency":"routine"}'}
        return httpx.Response(200, json=completion({"role":"assistant", "content":'{"done":true}'}))

    async def scenario():
        async with AsyncOpenAI(api_key="gemini-test-key", base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), max_retries=0) as client:
            provider = GeminiAdapter(client, min_interval=0)
            history = [{"role":"user", "content":"synthetic test"}]
            reply = await provider.responses.create(model="gemini-test", instructions="Policy", input=history, tools=TOOLS,
                tool_choice={"type":"function", "name":"triage_message"}, max_output_tokens=100, parallel_tool_calls=False, store=False)
            assert reply.usage.input_tokens == 123
            assert reply.usage.output_tokens == 24
            assert reply.output[0].call_id == "c1"
            history.extend(x.model_dump(exclude_none=True) for x in reply.output)
            history.append({"type":"function_call_output", "call_id":"c1", "output":'{"urgency":"routine"}'})
            final = await provider.responses.create(model="gemini-test", instructions="Policy", input=history, tools=TOOLS, max_output_tokens=100)
            assert final.output_text == '{"done":true}'
            assert final.output == []
    asyncio.run(scenario())


def test_rate_limit_retry_is_bounded_and_honors_retry_after():
    calls, waits = [], []
    def handle(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after":"2"}, json={"error":{"message":"Rate limit exceeded", "type":"rate_limit_exceeded"}})

    async def sleep(delay):
        waits.append(delay)

    async def scenario():
        async with AsyncOpenAI(api_key="test", base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), max_retries=0) as client:
            provider = GeminiAdapter(client, min_interval=0, max_retries=1, sleep=sleep)
            with pytest.raises(RateLimitError):
                await provider.responses.create(model="gemini-test", instructions="policy", input=[], tools=[])
    asyncio.run(scenario())
    assert len(calls) == 2
    assert waits == [2]


def test_daily_quota_is_not_retried():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(429, json={"error":{"message":"Daily quota exhausted", "type":"RESOURCE_EXHAUSTED"}})
    async def scenario():
        async with AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), max_retries=0) as client:
            with pytest.raises(RateLimitError):
                await GeminiAdapter(client, min_interval=0).responses.create(model="gemini-test", instructions="p", input=[], tools=[])
    asyncio.run(scenario())
    assert len(calls) == 1


def test_empty_choices_report_explicit_provider_error():
    async def scenario():
        async with AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200,json=completion({"role":"assistant","content":""},choices=[])))), max_retries=0) as client:
            with pytest.raises(ValueError, match="no choices"):
                await GeminiAdapter(client, min_interval=0).responses.create(model="test", instructions="p", input=[], tools=[])
    asyncio.run(scenario())


def test_multi_call_turn_is_replayed_once_with_both_tool_outputs():
    requests = []
    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, json=completion({"role":"assistant", "content":None, "tool_calls":[
                {"id":"one", "type":"function", "function":{"name":"find_patient", "arguments":"{}"},
                 "extra_content":{"google":{"thought_signature":"signature"}}},
                {"id":"two", "type":"function", "function":{"name":"list_medications", "arguments":"{}"}},
            ]}))
        assistant = [m for m in body["messages"] if m["role"] == "assistant"]
        assert len(assistant) == 1
        assert len(assistant[0]["tool_calls"]) == 2
        assert assistant[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "signature"
        assert [m["tool_call_id"] for m in body["messages"] if m["role"] == "tool"] == ["one", "two"]
        return httpx.Response(200, json=completion({"role":"assistant", "content":"done"}))

    async def scenario():
        async with AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), max_retries=0) as client:
            adapter = GeminiAdapter(client, min_interval=0)
            history = [{"role":"user", "content":"synthetic"}]
            reply = await adapter.responses.create(model="test", instructions="p", input=history, tools=TOOLS)
            history.extend(x.model_dump() for x in reply.output)
            history.extend({"type":"function_call_output", "call_id":id, "output":"{}"} for id in ["one", "two"])
            await adapter.responses.create(model="test", instructions="p", input=history, tools=TOOLS)
    asyncio.run(scenario())
