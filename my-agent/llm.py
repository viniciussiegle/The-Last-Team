"""The two hosted models as the agents use them.

Qwen3.8-Flash-Next does the reasoning: planning, reading pages, choosing searches and form values, the decision.
Clef is the click policy: given a page and a goal it picks the control to act on, classifies the page and maps form
controls to the fields they stand for. Clef answers in about a second, so most navigation steps never wait for Qwen.
"""
import asyncio
import json
import os
import re
import time

import httpx

import models

CLEF_MODELS = ["clef-flash", "clef"]  # clef-flash where the endpoint serves it, otherwise clef


class Usage:
    def __init__(self):
        self.qwen_calls = self.clef_calls = 0
        self.qwen_in = self.qwen_cached = self.qwen_out = self.clef_in = 0

    def cost(self) -> float:
        return ((self.qwen_in - self.qwen_cached) * 0.15 + self.qwen_cached * 0.016 + self.qwen_out * 0.47
                + self.clef_in * 0.24) / 1e6

    def __str__(self):
        return (f"qwen {self.qwen_calls} calls ({self.qwen_in} in, {self.qwen_cached} cached, {self.qwen_out} out), "
                f"clef {self.clef_calls} calls ({self.clef_in} in), ~${self.cost():.4f}")


USAGE = Usage()
_qwen = None
_qwen_slots = asyncio.Semaphore(24)
_clef_slots = asyncio.Semaphore(12)


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


async def qwen(system: str, user: str, max_tokens: int = 1500, images: list[str] | None = None,
               timeout: float = 60, attempts: int = 2, think: bool = False) -> dict:
    """One JSON-mode Qwen call. Workers call it without thinking (output tokens dominate latency); the master
    thinks when planning and deciding. The system prompt is shared between calls so the provider can cache it."""
    global _qwen
    _qwen = _qwen or models.qwen().with_options(max_retries=0)
    content = user if not images else [{"type": "text", "text": user}] + [
        {"type": "image_url", "image_url": {"url": image}} for image in images]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    error = None
    for attempt in range(attempts):
        try:
            async with _qwen_slots:
                reply = await asyncio.wait_for(_qwen.chat.completions.create(
                    model=models.QWEN_MODEL, messages=messages, max_tokens=max_tokens, temperature=0.1,
                    response_format={"type": "json_object"},
                    extra_body={"chat_template_kwargs": {"enable_thinking": think}}), timeout)
            USAGE.qwen_calls += 1
            if reply.usage:
                USAGE.qwen_in += reply.usage.prompt_tokens
                USAGE.qwen_out += reply.usage.completion_tokens
                details = reply.usage.prompt_tokens_details
                USAGE.qwen_cached += (details.cached_tokens or 0) if details else 0
            return parse_json(reply.choices[0].message.content)
        except Exception as exc:  # timeouts, 5xx, malformed JSON: retry like any provider call
            error = exc
            await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"qwen failed: {type(error).__name__}: {str(error)[:200]}")


async def clef(state, questions: dict, images: list[str] | None = None, timeout: float = 30) -> dict:
    """One Clef decision; returns the answers keyed by question id."""
    body = {"state": state, "questions": questions}
    if images:
        body["images"] = images[:4]
    url = os.environ["CLEF_BASE_URL"].rstrip("/") + "/v1/decide"
    headers = {"Authorization": f"Bearer {os.environ['CLEF_API_KEY']}"}
    error = None
    for attempt in range(4):
        model = CLEF_MODELS[0]
        try:
            async with _clef_slots, httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json={"model": model, **body}, headers=headers)
            if response.status_code == 400 and "unsupported_model" in response.text and len(CLEF_MODELS) > 1:
                if CLEF_MODELS[0] == model:  # concurrent calls may have switched already
                    CLEF_MODELS.pop(0)  # this endpoint does not serve the flash variant
                continue
            response.raise_for_status()
            data = response.json()
            USAGE.clef_calls += 1
            USAGE.clef_in += data.get("usage", {}).get("input_tokens", 0)
            return data["answers"]
        except Exception as exc:
            error = exc
            await asyncio.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"clef failed: {type(error).__name__}: {str(error)[:200]}")


def choice(question: str, options: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": question, "criteria": options}


def clock() -> float:
    return time.monotonic()


START = time.monotonic()


def stamp() -> str:
    """Seconds since start, for the logs: where the run's time goes."""
    return f"{time.monotonic() - START:6.1f}s"
