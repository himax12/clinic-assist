"""Provider-neutral function loop backed by an actual MCP client session."""
import json
import time
from openai import APIConnectionError, APIStatusError

from .contracts import Message, MessageResult
from .prompts import SYSTEM

HOST_TOOLS = {"begin_message", "message_audit"}
HIDDEN_TOOLS = HOST_TOOLS | {"draft_reply"}
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
        self.started_at = time.monotonic()
        self.request_events = []
        self.provider_unavailable = False
        message = Message.model_validate(raw_message)
        self.current_id = message.id
        self.trace = []
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.provider_error_status = None
        await self._call("begin_message", {"message": message.model_dump(mode="json")}, host=True)
        policy = await self._call("public_policy", {}, host=True)
        tools = [{"type": "function", "name": t.name, "description": t.description,
                  "parameters": t.inputSchema, "strict": False}
                 for t in (await self.session.list_tools()).tools if t.name not in HIDDEN_TOOLS]
        allowed = {t["name"] for t in tools}
        instructions = SYSTEM + "\nClinic policy:\n" + json.dumps(policy, ensure_ascii=False)
        instructions += "\nResult JSON schema:\n" + json.dumps(MessageResult.model_json_schema())
        history = [{"role": "user", "content": json.dumps(message.model_dump(mode="json"), ensure_ascii=False)}]
        error = None
        try:
            for step in range(self.max_steps):
                kwargs = dict(model=self.model, instructions=instructions, input=history, tools=tools,
                              parallel_tool_calls=False, store=False, max_output_tokens=6000)
                if step == 0:
                    kwargs["tool_choice"] = {"type": "function", "name": "triage_message"}
                observed = getattr(self.client, "events", None)
                cursor = len(observed) if observed is not None else 0
                started = time.monotonic()
                response = None
                try:
                    response = await self.client.responses.create(**kwargs)
                finally:
                    if observed is not None:
                        events = [dict(event, model_step=step + 1) for event in observed[cursor:]]
                    else:
                        usage = getattr(response, "usage", None)
                        events = [dict(model=self.model, model_step=step + 1, attempt=1,
                            elapsed_ms=round((time.monotonic()-started)*1000, 2),
                            status="completed" if response is not None else "failed", request_id=None,
                            http_status=None, failure_stage=None if response else "request", usage_known=usage is not None,
                            input_tokens=getattr(usage, "input_tokens", None), output_tokens=getattr(usage, "output_tokens", None))]
                    self.request_events.extend(events)
                    for event in events:
                        for key in self.usage:
                            self.usage[key] += event.get(key) or 0
                calls = [item for item in response.output if item.type == "function_call"]
                history.extend(item.model_dump(exclude_none=True) for item in response.output)
                if not calls:
                    proposal = MessageResult.model_validate_json(response.output_text)
                    return await self._complete(proposal.actions)
                for call in calls:
                    if call.name not in allowed or call.name in HOST_TOOLS:
                        raise ValueError("Model attempted a host-only or unknown tool")
                    if step == 0 and call.name != "triage_message":
                        raise ValueError("Message must be triaged first")
                    arguments = json.loads(call.arguments)
                    value = await self._call(call.name, arguments)
                    history.append({"type": "function_call_output", "call_id": call.call_id,
                                    "output": json.dumps(value, ensure_ascii=False)})
                    if call.name == "finish_message" and "error" not in value:
                        return await self._completed_value(value)
                    audit = await self._call("message_audit", {}, host=True)
                    if audit["urgency"] == "emergency":
                        return await self._complete(["escalate_emergency"])
            raise RuntimeError("Tool iteration limit reached")
        except Exception as exc:
            # Preserve performed operations and hand off uncertain processing to a
            # clinician. Do not pretend a partial run completed all requests.
            error = type(exc).__name__ + ": " + ("Provider request failed; inspect request_events" if isinstance(exc, (APIConnectionError, APIStatusError)) else str(exc))
            self.provider_error_status = getattr(exc, "status_code", None)
            self.provider_unavailable = isinstance(exc, APIConnectionError) or (
                self.provider_error_status in {400,401,402,403,404,422,429} or (self.provider_error_status or 0) >= 500)
        audit = await self._call("message_audit", {}, host=True)
        if audit["urgency"] is None:
            await self._call("triage_message", {"urgency": "urgent", "intents": ["other"]}, host=True)
        await self._call("route_to_human", {"queue": "clinician_urgent", "reason": "Agent processing failed; human review required"}, host=True)
        audit = await self._call("message_audit", {}, host=True)
        actions = sorted({e["kind"] for e in audit["events"]} & MUTATIONS)
        actions.append("escalate_emergency" if audit["urgency"] == "emergency" else "route_to_clinician")
        return await self._complete(actions, error)

    async def _complete(self, actions, error=None):
        value = await self._call("finish_message", {"actions": actions}, host=True)
        return await self._completed_value(value, error)

    async def _completed_value(self, value, error=None):
        error = error or value.get("safety_error")
        audit = await self._call("message_audit", {}, host=True)
        result = MessageResult(id=self.current_id, patient_id=audit["patient_id"],
            intents=audit["intents"], urgency=audit["urgency"], actions=value["actions"],
            routed_to=sorted({r["queue"] for r in audit["routes"]}), tool_calls=[], reply_draft=value["reply_draft"])
        return await self._finish(result, error)

    async def _finish(self, result: MessageResult, error: str | None) -> dict:
        audit = await self._call("message_audit", {}, host=True)
        result.tool_calls = [t["name"] for t in self.trace]
        result.routed_to = sorted({r["queue"] for r in audit["routes"]})
        return {"result": MessageResult.model_validate(result.model_dump()).model_dump(), "trace": self.trace,
                "audit": audit, "usage": self.usage, "error": error, "provider_error_status": self.provider_error_status,
                "provider_unavailable": self.provider_unavailable, "request_events": self.request_events,
                "metrics": {"elapsed_ms": round((time.monotonic()-self.started_at)*1000,2),
                    "model_calls": len(self.request_events),
                    "unknown_usage_attempts": sum(not e["usage_known"] for e in self.request_events)}, "model": self.model}
