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

USAGE = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
         "prompt_tokens_details": {"cached_tokens": 400}}
COST = (600 * 1.0 + 400 * 0.1 + 100 * 2.0) / 1e6  # at the synthetic prices configured below


def chunk(delta=None, finish=None, usage=None):
    body = {"id": "synthetic", "object": "chat.completion.chunk", "created": 0, "model": "synthetic-model",
            "choices": [] if delta is None and finish is None else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        body["usage"] = usage
    return body


ANSWER = [chunk({"role": "assistant", "content": "synthetic "}), chunk({"content": "answer"}), chunk({}, "stop"), chunk(usage=USAGE)]
CUT_OFF = [chunk({"role": "assistant", "content": None}), chunk({}, "length"), chunk(usage=USAGE)]
TOOL_CALL = [
    chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_synthetic", "type": "function",
                                                "function": {"name": "browser", "arguments": ""}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"actions": [{"kind": "cli'}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ck", "id": 7}], "reason": "synthetic"}'}}]}),
    chunk({"tool_calls": [{"index": 1, "id": "call_second", "type": "function",
                           "function": {"name": "record", "arguments": '{"kind": "Synthetic"}'}}]}),
    chunk({}, "tool_calls"), chunk(usage=USAGE)]


class Bus:
    def __init__(self):
        self.events = []

    def emit(self, kind, agent, data):
        self.events.append((kind, agent, data))

    def of(self, kind):
        return [data for name, _, data in self.events if name == kind]


