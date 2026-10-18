"""Provider-neutral function loop backed by an actual MCP client session."""
import json

from .contracts import Message, MessageResult
from .prompts import SYSTEM

HOST_TOOLS = {"begin_message", "message_audit"}
MUTATIONS = {"prepare_refill_for_approval", "book_appointment", "cancel_appointment", "reschedule_appointment"}


class Agent:
    def __init__(self, session, client, model: str, max_steps: int = 18):
        self.session, self.client, self.model = session, client, model
        self.max_steps = max_steps
        self.trace: list[dict] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.provider_error_status = None

    async def _call(self, name: str, args: dict, host: bool = False) -> dict:
        try:
            reply = await self.session.call_tool(name, args)
        except Exception as exc:
            self.trace.append({"name": name, "arguments": args, "result": {"error": type(exc).__name__}, "host": host, "ok": False})
            raise
        if reply.isError:
            value = {"error": " ".join(getattr(c, "text", "") for c in reply.content)}
        else:
            value = reply.structuredContent
            if value is None:
                value = json.loads(reply.content[0].text)
        self.trace.append({"name": name, "arguments": args, "result": value, "host": host, "ok": not reply.isError})
        if host and reply.isError:
            raise RuntimeError("Required MCP call failed: " + name)
        return value

    async def process(self, raw_message: dict) -> dict:
        message = Message.model_validate(raw_message)
        self.trace = []
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.provider_error_status = None
        await self._call("begin_message", {"message": message.model_dump(mode="json")}, host=True)
        policy = await self._call("public_policy", {}, host=True)
        tools = [{"type": "function", "name": t.name, "description": t.description,
                  "parameters": t.inputSchema, "strict": False}
                 for t in (await self.session.list_tools()).tools if t.name not in HOST_TOOLS]
        allowed = {t["name"] for t in tools}
        instructions = SYSTEM + "\nClinic policy:\n" + json.dumps(policy, ensure_ascii=False)
        instructions += "\nResult JSON schema:\n" + json.dumps(MessageResult.model_json_schema())
        history = [{"role": "user", "content": json.dumps(message.model_dump(mode="json"), ensure_ascii=False)}]
        error = None
        finalize = False
        try:
            for step in range(self.max_steps):
                kwargs = dict(model=self.model, instructions=instructions, input=history, tools=tools,
                              parallel_tool_calls=False, store=False, max_output_tokens=6000)
                if step == 0:
                    kwargs["tool_choice"] = {"type": "function", "name": "triage_message"}
                elif finalize:
                    kwargs["tool_choice"] = "none"
                response = await self.client.responses.create(**kwargs)
                for key in self.usage:
                    self.usage[key] += getattr(response.usage, key, 0) if response.usage else 0
                calls = [item for item in response.output if item.type == "function_call"]
                history.extend(item.model_dump(exclude_none=True) for item in response.output)
                if not calls:
                    proposal = MessageResult.model_validate_json(response.output_text)
                    audit = await self._call("message_audit", {}, host=True)
                    executed = {e["kind"] for e in audit["events"]}
                    invented = (set(proposal.actions) & MUTATIONS) - executed
                    queues = {r["queue"] for r in audit["routes"]}
                    required_queues = {"route_to_billing": {"billing"}, "route_to_front_desk": {"front_desk"},
                        "route_to_provider": {"provider_refills"}, "route_to_clinician": {"clinician_urgent", "clinician_routine"}}
                    invented |= {action for action, required in required_queues.items()
                                 if action in proposal.actions and not (queues & required)}
                    if "escalate_emergency" in proposal.actions and audit["urgency"] != "emergency":
                        invented.add("escalate_emergency")
                    if invented:
                        raise ValueError("Result claims unperformed mutations: " + ",".join(sorted(invented)))
                    if not audit["urgency"]:
                        raise ValueError("Missing triage")
                    proposal.id = message.id
                    proposal.patient_id = audit["patient_id"]
                    proposal.urgency = audit["urgency"]
                    proposal.intents = audit["intents"]
                    proposal.routed_to = sorted({r["queue"] for r in audit["routes"]})
                    proposal.actions = sorted(set(proposal.actions) | (executed & MUTATIONS))
                    patient = await self._call("find_patient", {}, host=True)
                    draft = await self._call("draft_reply", {"actions": proposal.actions, "language": patient.get("preferred_language", "English")}, host=True)
                    proposal.reply_draft = draft["reply_draft"]
                    return await self._finish(proposal, None)
                for call in calls:
                    if call.name not in allowed or call.name in HOST_TOOLS:
                        raise ValueError("Model attempted a host-only or unknown tool")
                    if step == 0 and call.name != "triage_message":
                        raise ValueError("Message must be triaged first")
                    arguments = json.loads(call.arguments)
                    value = await self._call(call.name, arguments)
                    finalize = call.name == "draft_reply" and "error" not in value
                    history.append({"type": "function_call_output", "call_id": call.call_id,
                                    "output": json.dumps(value, ensure_ascii=False)})
                    audit = await self._call("message_audit", {}, host=True)
                    if audit["urgency"] == "emergency":
                        patient = await self._call("find_patient", {}, host=True)
                        draft = await self._call("draft_reply", {"actions": ["escalate_emergency"], "language": patient.get("preferred_language", "English")}, host=True)
                        result = MessageResult(id=message.id, patient_id=audit["patient_id"],
                            intents=audit["intents"], urgency="emergency", actions=["escalate_emergency"],
                            routed_to=["clinician_urgent"], tool_calls=[], reply_draft=draft["reply_draft"])
                        return await self._finish(result, None)
            raise RuntimeError("Tool iteration limit reached")
        except Exception as exc:
            # Preserve performed operations and hand off uncertain processing to a
            # clinician. Do not pretend a partial run completed all requests.
            error = type(exc).__name__ + ": " + str(exc)
            self.provider_error_status = getattr(exc, "status_code", None)
        audit = await self._call("message_audit", {}, host=True)
        if audit["urgency"] is None:
            await self._call("triage_message", {"urgency": "urgent", "intents": ["other"]}, host=True)
        await self._call("route_to_human", {"queue": "clinician_urgent", "reason": "Agent processing failed; human review required"}, host=True)
        audit = await self._call("message_audit", {}, host=True)
        actions = sorted({e["kind"] for e in audit["events"]} & MUTATIONS)
        actions.append("escalate_emergency" if audit["urgency"] == "emergency" else "route_to_clinician")
        draft = await self._call("draft_reply", {"actions": actions}, host=True)
        result = MessageResult(id=message.id, patient_id=audit["patient_id"], intents=audit["intents"],
            urgency=audit["urgency"], actions=actions, routed_to=["clinician_urgent"], tool_calls=[], reply_draft=draft["reply_draft"])
        return await self._finish(result, error)

    async def _finish(self, result: MessageResult, error: str | None) -> dict:
        audit = await self._call("message_audit", {}, host=True)
        result.tool_calls = [t["name"] for t in self.trace]
        result.routed_to = sorted({r["queue"] for r in audit["routes"]})
        return {"result": MessageResult.model_validate(result.model_dump()).model_dump(), "trace": self.trace,
                "audit": audit, "usage": self.usage, "error": error, "provider_error_status": self.provider_error_status, "model": self.model}
