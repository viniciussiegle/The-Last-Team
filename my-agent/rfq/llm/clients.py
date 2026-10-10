from __future__ import annotations

import asyncio
import os
import time

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessage


class ModelClient:
    """One chat-completions interface for whichever model the configuration selects.

    The endpoint, key, model name, extra request parameters and prices all come from settings, so another
    OpenAI-compatible provider behind the run proxy needs configuration only.

    A request is occasionally left without any response while the service answers others normally. Answers are
    therefore read as a stream: a healthy call starts within a couple of seconds, so one that has sent nothing
    after a short limit is abandoned and sent again instead of being waited on.
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
            try:
                async with self.semaphore:
                    message, usage, finish = await (self.streamed(kwargs) if self.settings.model_stream else self.whole(kwargs))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.bus.emit("model_retry", agent, {"model": self.model, "attempt": attempt, "of": attempts,
                                                     "waited": round(time.monotonic() - started, 1),
                                                     "reason": f"{type(error).__name__}: {str(error)[:200]}"})
                if attempt >= attempts:
                    raise
                await asyncio.sleep(min(attempt, 4))
                continue
            self.answered = True
            self.account(agent, usage, finish, started)
            # Reasoning counts against the output allowance, so a cut-off answer can be empty or half a tool call.
            if finish == "length" and attempt < attempts:
                kwargs["max_tokens"] = min(kwargs["max_tokens"] * 2, 12000)
                self.bus.emit("model_retry", agent, {"model": self.model, "attempt": attempt, "of": attempts,
                                                     "reason": "answer cut off at the output allowance",
                                                     "next_max_tokens": kwargs["max_tokens"]})
                continue
            return message

    def wake_allowance(self, usual):
        """A hosted model may need minutes to wake for its first answer; afterwards a silent call is a stuck one."""
        return usual if self.answered else self.settings.model_first_call_timeout_s

    async def whole(self, kwargs):
        response = await asyncio.wait_for(self.client.chat.completions.create(**kwargs),
                                          self.wake_allowance(self.settings.model_call_timeout_s))
        choice = response.choices[0]
        return choice.message, response.usage.model_dump() if response.usage else {}, choice.finish_reason

    async def streamed(self, kwargs):
        began = time.monotonic()
        end = began + self.wake_allowance(self.settings.model_call_timeout_s)
        first_by = began + self.wake_allowance(self.settings.model_first_chunk_timeout_s)

        def remaining(until):
            left = min(until, end) - time.monotonic()
            if left <= 0:
                raise asyncio.TimeoutError
            return left

        stream = await asyncio.wait_for(self.client.chat.completions.create(
            **kwargs, stream=True, stream_options={"include_usage": True}), remaining(first_by))
        content, calls, usage, finish, started, first_chunk = [], {}, {}, None, False, None
        try:
            chunks = stream.__aiter__()
            while True:
                until = time.monotonic() + self.settings.model_chunk_gap_timeout_s if started else first_by
                try:
                    chunk = await asyncio.wait_for(chunks.__anext__(), remaining(until))
                except StopAsyncIteration:
                    break
                if not started:
                    first_chunk = round(time.monotonic() - began, 2)
                started = True
                if chunk.usage:
                    usage = chunk.usage.model_dump()
                for choice in chunk.choices:
                    finish = choice.finish_reason or finish
                    if choice.delta.content:
                        content.append(choice.delta.content)
                    for part in choice.delta.tool_calls or []:
                        call = calls.setdefault(part.index, {"id": "", "name": "", "arguments": ""})
                        call["id"] = part.id or call["id"]
                        if part.function:
                            call["name"] += part.function.name or ""
                            call["arguments"] += part.function.arguments or ""
        finally:
            await stream.close()
        if finish is None:
            raise RuntimeError("model stream ended without a finish reason")
        message = ChatCompletionMessage.model_validate({
            "role": "assistant", "content": "".join(content) or None,
            "tool_calls": [{"id": call["id"] or f"call_{index}", "type": "function",
                            "function": {"name": call["name"], "arguments": call["arguments"]}}
                           for index, call in sorted(calls.items())] or None})
        return message, {**usage, "first_chunk_s": first_chunk}, finish

    def account(self, agent, counts, finish, started):
        """Price one answered call, cut off or not."""
        cached = (counts.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        reasoning = (counts.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        inputs, outputs = counts.get("prompt_tokens", 0) or 0, counts.get("completion_tokens", 0) or 0
        prices = self.settings.model_prices
        cost = ((inputs - cached) * prices["input"] + cached * prices["cached"] + outputs * prices["output"]) / 1e6
        self.total_cost += cost
        self.bus.emit("model_call", agent, {"model": self.model, "tokens_in": inputs, "tokens_cached": cached,
                                             "tokens_out": outputs, "tokens_reasoning": reasoning, "cost": cost,
                                             "finish": finish, "first_chunk": counts.get("first_chunk_s"),
                                             "latency": round(time.monotonic() - started, 3)})


QwenClient = ModelClient  # name used by development scripts
