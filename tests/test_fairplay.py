from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fairplay_check.py"
SPEC = importlib.util.spec_from_file_location("fairplay_check", SCRIPT)
fairplay_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fairplay_check)


class FairplayScanner(unittest.TestCase):
    def findings_for(self, source: str):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sample.py").write_text(source, encoding="utf-8")
            return fairplay_check.scan(root)

    def test_literal_website_hostname_is_rejected(self):
        findings = self.findings_for('SITE_HOST = "vendor.example.invalid"')
        self.assertEqual([reason for _, _, reason in findings], ["hostname-like value"])

    def test_secret_literal_is_rejected_without_echoing_it(self):
        findings = self.findings_for('credential = "sk-abcdefghijklmnopqrstuvwxyz123456"')
        self.assertEqual([reason for _, _, reason in findings], ["secret-like literal"])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", str(findings))

    def test_reference_literal_is_rejected_without_echoing_it(self):
        findings = self.findings_for('reference = "RFQ-2024-1234"')
        self.assertEqual([reason for _, _, reason in findings], ["reference-like value"])

    def test_python_attribute_and_call_expressions_are_not_hostnames(self):
        source = "domain = context.get(\"domain\")\ndomain = info.bidder_domain\n"
        self.assertEqual(self.findings_for(source), [])


if __name__ == "__main__":
    unittest.main()
