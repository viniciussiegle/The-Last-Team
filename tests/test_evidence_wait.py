from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[1] / "my-agent"
sys.path.insert(0, str(PACKAGE))

from rfq.evidence.ledger import Ledger  # noqa: E402


class EvidenceWait(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ledger = Ledger(SimpleNamespace(emit=lambda *_args: None))
        self.session = SimpleNamespace(
            site={"siteId": "synthetic-site"},
            page=SimpleNamespace(url="http://127.0.0.1/synthetic"),
            last_snapshot=None,
        )

    def add_tender_info(self):
        return self.ledger.add(
            "TenderInfo",
            {"reference": "RFQ-SYNTH-481", "title": "Synthetic tender"},
            self.session,
            "synthetic-agent",
            "synthetic observation",
        )

    async def test_matching_record_wakes_waiter(self):
        waiter = asyncio.create_task(self.ledger.wait_for_change("TenderInfo", 0, 1.0))
        await asyncio.sleep(0)
        self.assertEqual(self.add_tender_info(), "Recorded TenderInfo")
        self.assertTrue(await waiter)

    async def test_unrelated_record_does_not_satisfy_requested_kind(self):
        waiter = asyncio.create_task(self.ledger.wait_for_change("Material", 0, 0.02))
        await asyncio.sleep(0)
        self.add_tender_info()
        self.assertFalse(await waiter)

    async def test_existing_count_returns_immediately(self):
        self.add_tender_info()
        self.assertTrue(await self.ledger.wait_for_change("TenderInfo", 0, 0))

    async def test_timeout_returns_false(self):
        self.assertFalse(await self.ledger.wait_for_change("TenderInfo", 0, 0))

    async def test_unknown_kind_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown evidence kind"):
            await self.ledger.wait_for_change("UnknownSyntheticKind", 0, 0)


if __name__ == "__main__":
    unittest.main()
