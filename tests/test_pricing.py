"""The bid calculation on generated synthetic evidence (no public scenario data)."""

import math
import unittest
from decimal import Decimal, ROUND_HALF_UP

from pricing import Evidence, allocate, calculate_bid, number

R = lambda value: Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def scenario(threshold="1000.00"):
    e = Evidence()
    add = lambda kind, **data: problems.extend(e.add(kind, data))
    problems = []
    ref, mail = "TX-3001-55", dict(reference="TX-3001-55", sender="ops@vendor.test", authorized=True)
    add("tender", reference=ref, scenario_date="2030-03-10T08:00:00Z")
    add("position", position="2.1", tender_quantity="10", unit="pcs", requirement_ref="Q-1")
    add("position", position="2.2", tender_quantity="4", unit="Set", requirement_ref="Q-2")
    add("position", position="2.3", tender_quantity="7", unit="pcs", requirement_ref="Q-1")
    # Quantity: 2.1 -> 12 (later 15 is withdrawn); 2.2 stays 4 sets x 3 pieces.
    add("qa_quantity", answer="11", date="2030-03-01T09:00:00Z", position="2.1", new_quantity="12")
    add("qa_quantity", answer="12", date="2030-03-02T09:00:00Z", position="2.1", new_quantity="15")
    add("qa_withdrawal", answer="13", date="2030-03-03T09:00:00Z", withdrawn_answer="12")
    add("qa_quantity", answer="14", date="2030-03-20T09:00:00Z", position="2.3", new_quantity="99")  # future
    # Spec: superseded revision is ignored; amendment to the current revision; usable stroke + reserves.
    add("spec_requirement", revision="V1", revision_status="superseded", requirement_ref="Q-1", bore="40",
        stroke="100", pressure="160", mounting="Clevis", certification="", catalog_group="CG-1")
    add("spec_requirement", revision="V2", revision_status="current", requirement_ref="Q-1", bore="40",
        stroke="200", pressure="160", mounting="Clevis", certification="none", catalog_group="CG-1",
        usable_stroke=True, end_reserve_key="RES-1")
    add("spec_requirement", revision="V2", revision_status="current", requirement_ref="Q-2", bore="63",
        stroke="300", pressure="250", mounting="Flange", certification="CERT-9", pieces_per_set="3")
    add("qa_amendment", answer="15", date="2030-02-01", revision="V2", requirement_ref="Q-1",
        attribute="stroke", value="210")
    add("qa_amendment", answer="16", date="2030-02-01", revision="V1", requirement_ref="Q-1",
        attribute="stroke", value="999")
    add("mail", topic="technical_release", date="2030-02-05", revision="V2",
        reserves=[{"key": "RES-1", "mm_per_end": "5"}], **mail)
    # Materials (an inactive look-alike must be ignored).
    add("material", material_number="M-A", active=True, bore="40", stroke="220", pressure="160",
        mounting="clevis", certification="", available_stock="18", stock_lead_weeks="2",
        production_lead_weeks="9", pieces_per_carton="4", surcharge_mass_g="1500", allocation_mass_g="500",
        metal_index_key="IDX-1")
    add("material", material_number="M-OLD", active=False, bore="40", stroke="220", pressure="160",
        mounting="Clevis", certification="")
    add("material", material_number="M-B", active=True, bore="63", stroke="300", pressure="250",
        mounting="Flange", certification="CERT-9", available_stock="100", stock_lead_weeks="1",
        production_lead_weeks="6", pieces_per_carton="5", surcharge_mass_g="0", allocation_mass_g="700")
    add("price_condition", material_number="M-A", valid_from="2029-01-01", valid_to="2029-12-31",
        currency="EUR", price_unit="1", number_format="en-US", scales=[{"min_qty": "1", "price": "1.00"}])
    add("price_condition", material_number="M-A", valid_from="2030-01-01", valid_to="2030-12-31",
        currency="USD", fx_key="FXK", price_unit="100", number_format="de-DE",
        scales=[{"min_qty": "1", "price": "12.345,6"}, {"min_qty": "10", "price": "11.000"}])
    add("price_condition", material_number="M-B", valid_from="2030-03-01", valid_to="2030-03-10",
        currency="EUR", price_unit="1", number_format="en-US",
        scales=[{"min_qty": "1", "price": "250.555"}, {"min_qty": "20", "price": "240"}])
    add("qa_keys", answer="20", date="2030-03-01", position="2.1", condition_group="KG-1",
        packaging_key="PK-1", delivery_key="DL-1", setup_pool_key="SP-1", bonus_plan_key="BP-1",
        setup_fee_eur="30.00")
    add("qa_keys", answer="21", date="2030-03-01", position="2.2", packaging_key="PK-1",
        setup_pool_key="SP-1", bonus_plan_key="BP-1")
    add("qa_keys", answer="22", date="2030-03-01", position="2.3", delivery_key="DL-2")
    add("mail", topic="fx_fixing", date="2030-03-05", rates=[{"key": "FXK", "eur_per_unit": "0.9"}], **mail)
    add("mail", topic="discount_approval", date="2030-03-01", percent="9", min_order_value_eur=None, **mail)
    add("mail", topic="discount_approval", date="2030-03-08", percent="4", min_order_value_eur=threshold, **mail)
    add("mail", topic="discount_approval", date="2030-03-09", percent="50", min_order_value_eur=None,
        **{**mail, "reference": "TX-3001-55-B"})
    add("mail", topic="discount_approval", date="2030-03-12", percent="60", min_order_value_eur=None, **mail)
    add("mail", topic="condition_rules", date="2030-03-02", groups={"KG-1": "base_and_surcharge"}, **mail)
    add("mail", topic="metal_index", date="2030-03-02", rates=[
        {"key": "IDX-1", "eur_per_kg": "3.00", "valid_from": "2030-01-01", "valid_to": "2030-02-28"},
        {"key": "IDX-1", "eur_per_kg": "4.20", "valid_from": "2030-03-01", "valid_to": "2030-03-31"},
        {"key": "IDX-1", "eur_per_kg": "5.00", "valid_from": "2030-04-01", "valid_to": "2030-04-30"}], **mail)
    add("mail", topic="packaging_tariff", date="2030-03-02", tariffs=[{"key": "PK-1", "eur_per_carton": "2.50"}], **mail)
    add("mail", topic="delivery_commitment", date="2030-03-02", key="DL-1", material_number="M-A",
        max_quantity="20", lead_time_weeks="4", **mail)
    add("mail", topic="delivery_commitment", date="2030-03-02", key="DL-2", material_number="M-A",
        max_quantity="15", lead_time_weeks="3", **mail)
    add("mail", topic="setup_pool", date="2030-03-02", key="SP-1", fee_eur="100.00",
        allocation_rule="largest_remainder", **mail)
    add("mail", topic="project_bonus", date="2030-03-02", key="BP-1", bands=[
        {"from_eur": "0", "to_eur": "1000", "percent": "0"},
        {"from_eur": "1000", "to_eur": None, "percent": "2"}], **mail)
    return e, problems


