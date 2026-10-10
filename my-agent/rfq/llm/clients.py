from __future__ import annotations

import asyncio
import os
import time

from openai import AsyncOpenAI


class ModelClient:
    """One chat-completions interface for whichever model the configuration selects.

    The endpoint, key, model name, extra request parameters and prices all come from settings, so another
    OpenAI-compatible provider behind the run proxy needs configuration only.
    """

    def __init__(self, settings, bus):
        self.client = AsyncOpenAI(base_url=os.environ[settings.model_base_url_env], api_key=os.environ[settings.model_api_key_env],
                                  timeout=settings.model_first_call_timeout_s, max_retries=0)
        self.settings = settings
        self.model = settings.model_name
        self.bus = bus
        self.semaphore = asyncio.Semaphore(settings.model_concurrency)
        self.total_cost = 0.0
        self.answered = False

    async def complete(self, agent, messages, tools=None, max_tokens=3500):
        started = time.monotonic()
        kwargs = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.15}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "required"
        if self.settings.model_extra_body:
            kwargs["extra_body"] = self.settings.model_extra_body
        attempts = self.settings.model_retries + 1
        attempt = 0
        while True:
            attempt += 1
            # A hosted model may need minutes to wake for its first answer; afterwards a silent call is a stuck one.
            limit = self.settings.model_call_timeout_s if self.answered else self.settings.model_first_call_timeout_s
            try:
                async with self.semaphore:
                    response = await asyncio.wait_for(self.client.chat.completions.create(**kwargs), limit)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.bus.emit("model_retry", agent, {"model": self.model, "attempt": attempt, "of": attempts,
                                                     "waited": round(time.monotonic() - started, 1),
                                                     "reason": f"{type(error).__name__}: {str(error)[:200]}"})
                if attempt >= attempts:
                    raise
                await asyncio.sleep(min(2 ** attempt, 6))
                continue
            self.answered = True
            finish = self.account(agent, response, started)
            # Reasoning counts against the output allowance, so a cut-off answer can be empty or half a tool call.
            if finish == "length" and attempt < attempts:
                kwargs["max_tokens"] = min(kwargs["max_tokens"] * 2, 12000)
                self.bus.emit("model_retry", agent, {"model": self.model, "attempt": attempt, "of": attempts,
                                                     "reason": "answer cut off at the output allowance",
                                                     "next_max_tokens": kwargs["max_tokens"]})
                continue
            return response.choices[0].message

    def account(self, agent, response, started):
        """Price one answered call, cut off or not, and return why it ended."""
        usage = response.usage
        counts = usage.model_dump() if usage else {}
        cached = (counts.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        reasoning = (counts.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        inputs, outputs = counts.get("prompt_tokens", 0), counts.get("completion_tokens", 0)
        prices = self.settings.model_prices
        cost = ((inputs - cached) * prices["input"] + cached * prices["cached"] + outputs * prices["output"]) / 1e6
        self.total_cost += cost
        finish = response.choices[0].finish_reason
        self.bus.emit("model_call", agent, {"model": self.model, "tokens_in": inputs, "tokens_cached": cached,
                                             "tokens_out": outputs, "tokens_reasoning": reasoning, "cost": cost,
                                             "finish": finish, "latency": round(time.monotonic() - started, 3)})
        return finish


QwenClient = ModelClient  # name used by development scripts
