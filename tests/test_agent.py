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
        checkbox.evaluate.assert_awaited()  # ticked through a click on the element itself
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


def fake_stream(chunks, delay=0.0, hang_first=False):
    """A stand-in for a streamed chat completion: yields chunks, optionally slowly or never."""
    from types import SimpleNamespace

    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if hang_first:
                await asyncio.sleep(3600)
            if not chunks:
                raise StopAsyncIteration
            await asyncio.sleep(delay)
            return chunks.pop(0)

        async def close(self):
            pass

    return Stream()


def tool_chunk(name, arguments, index=0, call_id="c1", finish=None):
    from types import SimpleNamespace
    function = SimpleNamespace(name=name, arguments=arguments)
    delta = SimpleNamespace(content=None, tool_calls=[SimpleNamespace(index=index, id=call_id, function=function)])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def text_chunk(text, finish=None):
    from types import SimpleNamespace
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None),
                                                    finish_reason=finish)])


class ModelCalls(unittest.IsolatedAsyncioTestCase):
    async def test_slow_answer_is_kept_and_silent_request_is_asked_again(self):
        import rfq_agent
        streams = [fake_stream([], hang_first=True),
                   fake_stream([text_chunk("slow "), text_chunk("but "), text_chunk("steady", "stop")], delay=0.3)]
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=lambda **_: streams.pop(0))
        old = rfq_agent.FIRST_TOKEN_TIMEOUT, rfq_agent.PAUSE_TIMEOUT
        rfq_agent.FIRST_TOKEN_TIMEOUT, rfq_agent.PAUSE_TIMEOUT = 0.5, 0.5
        try:
            reply = await rfq_agent.ask(client, [{"role": "user", "content": "hi"}])
        finally:
            rfq_agent.FIRST_TOKEN_TIMEOUT, rfq_agent.PAUSE_TIMEOUT = old
        self.assertEqual(reply.choices[0].message.content, "slow but steady")
        self.assertEqual(client.chat.completions.create.await_count, 2)

    async def test_streamed_tool_call_arguments_are_assembled(self):
        import rfq_agent
        chunks = [tool_chunk("navigate", '{"site": '), tool_chunk(None, '"s1"}'),
                  tool_chunk("calculate", '{"expression": "1+1"}', index=1, call_id="c2", finish="tool_calls")]
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=fake_stream(chunks))
        message = (await rfq_agent.ask(client, [], tools=[])).choices[0].message
        self.assertEqual([(c.function.name, c.function.arguments) for c in message.tool_calls],
                         [("navigate", '{"site": "s1"}'), ("calculate", '{"expression": "1+1"}')])


class RecordCheck(unittest.IsolatedAsyncioTestCase):
    async def test_rewritten_number_is_refused_once_then_accepted_identically(self):
        import json as json_
        import time as time_
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import rfq_agent
        wrong = {"kind": "price_condition", "data": {"material_number": "M-7", "number_format": "en-US",
                 "scales": [{"min_qty": "5", "price": "349.207"}]}}
        replies = [tool_chunk("inspect_page", json_.dumps({"site": "s"}), finish="tool_calls"),
                   tool_chunk("record", json_.dumps({"items": [wrong]}), finish="tool_calls"),
                   tool_chunk("record", json_.dumps({"items": [wrong]}), finish="tool_calls"),
                   tool_chunk("done", json_.dumps({"summary": "ok"}), finish="tool_calls")]
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=lambda **_: fake_stream([replies.pop(0)]))
        helper = MagicMock(pages={"s": object()})
        helper.inspect = AsyncMock(return_value={"url": "u", "text": "Material M-7 | Scale 5 | 349,207 (en-US)", "controls": []})
        with TemporaryDirectory() as folder:
            shared = rfq_agent.Shared({"brief": "", "access": []}, Path(folder) / "trace.jsonl")
            result = await rfq_agent.worker("t", client, shared, helper, rfq_agent.INVESTIGATION_TOOLS, "s", "o",
                                            time_.monotonic() + 60, "done")
        self.assertEqual(result, {"summary": "ok"})
        self.assertEqual(len(shared.evidence.of("price_condition")), 1)  # refused once, then accepted

    async def test_an_invented_value_is_never_accepted(self):
        import json as json_
        import time as time_
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import rfq_agent
        invented = {"kind": "mail", "data": {"topic": "setup_pool", "reference": "TX-5", "sender": "a@b.test",
                    "date": "2031-02-01", "authorized": True, "key": "RP-77", "fee_eur": "4321"}}
        replies = [tool_chunk("inspect_page", json_.dumps({"site": "s"}), finish="tool_calls"),
                   tool_chunk("record", json_.dumps({"items": [invented]}), finish="tool_calls"),
                   tool_chunk("record", json_.dumps({"items": [invented]}), finish="tool_calls"),
                   tool_chunk("done", json_.dumps({"summary": "ok"}), finish="tool_calls")]
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=lambda **_: fake_stream([replies.pop(0)]))
        helper = MagicMock(pages={"s": object()})
        helper.inspect = AsyncMock(return_value={"url": "u", "text": "Inbox TX-5: pool RP-77 fee 1.000,00", "controls": []})
        with TemporaryDirectory() as folder:
            shared = rfq_agent.Shared({"brief": "", "access": []}, Path(folder) / "trace.jsonl")
            await rfq_agent.worker("t", client, shared, helper, rfq_agent.INVESTIGATION_TOOLS, "s", "o",
                                   time_.monotonic() + 60, "done")
        self.assertEqual(shared.evidence.of("mail"), [])  # refused both times