class ModelEndpoint(unittest.IsolatedAsyncioTestCase):
    """A stand-in model endpoint whose successive requests behave as scripted."""

    async def serve(self, *script):
        """Each script entry handles one request: 'silent', 'half' (starts, then stops sending) or a chunk list."""
        self.script = list(script)
        self.requests = []
        self.held = []

        async def handle(reader, writer):
            headers = await reader.readuntil(b"\r\n\r\n")
            length = int(next((line.split(b":")[1] for line in headers.split(b"\r\n")
                               if line.lower().startswith(b"content-length")), b"0"))
            request = json.loads(await reader.readexactly(length))
            self.requests.append(request)
            plan = self.script[min(len(self.requests), len(self.script)) - 1]
            if plan == "silent":
                self.held.append(writer)  # connection accepted, nothing ever sent
                return
            chunks = ANSWER if plan in ("half", "thinking") else plan
            if not request.get("stream"):
                text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
                finish = next(c["choices"][0]["finish_reason"] for c in chunks if c["choices"] and c["choices"][0]["finish_reason"])
                body = json.dumps({"id": "synthetic", "object": "chat.completion", "created": 0, "model": "synthetic-model",
                                   "choices": [{"index": 0, "finish_reason": finish,
                                                "message": {"role": "assistant", "content": text or None}}],
                                   "usage": USAGE}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: "
                             + str(len(body)).encode() + b"\r\nconnection: close\r\n\r\n" + body)
                await writer.drain()
                writer.close()
                return
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n")
            if plan == "thinking":  # reasoning arrives steadily and no answer ever begins
                try:
                    for _ in range(100):
                        writer.write(b"data: " + json.dumps(chunk({"reasoning_content": "synthetic thought "})).encode() + b"\n\n")
                        await writer.drain()
                        await asyncio.sleep(0.05)
                except ConnectionError:
                    pass
                return
            for position, part in enumerate(chunks):
                writer.write(b"data: " + json.dumps(part).encode() + b"\n\n")
                await writer.drain()
                if plan == "half" and position == 0:
                    self.held.append(writer)  # the answer began and then went quiet
                    return
            writer.write(b"data: [DONE]\n\n")
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
                                   model_stream=True, model_first_chunk_timeout_s=1, model_chunk_gap_timeout_s=1,
                                   model_call_timeout_s=6, model_first_call_timeout_s=1, model_retries=2)
        for name, value in overrides.items():
            setattr(settings, name, value)
        self.bus = Bus()
        with patch.dict(os.environ, {"SYNTHETIC_URL": url, "SYNTHETIC_KEY": "synthetic-key"}):
            return ModelClient(settings, self.bus)

    async def ask(self, model, **kwargs):
        return await model.complete("worker", [{"role": "user", "content": "synthetic question"}], **kwargs)

    async def test_a_streamed_answer_is_assembled_and_priced(self):
        model = self.client(await self.serve(ANSWER))

        reply = await self.ask(model)

        self.assertEqual(reply.content, "synthetic answer")
        self.assertIsNone(reply.tool_calls)
        self.assertTrue(self.requests[0]["stream"])
        [call] = self.bus.of("model_call")
        self.assertEqual((call["finish"], call["tokens_in"], call["tokens_cached"], call["tokens_out"]), ("stop", 1000, 400, 100))
        self.assertAlmostEqual(call["cost"], COST)
        self.assertAlmostEqual(model.total_cost, COST)

    async def test_tool_calls_split_across_chunks_are_put_back_together(self):
        model = self.client(await self.serve(TOOL_CALL))

        reply = await self.ask(model, tools=[{"type": "function", "function": {"name": "browser", "parameters": {"type": "object"}}}])

        self.assertEqual([call.function.name for call in reply.tool_calls], ["browser", "record"])
        self.assertEqual(json.loads(reply.tool_calls[0].function.arguments),
                         {"actions": [{"kind": "click", "id": 7}], "reason": "synthetic"})
        self.assertEqual(json.loads(reply.tool_calls[1].function.arguments), {"kind": "Synthetic"})
        self.assertEqual(reply.tool_calls[0].id, "call_synthetic")
        self.assertEqual(self.requests[0]["tool_choice"], "required")

    async def test_a_call_that_sends_nothing_is_abandoned_quickly_and_sent_again(self):
        model = self.client(await self.serve("silent", ANSWER))
        started = time.monotonic()

        reply = await self.ask(model)

        self.assertEqual(reply.content, "synthetic answer")
        self.assertEqual(len(self.requests), 2)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual([(r["attempt"], r["of"]) for r in self.bus.of("model_retry")], [(1, 3)])
        self.assertAlmostEqual(model.total_cost, COST)

    async def test_an_answer_that_starts_and_then_goes_quiet_is_sent_again(self):
        model = self.client(await self.serve("half", ANSWER))
        started = time.monotonic()

        reply = await self.ask(model)

        self.assertEqual(reply.content, "synthetic answer")  # not the fragment "synthetic " from the abandoned attempt
        self.assertEqual(len(self.requests), 2)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(self.bus.of("model_retry")), 1)

    async def test_an_endpoint_that_never_answers_fails_within_the_bound(self):
        model = self.client(await self.serve("silent"), model_retries=1)
        started = time.monotonic()

        with self.assertRaises(Exception):
            await self.ask(model)

        self.assertEqual(len(self.requests), 2)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(len(self.bus.of("model_retry")), 2)
        self.assertEqual(model.total_cost, 0.0)

    async def test_a_cut_off_answer_is_asked_again_with_a_larger_allowance_and_both_are_priced(self):
        model = self.client(await self.serve(CUT_OFF, ANSWER))

        reply = await self.ask(model, max_tokens=800)

        self.assertEqual(reply.content, "synthetic answer")
        self.assertEqual([request["max_tokens"] for request in self.requests], [800, 1600])
        self.assertEqual([call["finish"] for call in self.bus.of("model_call")], ["length", "stop"])
        self.assertAlmostEqual(model.total_cost, 2 * COST)

    async def test_an_answer_still_cut_off_after_the_last_attempt_is_returned_as_it_is(self):
        model = self.client(await self.serve(CUT_OFF), model_retries=1)

        reply = await self.ask(model, max_tokens=800)

        self.assertIsNone(reply.content)
        self.assertEqual([request["max_tokens"] for request in self.requests], [800, 1600])

    async def test_an_answer_used_up_by_reasoning_is_asked_again_without_reasoning(self):
        model = self.client(await self.serve(CUT_OFF, ANSWER), model_light_body={"synthetic_switch": "plain"}, model_retries=0)

        reply = await self.ask(model, max_tokens=800)

        self.assertEqual(reply.content, "synthetic answer")
        self.assertEqual([request.get("synthetic_switch") for request in self.requests], [None, "plain"])
        self.assertEqual([request["max_tokens"] for request in self.requests], [800, 800])

    async def test_reasoning_that_runs_on_is_cut_short_and_asked_again_without_reasoning(self):
        model = self.client(await self.serve("thinking", ANSWER), model_light_body={"synthetic_switch": "plain"},
                            model_think_limit_s=0.3)
        started = time.monotonic()

        reply = await self.ask(model)

        self.assertEqual(reply.content, "synthetic answer")
        self.assertLess(time.monotonic() - started, 3)  # the reasoning alone would have gone on for five seconds
        self.assertEqual([request.get("synthetic_switch") for request in self.requests], [None, "plain"])
        self.assertEqual([r["next"] for r in self.bus.of("model_retry")], ["without reasoning"])

    async def test_only_the_first_answer_gets_the_long_wake_up_allowance(self):
        model = self.client(await self.serve(ANSWER, "silent", ANSWER), model_first_call_timeout_s=30)
        await self.ask(model)
        self.assertTrue(model.answered)

        started = time.monotonic()
        await self.ask(model)

        self.assertEqual(len(self.requests), 3)
        self.assertLess(time.monotonic() - started, 4)

    async def test_before_the_first_answer_a_slow_start_is_waited_for(self):
        model = self.client(await self.serve("silent", ANSWER), model_first_call_timeout_s=3)
        started = time.monotonic()

        await self.ask(model)

        # The 1 s first-chunk limit does not apply yet: the silent first request is given the 3 s wake allowance.
        self.assertGreater(time.monotonic() - started, 2.9)
        self.assertEqual(len(self.requests), 2)

    async def test_configured_extra_parameters_reach_the_endpoint(self):
        model = self.client(await self.serve(ANSWER), model_extra_body={"synthetic_switch": {"mode": "plain"}})

        await self.ask(model)

        self.assertEqual(self.requests[0]["synthetic_switch"], {"mode": "plain"})
        self.assertEqual(self.requests[0]["model"], "synthetic-model")

    async def test_a_provider_without_streaming_is_served_by_the_whole_answer_path(self):
        model = self.client(await self.serve("silent", ANSWER), model_stream=False, model_call_timeout_s=1)

        reply = await self.ask(model)

        self.assertEqual(reply.content, "synthetic answer")
        self.assertNotIn("stream", self.requests[1])
        self.assertEqual(len(self.bus.of("model_retry")), 1)
        self.assertAlmostEqual(model.total_cost, COST)


if __name__ == "__main__":
    unittest.main()
