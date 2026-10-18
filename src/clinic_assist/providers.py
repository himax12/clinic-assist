"""Provider translation at the HTTP boundary; policy and MCP remain independent."""
import asyncio
import copy
import os
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from types import SimpleNamespace

from openai import AsyncOpenAI, RateLimitError, APIConnectionError

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


@dataclass
class ToolItem:
    name: str
    arguments: str
    call_id: str
    assistant_message: dict | None = None
    type: str = "function_call"

    def model_dump(self, **kwargs) -> dict:
        return {"type": self.type, "name": self.name, "arguments": self.arguments,
                "call_id": self.call_id, "_gemini_message": self.assistant_message}


def chat_history(instructions: str, history: list[dict]) -> list[dict]:
    messages = [{"role": "system", "content": instructions}]
    for item in history:
        if item.get("type") == "function_call":
            # One complete assistant turn is attached to its first call; this
            # preserves multi-call turns and provider-specific thought signatures.
            if item.get("_gemini_message") is not None:
                messages.append(copy.deepcopy(item["_gemini_message"]))
        elif item.get("type") == "function_call_output":
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
        elif "role" in item:
            messages.append(copy.deepcopy(item))
        else:
            raise ValueError("Unsupported Gemini conversation item")
    return messages


class RequestAttempts:
    def __init__(self, min_interval=0, max_retries=2, sleep=asyncio.sleep):
        if min_interval < 0 or not 0 <= max_retries <= 3:
            raise ValueError("Invalid pacing or retry configuration")
        self.min_interval, self.max_retries, self.sleep = min_interval, max_retries, sleep
        self._last_attempt = None
        self.events = []

    async def request(self, method, request, chat=False):
        rate_retries = transport_retries = server_retries = 0
        attempt = 0
        while True:
            if self._last_attempt is not None:
                delay = self.min_interval - (time.monotonic() - self._last_attempt)
                if delay > 0:
                    await self.sleep(delay)
            self._last_attempt = started = time.monotonic()
            attempt += 1
            event = dict(model=request['model'], attempt=attempt, started_at=datetime.now(timezone.utc).isoformat(),
                elapsed_ms=0, http_status=None, request_id=None, status='pending', failure_stage=None,
                error_type=None, input_tokens=None, output_tokens=None, usage_known=False)
            self.events.append(event)
            retry = None
            try:
                reply = await method(**request)
                usage = reply.usage
                event.update(status='completed', http_status=200, request_id=getattr(reply, '_request_id', None),
                    input_tokens=getattr(usage, 'prompt_tokens' if chat else 'input_tokens', None),
                    output_tokens=getattr(usage, 'completion_tokens' if chat else 'output_tokens', None),
                    usage_known=usage is not None)
                return reply
            except Exception as exc:
                event.update(status='failed', failure_stage='transport' if isinstance(exc, APIConnectionError) else 'provider',
                    error_type=type(exc).__name__, http_status=getattr(exc, 'status_code', None),
                    request_id=getattr(exc, 'request_id', None))
                body = getattr(exc, 'body', None)
                if isinstance(body, str):
                    try:
                        body = json.loads(body)
                    except ValueError:
                        body = {}
                body = body if isinstance(body, dict) else {}
                body = body.get('error', body)
                body = body if isinstance(body, dict) else {}
                if isinstance(body.get('message'), str) and body['message'].lstrip().startswith('{'):
                    try:
                        nested = json.loads(body['message'])
                        body = nested.get('error', nested) if isinstance(nested, dict) else body
                    except ValueError:
                        pass
                reason = str(exc).casefold()
                daily = any(value in reason for value in ('daily','per day','per_day','perday'))
                exhausted = daily or any(value in reason for value in ('limit: 0','limit 0','insufficient_quota','credit_balance_exhausted'))
                if isinstance(exc, RateLimitError):
                    event['limit_category'] = 'daily_quota' if daily else ('exhausted_quota' if exhausted else 'rate_limit')
                details = body.get('details', [])
                event['provider_error_code'] = body.get('status', body.get('code'))
                event['quota_limits'] = [{key: value.get(key) for key in ('quotaMetric', 'quotaId', 'quotaValue')}
                    for detail in details if isinstance(detail, dict)
                    for value in detail.get('violations', []) if isinstance(value, dict)]
                retry_hint = None
                for detail in details:
                    if isinstance(detail, dict) and 'retryDelay' in detail:
                        try:
                            retry_hint = float(str(detail['retryDelay']).removesuffix('s'))
                        except ValueError:
                            pass
                event['retry_after_seconds'] = retry_hint
                if isinstance(exc, APIConnectionError) and transport_retries < 1:
                    transport_retries += 1
                    retry = 2
                elif event['http_status'] in {500, 502, 503, 504} and server_retries < 1:
                    server_retries += 1
                    retry = 2
                elif isinstance(exc, RateLimitError) and rate_retries < self.max_retries:
                    if not exhausted:
                        retry = retry_hint if retry_hint is not None else 2 ** (rate_retries + 1)
                        value = exc.response.headers.get('retry-after')
                        if value:
                            try:
                                retry = max(0, float(value))
                            except ValueError:
                                try:
                                    retry = max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
                                except (ValueError, TypeError):
                                    pass
                        rate_retries += 1
                        if retry > 60:
                            retry = None
                if retry is None:
                    raise
            finally:
                event['elapsed_ms'] = round((time.monotonic() - started) * 1000, 2)
            await self.sleep(retry)


