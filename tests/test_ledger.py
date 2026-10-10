from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[1] / "my-agent"
sys.path.insert(0, str(PACKAGE))

from rfq.evidence.ledger import Ledger  # noqa: E402


class SigningEvidence(unittest.TestCase):
    def test_nested_evidence_uses_observed_frame_site_and_url(self):
        ledger = Ledger(SimpleNamespace(emit=lambda *args: None))
        target = {"siteId": "second"}
        session = SimpleNamespace(site={"siteId": "first"}, current_site={"siteId": "first"},
            page=SimpleNamespace(url="https://first.example.test/", frames=[
                SimpleNamespace(url="https://first.example.test/"),
                SimpleNamespace(url="https://second.example.test/details")]),
            sites_by_origin={"https://second.example.test": target}, last_snapshot=None)
        ledger.add("TenderInfo", {"reference": "synthetic-ref"}, session, "tender", "synthetic", "frame/1")
        provenance = ledger.records[0][2]
        self.assertEqual(provenance.site_id, "second")
        self.assertEqual(provenance.url, "https://second.example.test/details")
        self.assertEqual(provenance.frame_path, "frame/1")

    def test_signing_code_requires_phase_authority_and_is_not_logged(self):
        events = []
        bus = SimpleNamespace(emit=lambda *args: events.append(args))
        ledger = Ledger(bus)
        session = SimpleNamespace(site={"siteId": "synthetic-mail"},
                                  page=SimpleNamespace(url="https://mail.example.test/"),
                                  last_snapshot=None)
        code = "synthetic-signing-code"
        denied = ledger.add("TAN", {"value": code}, session, "mail", code)
        self.assertIn("only during", denied)
        self.assertFalse(ledger.records)
        ledger.allow_tan = True
        self.assertEqual(ledger.add("TAN", {"value": code}, session, "mail", code), "Recorded TAN")
        self.assertEqual(ledger.of("TAN")[0].value, code)
        self.assertNotIn(code, str(events))
        self.assertNotIn(code, ledger.summary())


class CapturingBus:
    def __init__(self):
        self.events = []

    def emit(self, event_type, agent, data):
        event = {"type": event_type, "agent": agent, "data": data}
        self.events.append(event)
        return event


class SigningCodeLedger(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.reference = "RFQ-SYNTH-314"
        self.request_id = "request-synthetic-271"
        self.code = "synthetic-code-981"
        self.bus = CapturingBus()
        self.ledger = Ledger(self.bus)
        self.ledger.allow_tan = True
        self.ledger.tan_context = {
            "reference": self.reference,
            "request_id": self.request_id,
            "requested_after": self.now - timedelta(seconds=20),
        }
        self.session = SimpleNamespace(
            site={"siteId": "synthetic-mail"},
            page=SimpleNamespace(url="http://127.0.0.1/synthetic-message"),
            last_snapshot=SimpleNamespace(text="URL synthetic\nSynthetic message", digest="synthetic"),
        )

    def record(self, **changes):
        now = datetime.now(timezone.utc)
        value = {
            "value": self.code,
            "reference": self.reference,
            "request_id": self.request_id,
            "requested_at": now - timedelta(seconds=3),
            "expires_at": now + timedelta(minutes=5),
        }
        value.update(changes)
        return value

    def add(self, **changes):
        return self.ledger.add("TAN", self.record(**changes), self.session, "synthetic-agent", "synthetic message")

    def test_wrong_reference_is_rejected(self):
        result = self.add(reference="RFQ-SYNTH-OTHER")
        self.assertIn("exact tender reference", result)
        self.assertEqual(self.ledger.of("TAN"), [])

    def test_code_dated_on_the_scenario_clock_is_accepted_and_stays_out_of_telemetry(self):
        # The websites run on their own calendar, so a message date says nothing against the real clock.
        years_away = datetime.now(timezone.utc) + timedelta(days=400)
        result = self.add(requested_at=years_away, expires_at=None, request_id=None)

        self.assertEqual(result, "Recorded TAN")
        self.assertEqual([record.value for record in self.ledger.of("TAN")], [self.code])
        event = next(event for event in self.bus.events if event["type"] == "record_added")
        self.assertEqual(event["data"]["record"]["value"], "[ephemeral]")
        self.assertNotIn(self.code, str(event))

if __name__ == "__main__":
    unittest.main()
