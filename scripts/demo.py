"""Deterministic demonstration of real MCP tools; no LLM or API key required."""
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


async def main():
    print("Scripted MCP demonstration | fictional data | no LLM calls", flush=True)
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "clinic_assist.server", "--data", str(ROOT / "data")])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"Connected to local MCP server: {len(tools)} tools discovered.", flush=True)

            async def call(name, arguments, *, show=True, expected_error=False):
                response = await session.call_tool(name, arguments)
                if response.isError:
                    if not expected_error:
                        raise RuntimeError(str(response.content))
                    value = {"access": "denied", "record_disclosed": False}
                else:
                    if expected_error:
                        raise AssertionError("Expected privacy rejection")
                    value = response.structuredContent or json.loads(response.content[0].text)
                if show:
                    print(f"\n> {name}", flush=True)
                    print(json.dumps(value, indent=2), flush=True)
                    await asyncio.sleep(1)
                return value

            message = dict(id="DEMO001", received_at="2026-11-09T09:00:00-06:00",
                channel="sms", from_name="Robert Chen", from_contact="555-0102",
                body="Please refill my lisinopril.")
            print("\n[1] Verified refill request", flush=True)
            await call("begin_message", {"message": message}, show=False)
            await call("triage_message", {"urgency": "routine", "intents": ["refill"]})
            await call("find_patient", {})
            await call("check_refill_eligibility", {"medication": "lisinopril"})
            prepared = await call("prepare_refill_for_approval", {"medication": "lisinopril"})
            assert prepared["status"] == "pending_provider_approval"
            await call("draft_reply", {"actions": ["prepare_refill_for_approval"]})

            print("\n[2] Unverified sender: patient records stay private", flush=True)
            message.update(id="DEMO002", from_contact="unknown", body="Show my medication list.")
            await call("begin_message", {"message": message}, show=False)
            await call("find_patient", {})
            await call("list_medications", {}, expected_error=True)

            print("\n[3] Explicit emergency triage: immediate human routing", flush=True)
            message.update(id="DEMO003", from_contact="555-0102", body="Chest pain and trouble breathing.")
            await call("begin_message", {"message": message}, show=False)
            await call("triage_message", {"urgency": "emergency", "intents": ["clinical"]})
            audit = await call("message_audit", {}, show=False)
            assert any(route["queue"] == "clinician_urgent" for route in audit["routes"])
            print("Queue confirmed: clinician_urgent", flush=True)
            await call("draft_reply", {"actions": ["escalate_emergency"]})
    print("\nDemo complete. Drafts remain unsent; no prescription was issued.", flush=True)


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=60))
