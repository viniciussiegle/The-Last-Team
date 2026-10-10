from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1] / "my-agent"
sys.path.insert(0, str(PACKAGE))

from rfq.llm.clients import ModelClient  # noqa: E402

REPLY = {"id": "synthetic", "object": "chat.completion", "created": 0, "model": "synthetic-model", "choices": [
    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "synthetic answer"}}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
              "prompt_tokens_details": {"cached_tokens": 400}}}


class Bus:
    def __init__(self):
        self.events = []

    def emit(self, kind, agent, data):
        self.events.append((kind, agent, data))


class StalledThenAnswering(unittest.IsolatedAsyncioTestCase):
    """A model endpoint that leaves its first requests unanswered, then answers normally."""

    async def serve(self, silent_requests, cut_off_requests=0):
        self.requests = 0
        self.held = []
        self.allowances = []

        async def handle(reader, writer):
            headers = await reader.readuntil(b"\r\n\r\n")
            length = int(next((line.split(b":")[1] for line in headers.split(b"\r\n")
                               if line.lower().startswith(b"content-length")), b"0"))
            self.allowances.append(json.loads(await reader.readexactly(length)).get("max_tokens"))
            self.requests += 1
            if self.requests <= silent_requests:
                self.held.append(writer)  # keep the connection open and say nothing
                return
            reply = REPLY
            if self.requests <= silent_requests + cut_off_requests:
                reply = {**REPLY, "choices": [{"index": 0, "finish_reason": "length",
                                               "message": {"role": "assistant", "content": None}}]}
            body = json.dumps(reply).encode()
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: "
                         + str(len(body)).encode() + b"\r\nconnection: close\r\n\r\n" + body)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.addAsyncCleanup(self.stop, server)
        return f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"

    async def stop(self, server):
        for writer in self.held:
            writer.close()
        server.close()
        await server.wait_closed()

    def client(self, url, **overrides):
        settings = SimpleNamespace(model_name="synthetic-model", model_base_url_env="SYNTHETIC_URL",
                                   model_api_key_env="SYNTHETIC_KEY", model_extra_body={},
                                   model_prices={"input": 1.0, "cached": 0.1, "output": 2.0}, model_concurrency=4,
                                   model_call_timeout_s=1, model_first_call_timeout_s=1, model_retries=2)
        for name, value in overrides.items():
            setattr(settings, name, value)
        self.bus = Bus()
        with patch.dict(os.environ, {"SYNTHETIC_URL": url, "SYNTHETIC_KEY": "synthetic-key"}):
            return ModelClient(settings, self.bus)

    async def test_a_silent_call_is_abandoned_and_sent_again(self):
        model = self.client(await self.serve(silent_requests=1))
        started = time.monotonic()

        reply = await model.complete("worker", [{"role": "user", "content": "synthetic question"}])

        self.assertEqual(reply.content, "synthetic answer")
        self.assertEqual(self.requests, 2)
        self.assertLess(time.monotonic() - started, 8)
        retries = [data for kind, _, data in self.bus.events if kind == "model_retry"]
        self.assertEqual([(r["attempt"], r["of"]) for r in retries], [(1, 3)])
        [call] = [data for kind, _, data in self.bus.events if kind == "model_call"]
        # 600 fresh input, 400 cached, 100 output at the configured prices per million tokens
        self.assertAlmostEqual(call["cost"], (600 * 1.0 + 400 * 0.1 + 100 * 2.0) / 1e6)
        self.assertAlmostEqual(model.total_cost, call["cost"])

    async def test_an_endpoint_that_never_answers_fails_within_the_bound(self):
        model = self.client(await self.serve(silent_requests=99), model_retries=1)
        started = time.monotonic()

        with self.assertRaises(Exception):
            await model.complete("worker", [{"role": "user", "content": "synthetic question"}])

        self.assertEqual(self.requests, 2)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(len([1 for kind, _, _ in self.bus.events if kind == "model_retry"]), 2)
        self.assertEqual(model.total_cost, 0.0)

    async def test_a_cut_off_answer_is_asked_again_with_a_larger_allowance_and_both_are_priced(self):
        model = self.client(await self.serve(silent_requests=0, cut_off_requests=1))

        reply = await model.complete("worker", [{"role": "user", "content": "synthetic question"}], max_tokens=800)

        self.assertEqual(reply.content, "synthetic answer")
        self.assertEqual(self.allowances, [800, 1600])
        calls = [data for kind, _, data in self.bus.events if kind == "model_call"]
        self.assertEqual([call["finish"] for call in calls], ["length", "stop"])
        self.assertAlmostEqual(model.total_cost, sum(call["cost"] for call in calls))
        self.assertGreater(calls[0]["cost"], 0)

    async def test_an_answer_still_cut_off_after_the_last_attempt_is_returned_as_it_is(self):
        model = self.client(await self.serve(silent_requests=0, cut_off_requests=99), model_retries=1)

        reply = await model.complete("worker", [{"role": "user", "content": "synthetic question"}], max_tokens=800)

        self.assertIsNone(reply.content)
        self.assertEqual(self.allowances, [800, 1600])

    async def test_only_the_first_answer_gets_the_long_wake_up_allowance(self):
        model = self.client(await self.serve(silent_requests=0), model_first_call_timeout_s=30, model_call_timeout_s=1)
        await model.complete("worker", [{"role": "user", "content": "synthetic question"}])
        self.assertTrue(model.answered)
        self.requests = -1  # the next request is left unanswered once

        started = time.monotonic()
        await model.complete("worker", [{"role": "user", "content": "synthetic question"}])

        self.assertLess(time.monotonic() - started, 8)

    async def test_configured_extra_parameters_reach_the_endpoint(self):
        seen = {}
        original = ModelClient.complete

        model = self.client(await self.serve(silent_requests=0), model_extra_body={"synthetic_switch": {"mode": "plain"}})
        create = model.client.chat.completions.create

        async def recording(**kwargs):
            seen.update(kwargs)
            return await create(**kwargs)
        model.client.chat.completions.create = recording

        await original(model, "worker", [{"role": "user", "content": "synthetic question"}])

        self.assertEqual(seen["extra_body"], {"synthetic_switch": {"mode": "plain"}})
        self.assertEqual(seen["model"], "synthetic-model")


if __name__ == "__main__":
    unittest.main()
