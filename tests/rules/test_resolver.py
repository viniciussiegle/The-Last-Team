from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "my-agent"
sys.path.insert(0, str(PACKAGE))

from rfq.evidence.models import MailRecord, QAEntry, TenderInfo, TenderPosition  # noqa: E402
from rfq.evidence.resolver import resolve  # noqa: E402


REFERENCE = "RFQ-2031-042"
SCENARIO = "2031-04-15T12:00:00+00:00"
DOMAIN = "supplier.example.test"
AUTH = {"discount_approval": "leitung", "konditionsregeln": "kalkulation"}
CONTEXT = {"reference": REFERENCE, "scenario_date": SCENARIO, "bidder_domain": DOMAIN,
           "authorized_senders": AUTH}


class FakeLedger:
    def __init__(self, **records):
        self.records = records

    def of(self, kind):
        return list(self.records.get(kind, []))


def mail(message_id, percent, date, *, reference=REFERENCE, sender="leitung", confidence="high", kind="discount_approval"):
    return MailRecord(message_id=message_id, folder="archive", sender=f"{sender}@{DOMAIN}", date=date,
                      subject="Synthetic approval", reference=reference, kind=kind,
                      payload={"percent": str(percent)}, confidence=confidence)


def qa(number, kind, date, *, withdraws=None, quantity=None, pid="P1"):
    payload = {"quantity": str(quantity)} if quantity is not None else {}
    return QAEntry(number=number, date=date, kind=kind, position_id=pid, payload=payload,
                   withdraws_number=withdraws)


class MailAuthorityAndRecency(unittest.TestCase):
    def test_reference_authority_scenario_cutoff_and_latest_record_are_applied(self):
        messages = [
            mail("approval-old", "5", "2031-04-01T08:00:00+00:00"),
            mail("approval-current", "15", SCENARIO, reference="  rfq-2031-042  "),
            mail("approval-future", "25", "2031-04-15T12:00:01+00:00"),
            mail("approval-other-ref", "35", "2031-04-14T08:00:00+00:00", reference="RFQ-2031-999"),
            mail("approval-other-sender", "45", "2031-04-15T11:00:00+00:00", sender="sales"),
        ]
        resolved = resolve(FakeLedger(MailRecord=messages), CONTEXT)
        self.assertEqual(resolved.mails["discount_approval"].message_id, "approval-current")
        rejected = {item["id"]: item["reason"] for item in resolved.rejected}
        self.assertEqual(rejected["approval-old"], "superseded")
        self.assertEqual(rejected["approval-future"], "future-dated")
        self.assertEqual(rejected["approval-other-ref"], "other reference")
        self.assertEqual(rejected["approval-other-sender"], "unauthorized sender")

    def test_bidder_domain_comes_from_tender_info_and_low_confidence_is_a_gap(self):
        information = TenderInfo(reference=REFERENCE, bidder_domain="Supplier.Example.Test")
        good = mail("known-domain", "8", "2031-04-10T08:00:00+00:00")
        doubtful = mail("uncertain-clause", "9", SCENARIO, confidence="low", kind="konditionsregeln",
                        sender="kalkulation")
        resolved = resolve(FakeLedger(TenderInfo=[information], MailRecord=[good, doubtful]),
                           {**CONTEXT, "bidder_domain": "untrusted.example.test"})
        self.assertEqual(resolved.mails["discount_approval"].message_id, "known-domain")
        self.assertNotIn("konditionsregeln", resolved.mails)
        self.assertTrue(any("low-confidence" in gap for gap in resolved.gaps))
        self.assertIn({"kind": "MailRecord", "id": "uncertain-clause",
                       "reason": "latest record awaiting clarification"}, resolved.rejected)

    def test_newest_low_confidence_approval_does_not_revive_older_approval(self):
        older = mail("approved-earlier", "8", "2031-04-10T08:00:00+00:00")
        newest = mail("uncertain-newest", "15", SCENARIO, confidence="low")
        resolved = resolve(FakeLedger(MailRecord=[older, newest]), CONTEXT)

        self.assertNotIn("discount_approval", resolved.mails)
        self.assertTrue(any("uncertain-newest" in gap and "do not revive older records" in gap
                            for gap in resolved.gaps))
        rejected = {item["id"]: item["reason"] for item in resolved.rejected}
        self.assertEqual(rejected["uncertain-newest"], "latest record awaiting clarification")
        self.assertEqual(rejected["approved-earlier"], "superseded")


class EvidenceSelection(unittest.TestCase):
    def test_withdrawal_chain_restores_prior_change_and_future_withdrawal_is_ignored(self):
        changes = [
            qa("qty-first", "quantity_change", "2031-04-01T09:00:00+00:00", quantity="5"),
            qa("qty-later", "quantity_change", "2031-04-02T09:00:00+00:00", quantity="8"),
            qa("withdraw-later", "withdrawal", "2031-04-03T09:00:00+00:00", withdraws="qty-later"),
            qa("withdraw-withdrawal", "withdrawal", "2031-04-04T09:00:00+00:00", withdraws="withdraw-later"),
            qa("future-withdrawal", "withdrawal", "2031-04-16T09:00:00+00:00", withdraws="qty-first"),
        ]
        resolved = resolve(FakeLedger(QAEntry=changes), CONTEXT)
        kept = {entry.number for entry in resolved.qa}
        self.assertEqual(kept, {"qty-first", "qty-later", "withdraw-withdrawal"})
        self.assertNotIn("future-withdrawal", kept)
        self.assertEqual([entry.payload["quantity"] for entry in resolved.qa if entry.kind == "quantity_change"],
                         ["5", "8"])
        self.assertIn({"kind": "QAEntry", "id": "withdraw-later", "reason": "withdrawn"}, resolved.rejected)

    def test_only_current_specification_is_selected_and_conflicting_currents_are_reported(self):
        old = SimpleSpec("REV-OLD", "superseded")
        current = SimpleSpec("REV-CURRENT", "current")
        resolved = resolve(FakeLedger(SpecRevision=[old, current]), CONTEXT)
        self.assertEqual(resolved.spec.revision_id, "REV-CURRENT")
        self.assertFalse(any("Conflicting current" in gap for gap in resolved.gaps))

        competing = SimpleSpec("REV-OTHER", "current")
        conflicted = resolve(FakeLedger(SpecRevision=[current, competing]), CONTEXT)
        self.assertIsNone(conflicted.spec)
        self.assertTrue(any("Conflicting current" in gap for gap in conflicted.gaps))

    def test_duplicate_position_observations_keep_latest_record_by_identifier(self):
        earlier = TenderPosition(position_id="P1", description="first observation", quantity=Decimal("2"),
                                 unit="piece", requirement_ref="R1")
        later = TenderPosition(position_id="P1", description="later observation", quantity=Decimal("4"),
                               unit="piece", requirement_ref="R1")
        resolved = resolve(FakeLedger(TenderPosition=[earlier, later]), CONTEXT)
        self.assertEqual(len(resolved.positions), 1)
        self.assertEqual(resolved.positions[0].description, "later observation")
        self.assertEqual(resolved.positions[0].quantity, Decimal("4"))


class SimpleSpec:
    """Resolver only needs the status and revision identifier from this lightweight fake."""

    def __init__(self, revision_id, status):
        self.revision_id = revision_id
        self.status = status


if __name__ == "__main__":
    unittest.main()
