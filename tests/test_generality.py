from __future__ import annotations

import argparse
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "mock_sites"))
sys.path.insert(0, str(ROOT))

from generality import _page, generate_case  # noqa: E402
from scripts.generality import (_navigation_events, freeze_state, hashes_unchanged, report_result, run,
                                validate_output_dir)  # noqa: E402


class GeneralityInfrastructure(unittest.TestCase):
    def test_output_path_must_be_external_to_checkout(self):
        with self.assertRaisesRegex(ValueError, "outside the checkout"):
            validate_output_dir(ROOT / "runtime" / "holdout", ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(validate_output_dir(Path(temporary), ROOT), Path(temporary).resolve())

    def test_navigation_gate_counts_url_transitions_not_click_attempts(self):
        events = [
            {"type": "observation", "data": {"page_url": "http://127.0.0.1/start"}},
            {"type": "agent_step", "data": {"action": {"kind": "click"},
                                               "page_url": "http://127.0.0.1/catalog"}},
            {"type": "observation", "data": {"page_url": "http://127.0.0.1/catalog"}},
            {"type": "agent_step", "data": {"action": {"kind": "click"},
                                               "page_url": "http://127.0.0.1/catalog"}},
            {"type": "observation", "data": {"page_url": "http://127.0.0.1/catalog"}},
            {"type": "agent_step", "data": {"action": {"kind": "goto"},
                                               "page_url": "http://127.0.0.1/tender"}},
        ]

        transitions = _navigation_events(events)

        self.assertEqual(len(transitions), 2)
        self.assertEqual([row["to"] for row in transitions],
                         ["http://127.0.0.1/catalog", "http://127.0.0.1/tender"])

    def test_generated_layouts_routes_and_target_facts_vary(self):
        cases = [generate_case(random.Random(index + 31), index, layout, language)
                 for index, (layout, language) in enumerate(
                     zip(("table", "cards", "nested_frame"), ("en", "de", "en")), 1)]

        self.assertEqual({case["layout"] for case in cases}, {"table", "cards", "nested_frame"})
        self.assertEqual({case["language"] for case in cases}, {"en", "de"})
        self.assertEqual(len({case["routes"]["catalog"] for case in cases}), len(cases))
        self.assertGreaterEqual(len({case["labels"]["target"] for case in cases}), 2)
        for case in cases:
            self.assertGreaterEqual(len(case["positions"]), 2)
            self.assertLessEqual(len(case["positions"]), 3)
            self.assertNotEqual(case["reference"], case["decoy_reference"])
            self.assertEqual(len(set(case["routes"].values())), len(case["routes"]))
            self.assertEqual({row["position_id"] for row in case["positions"]},
                             {row["position_id"] for row in case["displayed_positions"]})
            self.assertNotEqual(case["decoy_position"]["position_id"],
                                case["positions"][0]["position_id"])
            landing_status, landing = _page(case, case["routes"]["landing"])
            catalog_status, catalog = _page(case, case["routes"]["catalog"])
            target_status, target = _page(case, case["routes"]["target"])
            self.assertEqual((landing_status, catalog_status, target_status), (200, 200, 200))
            self.assertIn(case["routes"]["catalog"], landing)
            self.assertIn(case["routes"]["target"], catalog)
            self.assertIn(case["reference"], catalog)
            self.assertIn(case["decoy_reference"], catalog)
            if case["layout"] == "nested_frame":
                self.assertIn(case["routes"]["frame_outer"], target)
                _, middle = _page(case, case["routes"]["frame_outer"])
                self.assertIn(case["routes"]["frame"], middle)
                _, detail = _page(case, case["routes"]["frame"])
            else:
                detail = target
            for row in case["positions"]:
                self.assertIn(row["position_id"], detail)
            self.assertNotIn(case["decoy_position"]["position_id"], detail)

    def test_package_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "my-agent"
            prompt = package / "rfq" / "llm" / "prompts" / "browser.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("synthetic prompt", encoding="utf-8")
            before = freeze_state(package, "synthetic-model", {"headless": True})
            prompt.write_text("tampered synthetic prompt", encoding="utf-8")
            after = freeze_state(package, "synthetic-model", {"headless": True})

            same, changed, changed_harness = hashes_unchanged(before, after)

        self.assertFalse(same)
        self.assertEqual(changed, ["rfq/llm/prompts/browser.md"])
        self.assertEqual(changed_harness, [])

    def test_evaluation_harness_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "my-agent"
            package.mkdir()
            script = root / "generality.py"
            fixture = root / "fixture.py"
            script.write_text("before", encoding="utf-8")
            fixture.write_text("fixture", encoding="utf-8")
            harness = {"scripts/generality.py": script, "tests/mock_sites/generality.py": fixture}
            before = freeze_state(package, "synthetic-model", {}, harness)
            script.write_text("tampered", encoding="utf-8")
            after = freeze_state(package, "synthetic-model", {}, harness)

            same, changed_package, changed_harness = hashes_unchanged(before, after)

        self.assertFalse(same)
        self.assertEqual(changed_package, [])
        self.assertEqual(changed_harness, ["scripts/generality.py"])

    def test_preparation_report_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "my-agent"
            package.mkdir()
            frozen = freeze_state(package, "synthetic-model", {"headless": True})
            report = report_result("prepared_only", [{"case": 1, "passed": False}], frozen, frozen, 0)

        self.assertFalse(report["passed"])
        self.assertEqual(report["status"], "prepared_only")

        partial = report_result("evaluated_partial", [{"case": 1, "layout": "table", "passed": True}],
                                frozen, frozen, 0)
        self.assertFalse(partial["passed"])

        missing_variants = report_result(
            "evaluated", [{"case": i, "layout": "table", "passed": True} for i in range(1, 4)],
            frozen, frozen, 0,
        )
        self.assertFalse(missing_variants["passed"])

    def test_each_invocation_gets_a_new_external_output_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "holdouts"
            package = Path(temporary) / "synthetic-package"
            package.mkdir()
            (package / "starter.py").write_text("stable package", encoding="utf-8")
            args = argparse.Namespace(out=base, cases=1, prepare_only=True)
            with patch("scripts.generality.PACKAGE", package), patch("scripts.generality._fairplay"):
                first = run(args)
                second = run(args)
            first_dir, second_dir = Path(first["output_dir"]), Path(second["output_dir"])
            self.assertNotEqual(first_dir, second_dir)
            self.assertTrue(first_dir.is_relative_to(base.resolve()))
            self.assertTrue(second_dir.is_relative_to(base.resolve()))
            self.assertTrue((first_dir / "report.json").exists())
            self.assertTrue((second_dir / "report.json").exists())
            self.assertNotEqual(first["run_id"], second["run_id"])


if __name__ == "__main__":
    unittest.main()
