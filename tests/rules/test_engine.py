from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "my-agent"
sys.path.insert(0, str(PACKAGE))

from rfq.evidence.models import (  # noqa: E402
    MailRecord,
    Material,
    PriceCondition,
    QAEntry,
    Requirement,
    SearchCoverage,
    SpecRevision,
    TenderPosition,
)
from rfq.rules.engine import calculate  # noqa: E402


SCENARIO = "2031-04-15T12:00:00+00:00"
REFERENCE = "RFQ-2031-042"
DOMAIN = "supplier.example.test"
SENDERS = {
    "discount_approval": "leitung",
    "konditionsregeln": "kalkulation",
    "technikfreigabe": "technik",
    "verpackungstarife": "logistik",
    "terminfreigabe": "logistik",
    "devisenfixing": "finanzen",
    "ruestpools": "kalkulation",
    "projektbonus": "finanzen",
    "preisindizes": "kalkulation",
}


class FakeLedger:
    def __init__(self, **records):
        self.records = records

    def of(self, kind):
        return list(self.records.get(kind, []))


def context(**changes):
    value = {"reference": REFERENCE, "scenario_date": SCENARIO, "bidder_domain": DOMAIN,
             "authorized_senders": SENDERS}
    value.update(changes)
    return value


def requirement(requirement_id="R1", **changes):
    value = {"requirement_id": requirement_id, "katalog_group": "Group-A", "bore": "20", "stroke": "100",
             "stroke_kind": "nominal", "pressure": "10", "mounting": "Mount-A", "certification": "Cert-A"}
    value.update(changes)
    return Requirement.model_validate(value)


def spec(reqs=None, revision="REV-C", status="current", date=SCENARIO):
    return SpecRevision(revision_id=revision, status=status, date=date,
                        requirements=reqs if reqs is not None else [requirement()])


def position(pid="P1", quantity="1", unit="piece", requirement_ref="R1"):
    return TenderPosition(position_id=pid, description="Synthetic component", quantity=Decimal(quantity),
                          unit=unit, requirement_ref=requirement_ref)


def material(number="MAT-1", **changes):
    value = {"number": number, "status": "active", "katalog_group": "Group-A", "bore": "20", "stroke": "100",
             "pressure": "10", "mounting": "Mount-A", "certification": "Cert-A", "stock_qty": "100",
             "lead_weeks_stock": 2, "lead_weeks_production": 6, "setup_fee": "0", "surcharge_mass": "0"}
    value.update(changes)
    return Material.model_validate(value)


def condition(mat="MAT-1", price="10", scales=None, currency="EUR", unit_qty="1",
              valid_from="2031-04-15", valid_to="2031-04-15", fx_key=None):
    return PriceCondition(material=mat, valid_from=valid_from, valid_to=valid_to, currency=currency,
                          fx_key=fx_key, price_unit_qty=unit_qty,
                          scales=scales if scales is not None else [{"min_qty": "1", "price": price}])


def qa(number, kind, *, date=SCENARIO, pid=None, payload=None, withdraws=None, revision="REV-C", req="R1"):
    return QAEntry(number=number, date=date, kind=kind, position_id=pid, payload=payload or {},
                   withdraws_number=withdraws, revision_ref=revision, requirement_ref=req)


def mail(message_id, kind, payload, *, date=SCENARIO, reference=REFERENCE, local=None, confidence="high"):
    local = local or SENDERS[kind]
    return MailRecord(message_id=message_id, folder="inbox", sender=f"{local}@{DOMAIN}", date=date,
                      subject="Synthetic record", reference=reference, kind=kind, payload=payload,
                      confidence=confidence)


def ledger(positions=None, requirements=None, materials=None, prices=None, qa_entries=None, mails=None, coverage=None,
           specs=None):
    return FakeLedger(
        TenderPosition=positions if positions is not None else [position()],
        SpecRevision=specs if specs is not None else [spec(requirements)],
        Material=materials if materials is not None else [material()],
        PriceCondition=prices if prices is not None else [condition()],
        QAEntry=qa_entries or [], MailRecord=mails or [], SearchCoverage=coverage or [],
    )