class Calculation(unittest.TestCase):
    def test_numbers_and_allocation(self):
        self.assertEqual(number("12.345,6 (de-DE)"), Decimal("12345.6"))
        self.assertEqual(number("186,615 (en-US)"), Decimal("186615"))
        self.assertEqual(number("1,5"), Decimal("1.5"))
        # Explanations appended by a model must not merge into the number.
        self.assertEqual(number("238,098 (en-US = 238098 EUR per 100 ST)"), Decimal("238098"))
        self.assertEqual(number("4.321,5 (de-DE; 4321.5 per unit)"), Decimal("4321.5"))
        self.assertEqual(number("100 ST"), Decimal("100"))
        self.assertEqual(number("EUR 1.234,56", "de-DE"), Decimal("1234.56"))
        # German/US ambiguity without a hint, resolved by the kind of field.
        self.assertEqual(number("1.250", kind="count"), Decimal("1250"))
        self.assertEqual(number("1,250", kind="count"), Decimal("1250"))
        self.assertEqual(number("1.250", kind="amount"), Decimal("1250"))
        self.assertEqual(number("517,25", kind="amount"), Decimal("517.25"))
        self.assertEqual(number("1.084"), Decimal("1.084"))  # rates keep the decimal point
        self.assertEqual(allocate(Decimal("1.00"), [Decimal(1)] * 3, ["b", "a", "c"], "largest_remainder"),
                         [Decimal("0.33"), Decimal("0.34"), Decimal("0.33")])

    def test_rules_end_to_end(self):
        evidence, problems = scenario()
        self.assertEqual(problems, [])
        result = calculate_bid(evidence)
        self.assertEqual(result["gaps"], [])
        answer = result["answer"]
        by = {p["position"]: p for p in answer["positions"]}
        # Quantities: withdrawal and future answers ignored; sets converted.
        self.assertEqual((by["2.1"]["quantity"], by["2.2"]["quantity"], by["2.3"]["quantity"]), (12, 12, 7))
        # Stroke 210 (amendment) + 2 x 5 reserve = 220 -> M-A; the inactive look-alike is ignored.
        self.assertEqual((by["2.1"]["material_number"], by["2.2"]["material_number"]), ("M-A", "M-B"))
        base_a = R(Decimal("11000") / 100 * Decimal("0.9"))  # 99.00
        base_b = R(Decimal("250.555"))
        self.assertEqual(Decimal(str(by["2.1"]["base_unit_price"])), base_a)
        self.assertEqual(Decimal(str(by["2.2"]["base_unit_price"])), base_b)
        # Discount: latest exact-reference approval no later than the scenario date, threshold met.
        undiscounted = R(12 * base_a + 12 * base_b + 7 * R(Decimal("12345.6") / 100 * Decimal("0.9")))
        self.assertGreaterEqual(undiscounted, Decimal("1000"))
        self.assertEqual(answer["discount_percent"], 4.0)
        d = Decimal("0.96")
        mass = Decimal("1.5") * Decimal("4.20")
        weights = [12 * 500, 12 * 700]
        pool = allocate(Decimal("100"), [Decimal(w) for w in weights], ["2.1", "2.2"], "largest_remainder")
        goods = [12 * base_a, 12 * base_b]
        credit = R((sum(goods) - 1000) * Decimal("0.02"))
        bonus = allocate(credit, goods, ["2.1", "2.2"], "largest_remainder")
        pack_a = math.ceil(12 / 4) * Decimal("2.50")
        final_a = R(base_a * d + mass * d + (Decimal(30) + pool[0] + pack_a - bonus[0]) / 12)
        self.assertEqual(Decimal(str(by["2.1"]["unit_price"])), final_a)
        self.assertEqual(Decimal(str(by["2.1"]["surcharge_per_unit"])), R(mass + (30 + pool[0]) / 12))
        self.assertEqual(Decimal(str(by["2.1"]["rebate_total"])), bonus[0])
        pack_b = math.ceil(12 / 5) * Decimal("2.50")
        final_b = R(base_b * d + (pool[1] + pack_b - bonus[1]) / 12)
        self.assertEqual(Decimal(str(by["2.2"]["unit_price"])), final_b)
        self.assertEqual(by["2.3"]["rebate_total"], 0.0)
        # Delivery: pooled M-A demand 19 <= stock 18? no -> 2.1 commitment (<=20) 4 weeks, 2.3 (ceiling 15) production.
        self.assertEqual((by["2.1"]["lead_time_weeks"], by["2.3"]["lead_time_weeks"], by["2.2"]["lead_time_weeks"]),
                         (4, 9, 1))
        total = sum(Decimal(p["quantity"]) * Decimal(str(p["unit_price"])) for p in answer["positions"])
        self.assertEqual(Decimal(str(answer["total_net"])), R(total))

    def test_mail_record_shapes_are_normalized(self):
        e = Evidence()
        base = {"reference": "R-1", "sender": "x@y.test", "date": "2030-01-01", "authorized": True}
        self.assertEqual(e.add("mail", {**base, "topic": "packaging_tariff",
                                        "packaging_tariff": {"key": "P-1", "eur_per_carton": "3.5"}}), [])
        self.assertEqual(e.add("mail", {**base, "topic": "metal_index", "key": "M-1", "eur_per_kg": "2"}), [])
        self.assertEqual(e.add("mail", {**base, "topic": "discount_approval",
                                        "discount_approval": {"percent": 3, "min_order_value_eur": None}}), [])
        rows = e.of("mail")
        self.assertEqual(rows[0]["tariffs"], [{"key": "P-1", "eur_per_carton": "3.5"}])
        self.assertEqual(rows[1]["rates"], [{"key": "M-1", "eur_per_kg": "2"}])
        self.assertEqual(rows[2]["percent"], 3)
        self.assertTrue(e.add("mail", {**base, "topic": "delivery_commitment", "key": "D-1"}))

    def test_malformed_nested_shapes_do_not_crash(self):
        evidence, _ = scenario()
        for record in evidence.records:
            data = record["data"]
            if data.get("topic") == "condition_rules":
                data["groups"] = [{"key": "KG-1", "rule": "base_and_surcharge"}, "junk"]
            if data.get("topic") == "fx_fixing":
                data["rates"] = {"key": "FXK", "eur_per_unit": "0.9"}
        result = calculate_bid(evidence)
        self.assertEqual(result["gaps"], [])

    def test_withdrawal_names_the_answer_differently(self):
        evidence, _ = scenario()
        for record in evidence.records:
            if record["kind"] == "qa_withdrawal":
                record["data"]["withdrawn_answer"] = "Rückfrage Nr. 12"
        by = {p["position"]: p for p in calculate_bid(evidence)["answer"]["positions"]}
        self.assertEqual(by["2.1"]["quantity"], 12)

    def test_failed_threshold_gives_zero_discount_and_missing_evidence_is_a_gap(self):
        evidence, _ = scenario(threshold="999999")
        result = calculate_bid(evidence)
        self.assertEqual(result["answer"]["discount_percent"], 0.0)
        empty = calculate_bid(Evidence(), "TX-1", "2030-01-01T00:00:00Z")
        self.assertEqual(empty["answer"]["positions"], [])
        self.assertIn("tender positions", empty["gaps"])


if __name__ == "__main__":
    unittest.main()
