from __future__ import annotations

import asyncio
import os
import time

from openai import AsyncOpenAI


class QwenClient:
    def __init__(self, settings, bus):
        self.client = AsyncOpenAI(base_url=os.environ["QWEN_BASE_URL"], api_key=os.environ["QWEN_API_KEY"],
                                  timeout=360, max_retries=2)
        self.model = settings.qwen_model
        self.bus = bus
        self.semaphore = asyncio.Semaphore(8)
        self.total_cost = 0.0

    async def complete(self, agent, messages, tools=None, max_tokens=3500):
        started = time.monotonic()
        async with self.semaphore:
            kwargs = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.15}
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "required"
            response = await self.client.chat.completions.create(**kwargs)
        usage = response.usage
        counts = usage.model_dump() if usage else {}
        cached = (counts.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        inputs, outputs = counts.get("prompt_tokens", 0), counts.get("completion_tokens", 0)
        cost = ((inputs - cached) * 0.15 + cached * 0.016 + outputs * 0.47) / 1e6
        self.total_cost += cost
        self.bus.emit("model_call", agent, {"model": self.model, "tokens_in": inputs, "tokens_cached": cached,
                                             "tokens_out": outputs, "cost": cost,
                                             "latency": round(time.monotonic() - started, 3)})
        return response.choices[0].message
