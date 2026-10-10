from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[1] / "my-agent"
sys.path.insert(0, str(PACKAGE))

from rfq.evidence.ledger import Ledger  # noqa: E402
from rfq.evidence.models import MailRecord  # noqa: E402
from tests.rules.test_engine import condition, material, position, requirement, spec  # noqa: E402


class CapturingBus:
    def emit(self, *_args, **_kwargs):
        return {}


class EvidenceSummary(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(CapturingBus())
        self.session = SimpleNamespace(
            site={"siteId": "synthetic-site"},
            page=SimpleNamespace(url="http://127.0.0.1/synthetic-evidence"),
            last_snapshot=SimpleNamespace(text="URL synthetic\nSynthetic evidence", digest="synthetic"),
        )

    def add(self, kind, record):
        self.assertEqual(self.ledger.add(kind, record, self.session, "synthetic-agent", "synthetic evidence"),
                         "Recorded " + kind)

    def test_erp_compaction_preserves_governing_pages_and_optional_material_facts(self):
        revision = "REV-SYNTHETIC-CURRENT"
        self.add("SpecRevision", spec([requirement("REQ-SYNTH-A")], revision=revision))
        self.add("SpecRevision", spec([requirement("REQ-SYNTH-B", bore="30")], revision=revision))
        self.add("TenderPosition", position("POS-SYNTH-A", "4", requirement_ref="REQ-SYNTH-A"))
        self.add("TenderPosition", position("POS-SYNTH-B", "7", requirement_ref="REQ-SYNTH-B"))

        known_stock = material("MAT-SYNTH", stock_qty="23", lead_weeks_stock=3,
                               lead_weeks_production=8, carton_size=None)
        partial_update = material("MAT-SYNTH", stock_qty=None, lead_weeks_stock=None,
                                  lead_weeks_production=None, carton_size="6")
        self.add("Material", known_stock)
        self.add("Material", partial_update)
        self.add("PriceCondition", condition("MAT-SYNTH", price="14.25"))

        now = datetime.now(timezone.utc)
        for index in range(24):
            self.add("MailRecord", MailRecord(
                message_id=f"synthetic-mail-{index:02d}",
                folder="archive",
                sender="synthetic@example.test",
                date=now,
                subject="Synthetic long message",
                reference="RFQ-SYNTHETIC-314",
                kind="discount_approval",
                payload={"percent": "5"},
                raw_text=("Synthetic archived correspondence. " * 250),
            ))

        before_records = list(self.ledger.records)
        before_keys = set(self.ledger.keys)
        budget = 2800
        encoded = self.ledger.summary(role="erp", max_chars=budget)
        parsed = json.loads(encoded)

        self.assertLessEqual(len(encoded), budget)
        self.assertEqual(set(parsed), {"records", "omitted"})
        self.assertGreater(parsed["omitted"].get("MailRecord", 0), 0)
        self.assertNotIn("raw_text", encoded)

        specs = [record for record in parsed["records"]
                 if record["record_type"] == "SpecRevision" and record["revision_id"] == revision]
        self.assertEqual(len(specs), 1)
        self.assertEqual({row["requirement_id"] for row in specs[0]["requirements"]},
                         {"REQ-SYNTH-A", "REQ-SYNTH-B"})

        materials = [record for record in parsed["records"]
                     if record["record_type"] == "Material" and record["number"] == "MAT-SYNTH"]
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0]["stock_qty"], "23")
        self.assertEqual(materials[0]["lead_weeks_stock"], 3)
        self.assertEqual(materials[0]["lead_weeks_production"], 8)
        self.assertEqual(materials[0]["carton_size"], "6")

        self.assertEqual(self.ledger.records, before_records)
        self.assertEqual(self.ledger.keys, before_keys)
        self.assertEqual(len(self.ledger.of("MailRecord")), 24)


if __name__ == "__main__":
    unittest.main()
