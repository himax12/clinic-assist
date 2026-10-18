"""Provider translation at the HTTP boundary; policy and MCP remain independent."""
import asyncio
import copy
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from types import SimpleNamespace

from openai import AsyncOpenAI, RateLimitError

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


class GeminiAdapter:
    """Expose the small Responses-shaped surface used by Agent via Google chat."""
    def __init__(self, client, min_interval: float = 12, max_retries: int = 2, sleep=asyncio.sleep):
        if min_interval < 0 or not 0 <= max_retries <= 3:
            raise ValueError("Invalid pacing or retry configuration")
        self.client = client
        self.responses = self
        self.min_interval, self.max_retries, self.sleep = min_interval, max_retries, sleep
        self._last_attempt: float | None = None

    async def create(self, *, model, instructions, input, tools, tool_choice=None, max_output_tokens=6000, **unused):
        request = {"model": model, "messages": chat_history(instructions, input),
                   "max_tokens": max_output_tokens}
        if tools:
            request["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""), "parameters": t["parameters"]}}
                for t in tools]
        if tool_choice is not None:
            request["tool_choice"] = ({"type": "function", "function": {"name": tool_choice["name"]}}
                                      if isinstance(tool_choice, dict) else tool_choice)
        for attempt in range(self.max_retries + 1):
            if self._last_attempt is not None:
                delay = self.min_interval - (time.monotonic() - self._last_attempt)
                if delay > 0:
                    await self.sleep(delay)
            self._last_attempt = time.monotonic()
            try:
                reply = await self.client.chat.completions.create(**request)
                break
            except RateLimitError as exc:
                reason = str(exc).casefold()
                exhausted = any(s in reason for s in ("daily", "per day", "per_day", "limit: 0", "limit 0", "insufficient_quota", "credit_balance_exhausted"))
                if exhausted or attempt == self.max_retries:
                    raise
                retry_after = exc.response.headers.get("retry-after")
                delay = min(2 ** (attempt + 1), 60)
                if retry_after:
                    try:
                        delay = max(0, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError):
                            pass
                if delay > 60:
                    raise  # Do not stall a run on a long quota reset.
                await self.sleep(delay)
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
    async with AsyncOpenAI(api_key=key, base_url=endpoint, timeout=60, max_retries=0) as client:
        yield GeminiAdapter(client, min_interval=min_interval) if provider == "gemini" else client
