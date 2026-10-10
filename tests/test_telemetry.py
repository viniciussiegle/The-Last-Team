from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[1] / "my-agent"
sys.path.insert(0, str(PACKAGE))

from rfq.evidence.ledger import Ledger  # noqa: E402
from rfq.telemetry.events import EventBus  # noqa: E402


class SecretRedaction(unittest.TestCase):
    def test_registered_credentials_are_scrubbed_inside_observation_and_error_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            bus = EventBus(Path(temporary), "synthetic")
            bus.add_secret("synthetic-sensitive-value")
            output = StringIO()
            with redirect_stdout(output):
                event = bus.emit("observation", "test", {
                    "text": "Control value=synthetic-sensitive-value",
                    "nested": ["error with synthetic-sensitive-value"],
                    "tokens_in": 12,
                })
            persisted = bus.events_path.read_text(encoding="utf-8")
        self.assertNotIn("synthetic-sensitive-value", persisted + output.getvalue())
        self.assertIn("[REDACTED]", persisted)
        self.assertEqual(event["data"]["tokens_in"], 12)


class InvalidSigningCodeTelemetry(unittest.TestCase):
    def test_typed_invalid_tan_value_is_redacted_from_event_stdout_and_file(self):
        with tempfile.TemporaryDirectory() as directory:
            bus = EventBus(Path(directory), "synthetic-run")
            ledger = Ledger(bus)
            ledger.allow_tan = True
            typed_value = 918273645
            session = SimpleNamespace(
                site={"siteId": "synthetic-mail"},
                page=SimpleNamespace(url="http://127.0.0.1/synthetic-message"),
                last_snapshot=None,
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                result = ledger.add(
                    "TAN",
                    {"value": typed_value},
                    session,
                    "synthetic-agent",
                    "synthetic message",
                )

            serialized = stdout.getvalue() + bus.events_path.read_text(encoding="utf-8")
            events = [json.loads(line) for line in bus.events_path.read_text(encoding="utf-8").splitlines()]

        self.assertIn("Validation failed", result)
        self.assertNotIn(str(typed_value), serialized)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "record_rejected")
        self.assertIn("[REDACTED]", serialized)


if __name__ == "__main__":
    unittest.main()
