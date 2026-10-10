"""Regression checks using synthetic data only."""

import asyncio
import base64
import unittest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from browser_tools import BrowserTools, _document_text, calculate
from rfq_agent import brief_without_credentials, confirmed_receipt, submission_site


class Arithmetic(unittest.TestCase):
    def test_half_up_and_quantity_arithmetic(self):
        self.assertEqual(calculate("round2('4.005')"), "4.01")
        self.assertEqual(calculate("ceil(25/12)"), "3")
        self.assertEqual(calculate("sum(['1.10','2.20'])"), "3.30")

    def test_code_is_not_executable(self):
        with self.assertRaises(ValueError):
            calculate("__import__('os').getcwd()")

    def test_credentials_are_not_sent_in_brief(self):
        brief = "Task\n## Site access\nSecret credentials\n## Submission\nUse portal"
        self.assertNotIn("Secret credentials", brief_without_credentials(brief))
        self.assertIn("Use portal", brief_without_credentials(brief))

    def test_receipt_requires_observed_words_but_allows_paragraph_punctuation(self):
        page = "Submission received\n\nSigned bid accepted. Receipt SYN-example"
        self.assertTrue(confirmed_receipt(page, "Submission received — Signed bid accepted. Receipt SYN-example"))
        self.assertFalse(confirmed_receipt(page, "Receipt SYN-invented"))
        self.assertFalse(confirmed_receipt("Draft saved", "Draft saved"))


class DocumentFetching(unittest.IsolatedAsyncioTestCase):
    async def test_changed_run_has_fresh_memory_and_arbitrary_site_names(self):
        for _ in range(3):
            site = uuid4().hex
            origin = f"https://{uuid4().hex}.example.test"
            root, document = origin + "/workspace/", origin + f"/files/{uuid4().hex}.txt"
            task = {"access": [{"siteId": site, "url": root}],
                    "submission": {"kind": "site", "url": root + "receipt"}}
            page = MagicMock(url=root, frames=[])
            first = BrowserTools(task, {site: page})
            first.observed_urls[site].add(document)
            first.documents[document] = "Evidence from an earlier run"
            second = BrowserTools(task, {site: page})
            self.assertEqual(submission_site(task), site)
            self.assertEqual(second.documents, {})
            with self.assertRaises(ValueError):
                await second.read_document(site, document)
        with self.assertRaises(ValueError):
            submission_site({**task, "submission": {"kind": "site"}})

    async def test_navigation_uses_observed_paths_with_model_chosen_queries(self):
        page = MagicMock(url="https://example.test/")
        page.goto = AsyncMock()
        helper = BrowserTools({"access": [{"siteId": "fixture", "url": page.url}]}, {"fixture": page})
        helper.inspect = AsyncMock(return_value={"controls": []})
        with self.assertRaises(ValueError):
            await helper.navigate("fixture", "/invented-document")
        page.goto.assert_not_awaited()
        helper.observed_urls["fixture"].add("https://example.test/search?q=old")
        await helper.navigate("fixture", "/search?q=new")
        page.goto.assert_awaited_once_with("https://example.test/search?q=new", wait_until="domcontentloaded", timeout=20000)

    async def test_selected_form_control_actions(self):
        page = MagicMock(url="https://example.test/", frames=[])
        helper = BrowserTools({"access": [{"siteId": "fixture", "url": page.url}]}, {"fixture": page})
        checkbox, select = AsyncMock(), AsyncMock()
        helper.controls["fixture"] = [(checkbox, {"id": 0}), (select, {"id": 1})]
        helper.inspect = AsyncMock(return_value={"controls": []})
        await helper.fill_many("fixture", [{"element": 0, "check": True}, {"element": 1, "select": "Example option"}])
        checkbox.set_checked.assert_awaited_once_with(True, timeout=10000)
        select.select_option.assert_awaited_once_with(label="Example option", timeout=10000)
        with self.assertRaises(ValueError):
            await helper.fill_many("fixture", [{"element": 0}])

    async def test_fetches_in_browser_and_does_not_cache_login_html(self):
        page = MagicMock()
        page.url = "https://example.test/"
        page.frames = []
        address = "https://example.test/document"
        def response(content, kind):
            return {"status": 200, "url": address, "type": kind,
                    "data": base64.b64encode(content).decode()}
        page.evaluate = AsyncMock(side_effect=[
            response(b"<html><body>Sign in again</body></html>", "text/html"),
            response(b"Current document", "text/plain"),
        ])
        helper = BrowserTools({"access": [{"siteId": "fixture", "url": page.url}]}, {"fixture": page})
        helper.observed_urls["fixture"].add(address)
        first = await helper.read_document("fixture", address)
        self.assertIn("Sign in again", first["text"])
        self.assertNotIn(address, helper.documents)
        second = await helper.read_document("fixture", address)
        self.assertEqual(second["text"], "Current document")
        self.assertEqual(page.evaluate.await_count, 2)
        await helper.read_document("fixture", address)
        self.assertEqual(page.evaluate.await_count, 2)

    async def test_unobserved_document_is_rejected(self):
        page = MagicMock(url="https://example.test/", frames=[])
        helper = BrowserTools({"access": [{"siteId": "fixture", "url": page.url}]}, {"fixture": page})
        with self.assertRaises(ValueError):
            await helper.read_document("fixture", "https://example.test/unseen")


class Deadlines(unittest.IsolatedAsyncioTestCase):
    async def test_a_blocked_browser_call_cannot_stall_a_worker(self):
        import json as json_
        import time as time_
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        import rfq_agent

        call = SimpleNamespace(id="c1", function=SimpleNamespace(name="inspect_page",
                                                                  arguments=json_.dumps({"site": "s"})))
        message = MagicMock(content="", tool_calls=[call])
        message.model_dump = lambda **_: {}
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=message)]))
        helper = MagicMock(pages={"s": object()})

        async def blocked(*_args, **_kwargs):
            await asyncio.sleep(3600)
        helper.inspect = blocked
        old_floor = rfq_agent.TOOL_TIMEOUT_FLOOR
        rfq_agent.TOOL_TIMEOUT_FLOOR = 0.5
        try:
            with TemporaryDirectory() as folder:
                shared = rfq_agent.Shared({"brief": "", "access": []}, Path(folder) / "trace.jsonl")
                started = time_.monotonic()
                result = await rfq_agent.worker("t", client, shared, helper, rfq_agent.INVESTIGATION_TOOLS, "s", "o",
                                                time_.monotonic() + 2, "done")
        finally:
            rfq_agent.TOOL_TIMEOUT_FLOOR = old_floor
        self.assertEqual(result, {"summary": "deadline reached"})
        self.assertLess(time_.monotonic() - started, 30)
