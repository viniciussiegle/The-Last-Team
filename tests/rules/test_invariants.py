from __future__ import annotations

import random
import sys
import unittest
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "my-agent"
sys.path.insert(0, str(PACKAGE))

from rfq.rules.engine import calculate  # noqa: E402
from tests.rules.test_engine import (  # noqa: E402
    REFERENCE,
    SCENARIO,
    answer_lines,
    condition,
    context,
    ledger,
    mail,
    material,
    position,
    requirement,
)


CENT = Decimal("0.01")


def independently_rounded(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def synthetic_case(seed: int, *, rename: str = "", reference: str = REFERENCE):
    rng = random.Random(seed)
    quantities = {"P-A": rng.randint(1, 19), "P-B": rng.randint(1, 19)}
    cents = {"P-A": rng.randint(325, 12_000), "P-B": rng.randint(325, 12_000)}
    discount = rng.choice((5, 7, 10, 12, 15))
    mapped = {pid: f"{rename}{pid}" for pid in quantities}
    req_ids = {"P-A": f"{rename}REQ-A", "P-B": f"{rename}REQ-B"}
    mat_ids = {"P-A": f"{rename}MAT-A", "P-B": f"{rename}MAT-B"}
    bores = {"P-A": "20", "P-B": "30"}

    positions = [position(mapped[pid], str(quantities[pid]), requirement_ref=req_ids[pid])
                 for pid in quantities]
    requirements = [requirement(req_ids[pid], bore=bores[pid]) for pid in quantities]
    demand = sum(quantities.values())
    materials = [material(mat_ids[pid], bore=bores[pid], stock_qty=str(demand),
                          lead_weeks_stock=2, lead_weeks_production=6) for pid in quantities]
    prices = [condition(mat_ids[pid], price=f"{Decimal(cents[pid]) / 100:.2f}") for pid in quantities]

    messages = [
        mail("approval-earlier", "discount_approval", {"percent": "5"},
             date="2031-04-10T09:00:00+00:00", reference=reference),
        mail("approval-current", "discount_approval", {"percent": str(discount)},
             date=SCENARIO, reference=reference),
        mail("approval-wrong-reference", "discount_approval", {"percent": "80"},
             date="2031-04-14T09:00:00+00:00", reference=reference + "-OTHER"),
        mail("approval-future", "discount_approval", {"percent": "90"},
             date="2031-04-15T12:00:01+00:00", reference=reference),
        mail("approval-unauthorized", "discount_approval", {"percent": "70"},
             date="2031-04-14T10:00:00+00:00", reference=reference, local="outsider"),
    ]
    test_ledger = ledger(positions=positions, requirements=requirements, materials=materials,
                         prices=prices, mails=messages)
    test_context = context(reference=reference)
    expected_by_position = {
        mapped[pid]: {
            "position_id": mapped[pid],
            "material_number": mat_ids[pid],
            "quantity": quantities[pid],
            "base_unit_price": Decimal(cents[pid]) / 100,
            "unit_price": independently_rounded((Decimal(cents[pid]) / 100)
                                                 * (Decimal(100 - discount) / 100)),
        }
        for pid in quantities
    }
    return test_ledger, test_context, positions, materials, prices, messages, discount, expected_by_position


def assert_independent_calculation(test: unittest.TestCase, result, expected: dict[str, dict]) -> None:
    test.assertTrue(result.complete, result.gaps)
    lines = answer_lines(result)
    test.assertEqual(set(lines), set(expected))
    expected_total = Decimal("0.00")
    for pid, expected_line in expected.items():
        line = lines[pid]
        test.assertEqual(line["material_number"], expected_line["material_number"])
        test.assertEqual(line["quantity"], expected_line["quantity"])
        test.assertEqual(line["base_unit_price"], expected_line["base_unit_price"])
        test.assertEqual(line["unit_price"], expected_line["unit_price"])
        expected_total += expected_line["unit_price"] * expected_line["quantity"]
    test.assertEqual(result.answer["total_net"], independently_rounded(expected_total))


class RuleMetamorphicInvariants(unittest.TestCase):
    """Deterministic development regressions; these seeds are not holdout seeds."""

    def test_position_material_price_and_mail_order_do_not_change_results(self):
        discounts_seen = set()
        for seed in range(12):
            with self.subTest(seed=seed):
                data = synthetic_case(seed)
                test_ledger, test_context, positions, materials, prices, messages, discount, expected = data
                discounts_seen.add(discount)
                original = calculate(test_ledger, test_context, core_only=True)
                assert_independent_calculation(self, original, expected)
                self.assertEqual(original.answer["discount_percent"], Decimal(discount))

                reordered = ledger(
                    positions=list(reversed(positions)),
                    requirements=test_ledger.of("SpecRevision")[0].requirements,
                    materials=list(reversed(materials)),
                    prices=list(reversed(prices)),
                    mails=list(reversed(messages)),
                )
                permuted = calculate(reordered, test_context, core_only=True)
                assert_independent_calculation(self, permuted, expected)
                self.assertEqual(answer_lines(permuted), answer_lines(original))
                self.assertEqual({key: value for key, value in permuted.answer.items() if key != "positions"},
                                 {key: value for key, value in original.answer.items() if key != "positions"})
        self.assertGreater(len(discounts_seen), 1)

    def test_consistently_renamed_identifiers_preserve_economics(self):
        for seed in range(12):
            with self.subTest(seed=seed):
                original_data = synthetic_case(seed)
                renamed_ref = f"ALT-{seed:03d}"
                renamed_data = synthetic_case(seed, rename="RENAMED-", reference=renamed_ref)
                original = calculate(original_data[0], original_data[1], core_only=True)
                renamed = calculate(renamed_data[0], renamed_data[1], core_only=True)
                assert_independent_calculation(self, original, original_data[-1])
                assert_independent_calculation(self, renamed, renamed_data[-1])
                self.assertEqual(renamed.answer["total_net"], original.answer["total_net"])
                self.assertEqual(renamed.answer["discount_percent"], original.answer["discount_percent"])
                for old_pid, new_pid in (("P-A", "RENAMED-P-A"), ("P-B", "RENAMED-P-B")):
                    old_line = answer_lines(original)[old_pid]
                    new_line = answer_lines(renamed)[new_pid]
                    self.assertEqual(new_line["quantity"], old_line["quantity"])
                    self.assertEqual(new_line["base_unit_price"], old_line["base_unit_price"])
                    self.assertEqual(new_line["unit_price"], old_line["unit_price"])
                    self.assertEqual(new_line["material_number"], "RENAMED-" + old_line["material_number"])

    def test_unrelated_future_and_unauthorized_approvals_never_override_valid_latest(self):
        for seed in range(12):
            with self.subTest(seed=seed):
                test_ledger, test_context, *_rest = synthetic_case(seed)
                expected_discount = _rest[-2]
                result = calculate(test_ledger, test_context, core_only=True)
                self.assertTrue(result.complete, result.gaps)
                self.assertEqual(result.answer["discount_percent"], Decimal(expected_discount))
                self.assertEqual(len(result.answer["positions"]), 2)

    def test_equivalent_eur_per_piece_and_fx_converted_usd_per_hundred_prices(self):
        quantity = Decimal("7")
        position_rows = [position("P-FX", str(quantity))]
        material_rows = [material("MAT-FX", stock_qty="7")]
        reference = "FX-SYNTHETIC"
        fixing = mail("fixing-synthetic", "devisenfixing", {"rates": {"USD-EUR": "0.5"}},
                      reference=reference)
        euro = condition("MAT-FX", price="12.34", currency="EUR", unit_qty="1")
        dollar_hundred = condition("MAT-FX", price="2468", currency="USD", unit_qty="100",
                                   fx_key="USD-EUR")
        euro_result = calculate(ledger(positions=position_rows, materials=material_rows,
                                       prices=[euro]), context(reference=reference), core_only=True)
        fx_result = calculate(ledger(positions=position_rows, materials=material_rows,
                                     prices=[dollar_hundred], mails=[fixing]),
                              context(reference=reference), core_only=True)

        self.assertTrue(euro_result.complete, euro_result.gaps)
        self.assertTrue(fx_result.complete, fx_result.gaps)
        expected_unit = independently_rounded(Decimal("2468") / Decimal(100) * Decimal("0.5"))
        expected_total = independently_rounded(expected_unit * quantity)
        self.assertEqual(expected_unit, Decimal("12.34"))
        for result in (euro_result, fx_result):
            line = answer_lines(result)["P-FX"]
            self.assertEqual(line["base_unit_price"], expected_unit)
            self.assertEqual(line["unit_price"], expected_unit)
            self.assertEqual(line["quantity"], 7)
            self.assertEqual(result.answer["total_net"], expected_total)
        self.assertEqual(euro_result.answer["total_net"], fx_result.answer["total_net"])

    def test_shared_stock_choice_uses_pooled_demand_independent_of_position_order(self):
        for seed in range(12):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                quantities = [rng.randint(1, 19), rng.randint(1, 19)]
                base = Decimal(rng.randint(325, 12_000)) / 100
                positions = [position("P-A", str(quantities[0])), position("P-B", str(quantities[1]))]
                shared = material("MAT-SHARED", stock_qty=str(sum(quantities) - 1),
                                  lead_weeks_stock=2, lead_weeks_production=6)
                prices = [condition("MAT-SHARED", price=f"{base:.2f}")]
                test_context = context()
                test_ledger = ledger(positions=positions, materials=[shared], prices=prices)
                original = calculate(test_ledger, test_context, core_only=True)
                reversed_positions = ledger(
                    positions=list(reversed(positions)),
                    materials=[shared],
                    prices=prices,
                )
                reordered = calculate(reversed_positions, test_context, core_only=True)
                expected_unit = independently_rounded(base)
                expected_total = independently_rounded(expected_unit * sum(quantities))
                for result in (original, reordered):
                    self.assertTrue(result.complete, result.gaps)
                    self.assertEqual(result.answer["total_net"], expected_total)
                    self.assertEqual(set(answer_lines(result)), {"P-A", "P-B"})
                    for pid, quantity in zip(("P-A", "P-B"), quantities):
                        self.assertEqual(answer_lines(result)[pid]["quantity"], quantity)
                        self.assertEqual(answer_lines(result)[pid]["material_number"], "MAT-SHARED")
                        self.assertEqual(answer_lines(result)[pid]["unit_price"], expected_unit)
                self.assertEqual({row["lead_time_weeks"] for row in original.answer["positions"]}, {6})
                self.assertEqual({row["lead_time_weeks"] for row in reordered.answer["positions"]}, {6})
                self.assertEqual(answer_lines(reordered), answer_lines(original))


if __name__ == "__main__":
    unittest.main()