def answer_lines(calculation):
    return {line["position"]: line for line in calculation.answer["positions"]}


class PricingAndSelection(unittest.TestCase):
    def test_later_partial_material_record_does_not_erase_known_stock_or_lead_times(self):
        known = material(stock_qty="8", lead_weeks_stock=2, lead_weeks_production=6)
        partial = material(stock_qty=None, lead_weeks_stock=None, lead_weeks_production=None)
        result = calculate(ledger(positions=[position(quantity="5")], materials=[known, partial]),
                           context(), core_only=True)

        self.assertTrue(result.complete, result.gaps)
        line = result.answer["positions"][0]
        self.assertEqual(line["quantity"], 5)
        self.assertEqual(line["material_number"], "MAT-1")
        self.assertEqual(line["lead_time_weeks"], 2)
        self.assertEqual(line["base_unit_price"], Decimal("10.00"))
        self.assertEqual(result.answer["total_net"], Decimal("50.00"))

    def test_partial_candidate_inspection_does_not_prove_no_bid(self):
        partial = ledger(materials=[material(status="blocked")], coverage=[
            SearchCoverage(katalog_group="Group-A", complete=True,
                           candidate_materials=["MAT-1", "MAT-UNREAD"])])
        result = calculate(partial, context(), core_only=True)
        self.assertEqual(result.answer["decision"], "bid")
        self.assertFalse(result.complete)
        self.assertTrue(any("search all active candidates" in gap for gap in result.gaps))

    def test_current_revision_requirements_can_arrive_on_separate_pages(self):
        first = spec([requirement("R-A")])
        second = spec([requirement("R-B")])
        result = calculate(ledger(
            positions=[position("P-A", "2", requirement_ref="R-A"),
                       position("P-B", "3", requirement_ref="R-B")],
            specs=[first, second]), context(), core_only=True)
        self.assertTrue(result.complete, result.gaps)
        self.assertEqual(set(answer_lines(result)), {"P-A", "P-B"})
        self.assertEqual(result.answer["total_net"], Decimal("50.00"))

    def test_core_only_prices_all_positions_with_unknown_commercial_adjustments(self):
        positions = [position("P-A", "2"), position("P-B", "3")]
        incomplete_commercials = material(setup_fee=None, surcharge_mass=None)
        approval = mail("core-discount", "discount_approval", {"percent": "10"})
        result = calculate(
            ledger(positions=positions, materials=[incomplete_commercials],
                   prices=[condition(price="12")], mails=[approval]),
            context(), core_only=True,
        )

        self.assertTrue(result.complete, result.gaps)
        lines = answer_lines(result)
        self.assertEqual(set(lines), {"P-A", "P-B"})
        for pid, quantity in (("P-A", 2), ("P-B", 3)):
            line = lines[pid]
            self.assertEqual(line["quantity"], quantity)
            self.assertEqual(line["base_unit_price"], Decimal("12.00"))
            self.assertEqual(line["unit_price"], Decimal("10.80"))
            self.assertEqual(line["lead_time_weeks"], 2)
            self.assertNotIn("surcharge_per_unit", line)
            self.assertNotIn("packaging_per_unit", line)
            self.assertNotIn("rebate_total", line)
        self.assertEqual(result.answer["total_net"], Decimal("54.00"))

    def test_condition_endpoints_and_discount_threshold_are_inclusive(self):
        approval = mail("discount-at-threshold", "discount_approval",
                        {"percent": "10", "min_order_value": "20"})
        result = calculate(ledger(positions=[position(quantity="2")], prices=[condition(price="10")], mails=[approval]),
                           context())
        self.assertTrue(result.complete)
        self.assertEqual(result.answer["discount_percent"], Decimal("10"))
        self.assertEqual(answer_lines(result)["P1"]["unit_price"], Decimal("9.00"))
        self.assertEqual(result.answer["total_net"], Decimal("18.00"))

    def test_order_below_conditional_approval_threshold_gets_no_discount(self):
        approval = mail("discount-minimum", "discount_approval",
                        {"percent": "10", "min_order_value": "20.01"})
        result = calculate(ledger(positions=[position(quantity="2")], prices=[condition(price="10")], mails=[approval]),
                           context())
        self.assertTrue(result.complete)
        self.assertEqual(result.answer["discount_percent"], Decimal("0"))
        self.assertEqual(answer_lines(result)["P1"]["unit_price"], Decimal("10.00"))

    def test_quantity_scales_are_chosen_per_position_and_stock_is_pooled(self):
        positions = [position("P1", "3"), position("P2", "4")]
        scaled = condition(scales=[{"min_qty": "1", "price": "10"}, {"min_qty": "5", "price": "8"}])
        shared = material(stock_qty="6", lead_weeks_stock=2, lead_weeks_production=7)
        result = calculate(ledger(positions=positions, materials=[shared], prices=[scaled]), context())
        self.assertTrue(result.complete)
        lines = answer_lines(result)
        self.assertEqual(lines["P1"]["base_unit_price"], Decimal("10.00"))
        self.assertEqual(lines["P2"]["base_unit_price"], Decimal("10.00"))
        self.assertEqual(lines["P1"]["lead_time_weeks"], 7)
        self.assertEqual(lines["P2"]["lead_time_weeks"], 7)
        self.assertEqual(result.answer["total_net"], Decimal("70.00"))

    def test_fx_and_price_unit_are_normalized_before_half_up_rounding(self):
        priced = condition(price="100.5", currency="USD", unit_qty="100", fx_key="USD-EUR")
        fx = mail("fx-rate", "devisenfixing", {"rates": {"USD-EUR": "1"}})
        result = calculate(ledger(prices=[priced], mails=[fx]), context())
        self.assertTrue(result.complete)
        self.assertEqual(answer_lines(result)["P1"]["base_unit_price"], Decimal("1.01"))
        self.assertEqual(result.answer["total_net"], Decimal("1.01"))

    def test_scenario_valid_metal_window_wins_over_adjacent_rates(self):
        metal = material(surcharge_mass="2", surcharge_index="IDX-A")
        rates = [{"valid_from": "2031-04-01", "valid_to": "2031-04-14", "rate": "2"},
                 {"valid_from": "2031-04-15", "valid_to": "2031-04-15", "rate": "3"},
                 {"valid_from": "2031-04-16", "valid_to": "2031-04-30", "rate": "99"}]
        index_mail = mail("metal-windows", "preisindizes", {"indices": {"IDX-A": rates}})
        result = calculate(ledger(materials=[metal], mails=[index_mail]), context())
        self.assertTrue(result.complete)
        self.assertEqual(answer_lines(result)["P1"]["surcharge_per_unit"], Decimal("6.00"))
        self.assertEqual(answer_lines(result)["P1"]["unit_price"], Decimal("16.00"))

    def test_commitment_ceiling_is_inclusive_for_pooled_material_demand(self):
        positions = [position("P1", "2"), position("P2", "3")]
        commitment_mail = mail("delivery-key", "terminfreigabe", {"commitments": {
            "FAST": {"material": "MAT-1", "lead_weeks": 1, "ceiling_qty": "5"}}})
        qas = [qa("qa-fast-1", "delivery_commitment_key", pid="P1", payload={"key": "FAST"}),
               qa("qa-fast-2", "delivery_commitment_key", pid="P2", payload={"key": "FAST"})]
        result = calculate(ledger(positions=positions, qa_entries=qas, mails=[commitment_mail]), context())
        self.assertTrue(result.complete)
        self.assertEqual({row["lead_time_weeks"] for row in result.answer["positions"]}, {1})

    def test_active_exact_material_is_required_and_no_bid_needs_complete_search(self):
        blocked_match = material(status="blocked")
        mismatch = material("MAT-2", stroke="101")
        incomplete = calculate(ledger(materials=[blocked_match, mismatch]), context())
        self.assertFalse(incomplete.complete)
        self.assertEqual(incomplete.answer["decision"], "bid")
        self.assertTrue(any("search all active candidates" in gap for gap in incomplete.gaps))

        proved = calculate(ledger(materials=[blocked_match, mismatch],
                                  coverage=[SearchCoverage(katalog_group="Group-A", complete=True,
                                                           candidate_materials=["MAT-1", "MAT-2"])]), context())
        self.assertTrue(proved.complete)
        self.assertEqual(proved.answer["decision"], "no_bid")
        self.assertEqual(proved.answer["positions"], [])
        self.assertEqual(proved.answer["total_net"], Decimal("0"))


