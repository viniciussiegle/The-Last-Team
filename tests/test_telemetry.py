import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "my-agent"))
from rfq.telemetry.events import EventBus


class SecretRedaction(unittest.TestCase):
    def test_registered_credentials_are_scrubbed_inside_observation_and_error_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            bus = EventBus(Path(temporary), "synthetic")
            bus.add_secret("synthetic-sensitive-value")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                event = bus.emit("observation", "test", {
                    "text": "Control value=synthetic-sensitive-value",
                    "nested": ["error with synthetic-sensitive-value"],
                    "tokens_in": 12,
                })
            persisted = bus.events_path.read_text(encoding="utf-8")
        self.assertNotIn("synthetic-sensitive-value", persisted + output.getvalue())
        self.assertIn("[REDACTED]", persisted)
        self.assertEqual(event["data"]["tokens_in"], 12)
