from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1] / "my-agent"
sys.path.insert(0, str(PACKAGE))

from rfq.config import Settings  # noqa: E402
from rfq.submit.watchdog import deadlines  # noqa: E402


class WatchdogDeadlines(unittest.TestCase):
    def test_default_budget_is_clamped_by_configured_run_budget(self):
        settings = Settings.from_env({})
        with patch("rfq.submit.watchdog.time.monotonic", return_value=100.0), \
                patch("rfq.submit.watchdog.seconds_left", return_value=3000.0):
            freeze, end = deadlines({}, settings)

        self.assertEqual(settings.run_budget_s, 1140)
        self.assertEqual(settings.submit_margin_s, 420)
        self.assertEqual((freeze, end), (820.0, 1240.0))

    def test_a_long_test_run_and_a_scored_run_get_the_same_deadlines_inside_twenty_minutes(self):
        settings = Settings.from_env({})
        outcomes = []
        for platform_seconds_left in (7200.0, 1200.0):
            with patch("rfq.submit.watchdog.time.monotonic", return_value=100.0), \
                    patch("rfq.submit.watchdog.seconds_left", return_value=platform_seconds_left):
                outcomes.append(deadlines({}, settings))

        self.assertEqual(outcomes[0], outcomes[1])
        freeze, end = outcomes[1]
        self.assertLessEqual(end - 100.0, 1200.0)
        self.assertGreater(freeze, 100.0)

    def test_short_platform_deadline_never_freezes_before_start(self):
        settings = Settings.from_env({})
        with patch("rfq.submit.watchdog.time.monotonic", return_value=1000.0), \
                patch("rfq.submit.watchdog.seconds_left", return_value=120.0):
            freeze, end = deadlines({}, settings)

        self.assertEqual(end, 1110.0)
        self.assertEqual(freeze, 1000.0)
        self.assertGreaterEqual(freeze, 1000.0)

    def test_budget_and_margin_environment_overrides_use_external_data_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            external = Path(directory) / "synthetic-run-data"
            settings = Settings.from_env({
                "RFQ_DATA_DIR": str(external),
                "RUN_BUDGET_S": "900",
                "SUBMIT_MARGIN_S": "120",
            })

        self.assertEqual(settings.data_dir, external.resolve())
        self.assertEqual(settings.run_budget_s, 900)
        self.assertEqual(settings.submit_margin_s, 120)


if __name__ == "__main__":
    unittest.main()