class RequirementsAndUnits(unittest.TestCase):
    def test_current_revision_set_conversion_and_scoped_usable_stroke(self):
        old_requirement = requirement("R-OLD", stroke="700")
        current_requirement = requirement("R-CURRENT", stroke="80", stroke_kind="usable", pieces_per_set="3")
        old_spec = spec([old_requirement], revision="REV-OLD", status="superseded")
        current_spec = spec([current_requirement], revision="REV-CURRENT")
        positions = [position(quantity="2", unit="set", requirement_ref="R-CURRENT")]
        amendments = [
            qa("amend-stroke", "technical_amendment", payload={"attributes": {"stroke": "85"}},
               revision="REV-CURRENT", req="R-CURRENT"),
            qa("wrong-revision", "technical_amendment", payload={"attributes": {"bore": "99"}},
               revision="REV-OLD", req="R-CURRENT"),
            qa("wrong-requirement", "technical_amendment", payload={"attributes": {"pressure": "99"}},
               revision="REV-CURRENT", req="R-OLD"),
        ]
        reserve = mail("per-end-reserve", "technikfreigabe", {
            "reserves": [{"revision": "REV-CURRENT", "requirement": "R-CURRENT", "per_end": "5"}]})
        exact = material("MAT-95", stroke="95")
        result = calculate(ledger(positions=positions, specs=[old_spec, current_spec], materials=[exact],
                                  prices=[condition(mat="MAT-95", price="11")], qa_entries=amendments,
                                  mails=[reserve]), context())
        self.assertTrue(result.complete)
        line = result.answer["positions"][0]
        self.assertEqual(line["material_number"], "MAT-95")
        self.assertEqual(line["quantity"], 6)
        self.assertEqual(line["unit_price"], Decimal("11.00"))
        self.assertEqual(result.answer["total_net"], Decimal("66.00"))

    def test_non_matching_mounting_or_certification_cannot_substitute(self):
        wrong_mount = material("MAT-WRONG-MOUNT", mounting="Mount-B")
        wrong_cert = material("MAT-WRONG-CERT", certification="Cert-B")
        result = calculate(ledger(materials=[wrong_mount, wrong_cert],
                                  coverage=[SearchCoverage(katalog_group="Group-A", complete=True,
                                                           candidate_materials=[wrong_mount.number, wrong_cert.number])]),
                           context())
        self.assertEqual(result.answer["decision"], "no_bid")


