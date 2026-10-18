import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def unpack(reply):
    assert not reply.isError, reply
    return reply.structuredContent or json.loads(reply.content[0].text)


def test_real_mcp_discovery_privacy_and_mutation():
    async def scenario():
        params = StdioServerParameters(command=sys.executable, args=["-m", "clinic_assist.server", "--data", str(ROOT / "data")])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {x.name for x in (await session.list_tools()).tools}
                assert {"find_patient", "check_refill_eligibility", "prepare_refill_for_approval", "book_appointment", "draft_reply"} <= tools
                message = dict(id="unseen-remote", channel="sms", from_name="Robert Chen", from_contact="555-0102",
                               received_at="2026-11-09T09:00:00-06:00", body="Please refill lisinopril")
                unpack(await session.call_tool("begin_message", {"message": message}))
                unpack(await session.call_tool("triage_message", {"urgency": "routine", "intents": ["refill"]}))
                assert unpack(await session.call_tool("find_patient", {}))["patient_id"] == "P002"
                prepared = unpack(await session.call_tool("prepare_refill_for_approval", {"medication": "lisinopril"}))
                assert prepared["status"] == "pending_provider_approval"
                audit = unpack(await session.call_tool("message_audit", {}))
                assert any(e["kind"] == "prepare_refill_for_approval" for e in audit["events"])
                assert audit["routes"][0]["queue"] == "provider_refills"
                message.update(id="unseen-private", from_contact="unlisted", stated_dob="1971-11-20")
                unpack(await session.call_tool("begin_message", {"message": message}))
                assert unpack(await session.call_tool("find_patient", {}))["patient_id"] is None
                assert (await session.call_tool("list_medications", {})).isError
    asyncio.run(asyncio.wait_for(scenario(), timeout=45))
