from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PACKAGE = Path(__file__).resolve().parents[1] / "my-agent"
sys.path.insert(0, str(PACKAGE))

from rfq.agents.base import BrowserAgent  # noqa: E402
from rfq.evidence.ledger import Ledger  # noqa: E402


def tool_reply(name: str, arguments: dict) -> SimpleNamespace:
    call = SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))
    return SimpleNamespace(tool_calls=[call], content=None)


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, *_args, **_kwargs):
        self.calls += 1
        return self.replies.pop(0)


class FakeBus:
    def __init__(self):
        self.events = []

    def emit(self, event_type, agent, data):
        event = {"type": event_type, "agent": agent, "data": data}
        self.events.append(event)
        return event


class AgentRecordStop(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, replies, *, allow_tan: bool):
        bus = FakeBus()
        session = SimpleNamespace(
            site={"siteId": "synthetic-site", "credentials": {}},
            page=SimpleNamespace(url="http://127.0.0.1/synthetic"),
            last_snapshot=SimpleNamespace(text="URL synthetic\nSynthetic evidence", digest="synthetic", elements={}),
            last_good_url="",
            last_good_urls={},
            sites_by_origin={},
            nav_hashes=set(),
        )
        ledger = Ledger(bus)
        ledger.allow_tan = allow_tan
        model = FakeModel(replies)
        settings = SimpleNamespace(max_steps=3, run_token=None)
        agent = BrowserAgent("synthetic-agent", session, model, bus, ledger,
                             {"brief": "Synthetic task"}, settings, {})
        snapshot = SimpleNamespace(text="Synthetic observation", digest="synthetic", elements={})
        return agent, model, ledger, bus, session, snapshot

    @staticmethod
    def tan_record(value):
        return tool_reply("record", {"kind": "TAN", "data": {"value": value}, "snippet": "synthetic code"})

    @staticmethod
    def done_reply():
        return tool_reply("done", {"summary": "Synthetic task complete"})

    async def run_with_mocks(self, agent, snapshot, *, stop_record="TAN"):
        with patch("rfq.agents.base.recover", new=AsyncMock()), \
             patch("rfq.agents.base.observe", new=AsyncMock(return_value=snapshot)):
            return await agent.run("Synthetic task", max_steps=3, stop_record=stop_record)

    async def test_newly_recorded_valid_tan_stops_after_one_model_call(self):
        secret = "synthetic-code-alpha"
        agent, model, ledger, bus, _session, snapshot = self.make_agent(
            [self.tan_record(secret)], allow_tan=True)

        result = await self.run_with_mocks(agent, snapshot)

        self.assertEqual(model.calls, 1)
        self.assertEqual([record.value for record in ledger.of("TAN")], [secret])
        self.assertNotIn(secret, str(result))
        self.assertNotIn(secret, str(bus.events))

    async def test_phase_denied_tan_does_not_stop_before_done(self):
        agent, model, ledger, _bus, _session, snapshot = self.make_agent(
            [self.tan_record("synthetic-code-denied"), self.done_reply()], allow_tan=False)

        result = await self.run_with_mocks(agent, snapshot)

        self.assertEqual(model.calls, 2)
        self.assertEqual(ledger.of("TAN"), [])
        self.assertEqual(result["summary"], "Synthetic task complete")

    async def test_duplicate_tan_does_not_stop_but_new_code_on_next_turn_does(self):
        old_secret = "synthetic-code-old"
        new_secret = "synthetic-code-new"
        agent, model, ledger, bus, session, snapshot = self.make_agent(
            [self.tan_record(old_secret), self.tan_record(new_secret), self.done_reply()], allow_tan=True)
        self.assertEqual(ledger.add("TAN", {"value": old_secret}, session, "setup", "synthetic preload"),
                         "Recorded TAN")

        result = await self.run_with_mocks(agent, snapshot)

        self.assertEqual(model.calls, 2)
        self.assertEqual([record.value for record in ledger.of("TAN")], [old_secret, new_secret])
        self.assertNotIn(old_secret, str(result))
        self.assertNotIn(new_secret, str(result))
        self.assertNotIn(old_secret, str(bus.events))
        self.assertNotIn(new_secret, str(bus.events))


if __name__ == "__main__":
    unittest.main()