class CommercialRules(unittest.TestCase):
    def test_condition_groups_apply_discount_to_the_defined_components(self):
        positions = [position(pid) for pid in ("P-A", "P-B", "P-C")]
        groups = [qa(f"group-{pid}", "condition_group", pid=pid, payload={"key": key})
                  for pid, key in (("P-A", "BASE"), ("P-B", "ALL"), ("P-C", "NONE"))]
        terms = mail("clauses", "konditionsregeln", {"groups": {
            "BASE": "base_only", "ALL": "base_plus_surcharge", "NONE": "none"}})
        approval = mail("discount", "discount_approval", {"percent": "10"})
        metal = material(surcharge_mass="1", surcharge_rate="10")
        result = calculate(ledger(positions=positions, materials=[metal], prices=[condition(price="100")],
                                  qa_entries=groups, mails=[terms, approval]), context())
        self.assertTrue(result.complete)
        lines = answer_lines(result)
        self.assertEqual(lines["P-A"]["unit_price"], Decimal("100.00"))
        self.assertEqual(lines["P-B"]["unit_price"], Decimal("99.00"))
        self.assertEqual(lines["P-C"]["unit_price"], Decimal("110.00"))
        self.assertEqual(result.answer["total_net"], Decimal("309.00"))

    def test_cartons_round_up_without_changing_bid_quantity(self):
        package_material = material(carton_size="4")
        q = qa("pack-key", "packaging_tariff_key", pid="P1", payload={"key": "TARIFF-A"})
        tariffs = mail("tariff", "verpackungstarife", {"tariffs": {"TARIFF-A": "3.10"}})
        result = calculate(ledger(positions=[position(quantity="5")], materials=[package_material], qa_entries=[q],
                                  mails=[tariffs]), context())
        self.assertTrue(result.complete)
        line = result.answer["positions"][0]
        self.assertEqual(line["quantity"], 5)
        self.assertEqual(line["packaging_per_unit"], Decimal("1.24"))
        self.assertEqual(line["unit_price"], Decimal("11.24"))
        self.assertEqual(result.answer["total_net"], Decimal("56.20"))

    def test_shared_setup_largest_remainder_ties_go_to_ascending_position(self):
        positions = [position(pid) for pid in ("P-A", "P-B", "P-C")]
        members = [qa(f"pool-{pid}", "setup_pool_membership", pid=pid, payload={"key": "POOL-X"})
                   for pid in ("P-A", "P-B", "P-C")]
        pool_mail = mail("pool-fee", "ruestpools", {"pools": {"POOL-X": {
            "fee": "0.02", "allocation": "largest_remainder", "tie_break": "position_asc"}}})
        pooled_material = material(allocation_mass="1")
        result = calculate(ledger(positions=positions, materials=[pooled_material], qa_entries=members,
                                  mails=[pool_mail]), context())
        self.assertTrue(result.complete)
        lines = answer_lines(result)
        self.assertEqual(lines["P-A"]["unit_price"], Decimal("10.01"))
        self.assertEqual(lines["P-B"]["unit_price"], Decimal("10.01"))
        self.assertEqual(lines["P-C"]["unit_price"], Decimal("10.00"))
        self.assertEqual(lines["P-A"]["surcharge_per_unit"], Decimal("0.01"))
        self.assertEqual(lines["P-C"]["surcharge_per_unit"], Decimal("0.00"))
        self.assertEqual(result.answer["total_net"], Decimal("30.02"))

    def test_marginal_bonus_rounds_then_allocates_cent_remainders_by_position(self):
        positions = [position(pid) for pid in ("P-A", "P-B", "P-C")]
        bonus = mail("bonus-bands", "projektbonus", {"positions": ["P-A", "P-B", "P-C"], "bands": [
            {"up_to": "0.10", "percent": "5"}, {"up_to": "0.20", "percent": "5"},
            {"up_to": None, "percent": "10"}]})
        result = calculate(ledger(positions=positions, prices=[condition(price="0.10")], mails=[bonus]), context())
        self.assertTrue(result.complete)
        lines = answer_lines(result)
        self.assertEqual(lines["P-A"]["rebate_total"], Decimal("0.01"))
        self.assertEqual(lines["P-B"]["rebate_total"], Decimal("0.01"))
        self.assertEqual(lines["P-C"]["rebate_total"], Decimal("0.00"))
        self.assertEqual(lines["P-A"]["unit_price"], Decimal("0.09"))
        self.assertEqual(lines["P-B"]["unit_price"], Decimal("0.09"))
        self.assertEqual(lines["P-C"]["unit_price"], Decimal("0.10"))
        self.assertEqual(result.answer["total_net"], Decimal("0.28"))


if __name__ == "__main__":
    unittest.main()