class ObservedOpenAI(RequestAttempts):
    def __init__(self, client):
        super().__init__()
        self.client, self.responses = client, self

    async def create(self, **kwargs):
        return await self.request(self.client.responses.create, kwargs)


class GeminiAdapter(RequestAttempts):
    """Responses-shaped surface using Google's Chat Completions endpoint."""
    def __init__(self, client, min_interval=12, max_retries=2, sleep=asyncio.sleep, fallback_models=()):
        super().__init__(min_interval, max_retries, sleep)
        self.client, self.responses = client, self
        self.fallback_models = list(dict.fromkeys(fallback_models))
        if len(self.fallback_models) > 3 or any(not name.startswith('gemini-') for name in self.fallback_models):
            raise ValueError('Configure at most three Gemini fallback models')
        self.selected_model = None

    async def _request_with_fallback(self, request, *, initial):
        candidates = list(dict.fromkeys([request['model'], *self.fallback_models])) if initial else [request['model']]
        previous = reason = None
        for index, model in enumerate(candidates):
            self.selected_model = model
            cursor = len(self.events)
            try:
                return await self.request(self.client.chat.completions.create, request | {'model': model}, chat=True)
            except Exception as exc:
                status = getattr(exc, 'status_code', None)
                eligible = isinstance(exc, APIConnectionError) or status in {404, 429, 500, 502, 503, 504}
                if not eligible or index == len(candidates) - 1:
                    raise
                next_reason = 'transport' if isinstance(exc, APIConnectionError) else f'http_{status}'
            finally:
                if previous is not None:
                    for event in self.events[cursor:]:
                        event.update(fallback_from=previous, fallback_reason=reason)
            previous, reason = model, next_reason

    async def create(self, *, model, instructions, input, tools, tool_choice=None, max_output_tokens=6000, **unused):
        # Never migrate a successful assistant/tool turn or its thought signatures
        # to another model. A new message starts with fresh user-only history.
        initial = not any(item.get('type') == 'function_call' or item.get('role') == 'assistant' for item in input)
        if initial:
            self.selected_model = model
        request = {"model": self.selected_model or model, "messages": chat_history(instructions, input),
                   "max_tokens": max_output_tokens}
        if tools:
            request["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""), "parameters": t["parameters"]}}
                for t in tools]
        if tool_choice is not None:
            request["tool_choice"] = ({"type": "function", "function": {"name": tool_choice["name"]}}
                                      if isinstance(tool_choice, dict) else tool_choice)
        reply = await self._request_with_fallback(request, initial=initial)
        if not reply.choices or (not reply.choices[0].message.tool_calls and not reply.choices[0].message.content):
            self.events[-1].update(status='failed', failure_stage='response_validation', error_type='ValueError')
        if not reply.choices:
            raise ValueError("Gemini returned no choices; request may have been blocked")
        message = reply.choices[0].message
        raw = message.model_dump(exclude_none=True)
        calls = message.tool_calls or []
        items = [ToolItem(c.function.name, c.function.arguments, c.id, raw if i == 0 else None)
                 for i, c in enumerate(calls)]
        if not calls and not message.content:
            raise ValueError("Gemini returned no usable text or tool calls")
        usage = reply.usage
        return SimpleNamespace(output=items, output_text=message.content or "",
            usage=SimpleNamespace(input_tokens=usage.prompt_tokens if usage else 0,
                                  output_tokens=usage.completion_tokens if usage else 0))


@asynccontextmanager
async def model_client(provider: str, key: str, min_interval: float = 12):
    if provider not in {"gemini", "openai"}:
        raise ValueError("Unknown model provider")
    # Explicit endpoint prevents accidental use of OPENAI_BASE_URL for Gemini.
    endpoint = GEMINI_BASE_URL if provider == "gemini" else "https://api.openai.com/v1/"
    timeout = float(os.getenv('MODEL_REQUEST_TIMEOUT', '60'))
    if timeout <= 0:
        raise ValueError('MODEL_REQUEST_TIMEOUT must be positive')
    async with AsyncOpenAI(api_key=key, base_url=endpoint, timeout=timeout, max_retries=0) as client:
        fallbacks = [name.strip() for name in os.getenv('GEMINI_FALLBACK_MODELS', '').split(',') if name.strip()]
        yield GeminiAdapter(client, min_interval=min_interval, fallback_models=fallbacks) if provider == "gemini" else ObservedOpenAI(client)
