import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clinic_assist.agent import Agent
from clinic_assist.clinic import Clinic
from clinic_assist.data import ClinicData
from clinic_assist.server import create_server

ROOT = Path(__file__).resolve().parents[1]


class LocalMCP:
    """Real FastMCP tool dispatch without process overhead; subprocess is tested separately."""
    def __init__(self):
        self.server = create_server(ROOT / "data")

    async def list_tools(self):
        return SimpleNamespace(tools=await self.server.list_tools())

    async def call_tool(self, name, arguments):
        try:
            raw = await self.server.call_tool(name, arguments)
            content, structured = raw if isinstance(raw, tuple) else (raw, None)
            return SimpleNamespace(isError=False, structuredContent=structured, content=content)
        except Exception as exc:
            return SimpleNamespace(isError=True, structuredContent=None, content=[SimpleNamespace(text=str(exc))])


def tool(name, arguments):
    return SimpleNamespace(type="function_call", name=name, arguments=json.dumps(arguments), call_id="call-"+name,
                           model_dump=lambda **_: dict(type="function_call",name=name, arguments=json.dumps(arguments),call_id="call-"+name))


class Responses:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


def response(calls=(), text=""):
    return SimpleNamespace(output=list(calls), output_text=text, usage=SimpleNamespace(input_tokens=100, output_tokens=20))


def message(contact="555-0102"):
    return dict(id="unseen",channel="sms",from_name="Robert Chen",from_contact=contact,
                body="Please help",received_at="2026-11-09T09:00:00-06:00")


def final(**kwargs):
    return json.dumps(dict(id="unseen",patient_id="P002",intents=["refill"],urgency="routine",
                           actions=["prepare_refill_for_approval"],routed_to=["provider_refills"],
                           tool_calls=["invented"],reply_draft="Prepared for provider approval.") | kwargs)


def test_agent_executes_real_tools_and_records_actual_calls():
    async def scenario():
        api = Responses([
            response([tool("triage_message", {"urgency":"routine", "intents":["refill"]})]),
            response([tool("prepare_refill_for_approval", {"medication":"lisinopril"})]),
            response(text=final()),
        ])
        outcome = await Agent(LocalMCP(), SimpleNamespace(responses=api), "test-model").process(message())
        assert outcome["result"]["patient_id"] == "P002"
        assert "prepare_refill_for_approval" in outcome["result"]["tool_calls"]
        assert "invented" not in outcome["result"]["tool_calls"]
        assert outcome["audit"]["routes"][0]["queue"] == "provider_refills"
        assert all(not any(t["name"] == "begin_message" for t in r["tools"]) for r in api.requests)
    asyncio.run(scenario())


def test_emergency_is_escalated_and_template_replaces_llm_advice():
    async def scenario():
        api = Responses([response([tool("triage_message", {"urgency":"emergency", "intents":["clinical"]})])])
        outcome = await Agent(LocalMCP(), SimpleNamespace(responses=api), "test-model").process(message("unknown"))
        assert outcome["result"]["patient_id"] is None
        assert outcome["result"]["actions"] == ["escalate_emergency"]
        assert outcome["result"]["routed_to"] == ["clinician_urgent"]
        assert outcome["result"]["reply_draft"].startswith("Call 911")
        assert len(api.requests) == 1
    asyncio.run(scenario())


def test_failed_api_produces_explicit_human_handoff():
    async def scenario():
        api = Responses([RuntimeError("provider unavailable")])
        outcome = await Agent(LocalMCP(), SimpleNamespace(responses=api), "test-model").process(message())
        assert outcome["error"]
        assert "route_to_clinician" in outcome["result"]["actions"]
        assert outcome["audit"]["routes"][0]["queue"] == "clinician_urgent"
        assert outcome["result"]["urgency"] == "urgent"
    asyncio.run(scenario())


def test_invented_booking_claim_is_rejected_and_reported():
    async def scenario():
        api = Responses([response([tool("triage_message", {"urgency":"routine", "intents":["schedule"]})]),
                         response(text=final(intents=["schedule"], actions=["book_appointment"], reply_draft="Booked!"))])
        outcome = await Agent(LocalMCP(), SimpleNamespace(responses=api), "test-model").process(message())
        assert outcome["error"]
        assert "book_appointment" not in outcome["result"]["actions"]
        assert "Booked!" not in outcome["result"]["reply_draft"]
    asyncio.run(scenario())


def test_host_tools_cannot_be_called_by_model():
    async def scenario():
        api = Responses([response([tool("begin_message", {"message":message("555-0101")})])])
        outcome = await Agent(LocalMCP(), SimpleNamespace(responses=api), "test-model").process(message())
        assert outcome["error"]
        assert outcome["result"]["patient_id"] == "P002"
    asyncio.run(scenario())


def test_claimed_handoff_requires_a_real_queue_event():
    async def scenario():
        api = Responses([response([tool("triage_message", {"urgency":"routine", "intents":["billing"]})]),
                         response(text=final(intents=["billing"], actions=["route_to_billing"], reply_draft="Billing has been alerted."))])
        outcome = await Agent(LocalMCP(), SimpleNamespace(responses=api), "test-model").process(message())
        assert outcome["error"]
        assert "route_to_billing" not in outcome["result"]["actions"]
        assert "Billing has been alerted" not in outcome["result"]["reply_draft"]
    asyncio.run(scenario())


def test_reply_cannot_invent_booking_or_clinical_advice_under_other_action():
    async def scenario():
        api = Responses([response([tool("triage_message", {"urgency":"routine", "intents":["admin"]})]),
                         response(text=final(intents=["admin"], actions=["answer_directly"], reply_draft="Booked! Take an extra dose of insulin."))])
        outcome = await Agent(LocalMCP(), SimpleNamespace(responses=api), "test-model").process(message())
        assert "Booked!" not in outcome["result"]["reply_draft"]
        assert "extra dose" not in outcome["result"]["reply_draft"]
        assert "09:00" in outcome["result"]["reply_draft"]
    asyncio.run(scenario())


def test_failed_required_handoff_never_returns_success_and_keeps_failed_trace():
    class RejectRoute(LocalMCP):
        async def call_tool(self, name, arguments):
            if name == "route_to_human":
                return SimpleNamespace(isError=True, structuredContent=None, content=[SimpleNamespace(text="Queue unavailable")])
            return await super().call_tool(name, arguments)

    async def scenario():
        api = Responses([RuntimeError("provider unavailable")])
        agent = Agent(RejectRoute(), SimpleNamespace(responses=api), "test-model")
        with pytest.raises(RuntimeError, match="Required MCP call failed"):
            await agent.process(message())
        assert any(t["name"] == "route_to_human" and not t["ok"] for t in agent.trace)
    asyncio.run(scenario())


def test_saved_draft_requests_final_result_without_repeating_tools():
    async def scenario():
        api = Responses([
            response([tool("triage_message", {"urgency":"routine", "intents":["admin"]})]),
            response([tool("draft_reply", {"actions":["answer_directly"], "language":"English"})]),
            response(text=final(intents=["admin"], actions=["answer_directly"])),
        ])
        outcome = await Agent(LocalMCP(), SimpleNamespace(responses=api), "test-model").process(message())
        assert outcome["error"] is None
        assert api.requests[-1]["tool_choice"] == "none"
    asyncio.run(scenario())