class Planning(unittest.TestCase):
    def test_one_investigator_per_website(self):
        from rfq_agent import one_per_site
        sites = [{"siteId": "a"}, {"siteId": "b"}]
        joined = one_per_site([{"site": "a", "goal": "first"}, {"site": "a", "goal": "second"},
                               {"site": "x", "goal": "unknown site"}], sites)
        self.assertEqual(joined, [{"site": "a", "goal": "first"}, {"site": "a", "goal": "second"}])


class Deadlines(unittest.IsolatedAsyncioTestCase):
    async def test_a_blocked_browser_call_cannot_stall_a_worker(self):
        import json as json_
        import time as time_
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        import rfq_agent

        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            side_effect=lambda **_: fake_stream([tool_chunk("inspect_page", json_.dumps({"site": "s"}), finish="tool_calls")]))
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


class Orchestration(unittest.IsolatedAsyncioTestCase):
    async def test_investigation_then_submission_with_the_calculated_sheet(self):
        import time as time_
        from unittest.mock import patch
        import rfq_agent

        sites = [{"siteId": f"s{i}", "url": f"https://s{i}.example.test/", "credentials": {}} for i in range(3)]
        task = {"brief": "Prepare a bid for (TX-2030-77). The scenario date is 2030-03-10T08:00:00Z.",
                "access": sites, "submission": {"kind": "site", "siteId": "s0"},
                "expires_at": int((time_.time() + 1200) * 1000)}
        seen = {}

        async def fake_plan(client, task_, sites_):
            return [{"site": "s1", "goal": "find positions"}]

        async def fake_worker(name, client, shared, helper, tools, system, opening, deadline, terminal, submit_site=None):
            if terminal == "finish":
                seen["opening"] = opening
                seen["shared_urls"] = helper.observed_urls is shared.observed
                return {"submitted": True, "receipt": "Receipt X-1"}
            shared.evidence.add("position", {"position": "1", "tender_quantity": "2", "unit": "pcs"})
            return {"summary": "ok"}

        async def page():
            return MagicMock(url="about:blank", frames=[])
        contexts = {site["siteId"]: page for site in sites}
        with patch.object(rfq_agent, "plan", fake_plan), patch.object(rfq_agent, "worker", fake_worker), \
                patch.object(rfq_agent.models, "qwen", MagicMock()):
            result = await asyncio.wait_for(rfq_agent.run(task, contexts), 60)
        self.assertEqual(result, {"submitted": True, "receipt": "Receipt X-1"})
        self.assertIn("TX-2030-77", seen["opening"])
        self.assertTrue(seen["shared_urls"])


class ReviewCheck(unittest.TestCase):
    def test_review_rows_are_checked_against_the_sheet(self):
        from rfq_agent import check_entries
        answer = {"total_net": 1471.08, "positions": [
            {"position": "2.1", "material_number": "M-A", "quantity": 12, "unit_price": 106.18, "lead_time_weeks": 4,
             "base_unit_price": 99.0, "surcharge_per_unit": 12.27, "packaging_per_unit": 0.63, "rebate_total": 18.09},
            {"position": "2.2", "material_number": "M-B", "quantity": 1, "unit_price": 196.92, "lead_time_weeks": 1,
             "base_unit_price": 250.56, "rebate_total": 0.0}]}
        page = ("Nettosumme 1.471,08 EUR\n| Position | Katalognummer | Menge | Einzelpreis | Lieferzeit\n"
                "| 2.1 | M-A | 12 | 106,18 | 4 Wochen | 99,00 | 12,27 | 0,63 | 18,09\n"
                "| 2.2 | M-B | 1 | 196,92 | 1 Wochen | 250,56 | | | 0,00")
        self.assertEqual(check_entries(page, answer), {"ok": True, "mismatches": []})
        slipped = page.replace("| 18,09", "| 0,00")
        result = check_entries(slipped, answer)
        self.assertFalse(result["ok"])
        self.assertEqual(result["mismatches"], ["position 2.1: rebate total should be 18,09"])


class PageBlocks(unittest.TestCase):
    def test_the_block_of_the_position_a_page_shows(self):
        from rfq_agent import sheet_block_for_page
        sheet = ("Tender reference: TX-1\n=== POSITION 3.1 (enter only ...) ===\n  quantity (pieces): 4\n"
                 "=== POSITION 3.2 (enter only ...) ===\n  quantity (pieces): 9")
        block = sheet_block_for_page({"page": {"text": "Step 4 · Position 3.2 · Configurator"}}, sheet)
        self.assertIn("quantity (pieces): 9", block)
        self.assertEqual(sheet_block_for_page({"text": "Review: 3.1 and 3.2"}, sheet), "")
