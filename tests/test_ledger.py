from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "my-agent"))
from rfq.evidence.ledger import Ledger


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
