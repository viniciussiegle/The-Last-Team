from __future__ import annotations

import sys
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

PACKAGE = Path(__file__).resolve().parents[1] / "my-agent"
sys.path.insert(0, str(PACKAGE))

from rfq.browser.actions import act  # noqa: E402
from rfq.browser.session import BrowserSession  # noqa: E402
from rfq.browser.snapshot import observe  # noqa: E402
from rfq.browser.guards import login, recover  # noqa: E402


class ObservedActions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch()
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        self.session = BrowserSession(
            self.browser,
            self.context,
            self.page,
            {"url": "https://synthetic.example.test/"},
            {"https://synthetic.example.test"},
        )
        self.session.install_handlers(self.page)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def observed_id(self, selector):
        snapshot = await observe(self.session)
        element = await self.page.locator(selector).get_attribute("data-rfq-observe")
        self.assertIsNotNone(element)
        self.assertIn(int(element), snapshot.elements)
        return int(element)

    async def test_range_type_sets_value_and_dispatches_input_and_change(self):
        await self.page.set_content(
            '<input type="range" min="0" max="20" value="2" aria-label="range">'
            '<output id="events"></output>'
            '<script>const r=document.querySelector("input"),o=document.querySelector("output");'
            'r.addEventListener("input",()=>o.dataset.input=(+(o.dataset.input||0)+1));'
            'r.addEventListener("change",()=>o.dataset.change=(+(o.dataset.change||0)+1));</script>'
        )
        element_id = await self.observed_id('input[type="range"]')

        await act(self.session, {"kind": "type", "id": element_id, "text": "13"})

        self.assertEqual(await self.page.locator('input[type="range"]').input_value(), "13")
        self.assertEqual(await self.page.locator("output").get_attribute("data-input"), "1")
        self.assertEqual(await self.page.locator("output").get_attribute("data-change"), "1")

    async def test_press_id_targets_observed_text_field(self):
        await self.page.set_content(
            '<input aria-label="synthetic field" value="replace this">'
            '<p>page body must not receive the typing</p>'
        )
        element_id = await self.observed_id("input")

        await act(self.session, {"kind": "press", "id": element_id, "key": "Control+A"})
        await self.page.keyboard.type("selected")

        self.assertEqual(await self.page.locator("input").input_value(), "selected")
        self.assertEqual(await self.page.locator("body").inner_text(), "page body must not receive the typing")

    async def test_fill_handles_checkbox_yes_and_text_field_together(self):
        await self.page.set_content(
            '<label><input type="checkbox" aria-label="synthetic choice"> Accept</label>'
            '<input type="text" aria-label="synthetic note">'
        )
        checkbox_id = await self.observed_id('input[type="checkbox"]')
        text_id = await self.observed_id('input[type="text"]')

        await act(self.session, {"kind": "fill", "fields": [
            {"id": checkbox_id, "text": "yes"}, {"id": text_id, "text": "synthetic note"},
        ]})

        self.assertTrue(await self.page.locator('input[type="checkbox"]').is_checked())
        self.assertEqual(await self.page.locator('input[type="text"]').input_value(), "synthetic note")

    async def test_confirm_is_dismissed_unless_final_authority_is_set(self):
        await self.page.set_content(
            '<button aria-label="synthetic confirm" '
            'onclick="if(confirm(\'Synthetic confirm text\')) document.body.dataset.accepted=\'yes\'">Open</button>'
        )
        element_id = await self.observed_id("button")

        await act(self.session, {"kind": "click", "id": element_id})
        self.assertEqual(self.session.dialogs, ["Synthetic confirm text"])
        self.assertTrue(await self.page.locator("button").is_visible())
        self.assertIsNone(await self.page.locator("body").get_attribute("data-accepted"))
        self.assertTrue(any("dismissed without final-confirm authority" in note for note in self.session.notices))

        await act(self.session, {"kind": "click", "id": element_id, "confirm_final": True})
        self.assertEqual(self.session.dialogs, ["Synthetic confirm text", "Synthetic confirm text"])
        self.assertEqual(await self.page.locator("body").get_attribute("data-accepted"), "yes")
        self.assertFalse(self.session.confirm_final)

    async def test_goto_outside_allowed_origins_fails_before_navigation(self):
        await self.page.set_content("<p>synthetic page</p>")
        starting_url = self.page.url

        with self.assertRaisesRegex(ValueError, "stay on this run's websites"):
            await act(self.session, {"kind": "goto", "url": "https://outside.invalid/path"})

        self.assertEqual(self.page.url, starting_url)

    async def test_stale_observed_id_is_rejected(self):
        await self.page.set_content("<button>synthetic control</button>")
        stale_id = await self.observed_id("button")
        self.session.elements.clear()

        with self.assertRaisesRegex(ValueError, "Unknown element id"):
            await act(self.session, {"kind": "click", "id": stale_id})

    async def test_hiding_observed_control_removes_its_id_before_renumbering(self):
        await self.page.set_content(
            '<button id="synthetic-first">First</button>'
            '<button id="synthetic-second">Second</button>'
            '<script>document.querySelector("#synthetic-second").addEventListener('
            '"click", () => document.body.dataset.clicked = "yes")</script>'
        )
        await observe(self.session)
        first = self.page.locator("#synthetic-first")
        second = self.page.locator("#synthetic-second")
        self.assertIsNotNone(await first.get_attribute("data-rfq-observe"))
        self.assertIsNotNone(await second.get_attribute("data-rfq-observe"))

        await first.evaluate("element => element.style.display = 'none'")
        snapshot = await observe(self.session)

        self.assertIsNone(await first.get_attribute("data-rfq-observe"))
        visible_id = await second.get_attribute("data-rfq-observe")
        self.assertIsNotNone(visible_id)
        self.assertEqual(len(snapshot.elements), len(set(snapshot.elements)))
        await act(self.session, {"kind": "click", "id": int(visible_id)})
        self.assertEqual(await self.page.locator("body").get_attribute("data-clicked"), "yes")

    async def test_recover_restores_surviving_page_after_selected_popup_closes(self):
        await self.page.set_content(
            '<button onclick="window.open(\'about:blank\')">Open synthetic popup</button>'
            '<p>Synthetic original page</p>'
        )
        async with self.page.expect_popup() as pending_popup:
            await self.page.get_by_role("button", name="Open synthetic popup").click()
        popup = await pending_popup.value
        self.session.install_handlers(popup)
        await popup.set_content("<p>Synthetic popup content</p>")
        self.session.page = popup
        await popup.close()

        await recover(self.session)

        self.assertIs(self.session.page, self.page)
        self.assertTrue(any("Selected tab closed" in notice for notice in self.session.notices))

    async def test_cross_site_login_uses_current_origin_and_does_not_restore_other_site(self):
        form = '<form onsubmit="event.preventDefault()"><input name="user"><input type="password"></form>'
        await self.page.route("**/*", lambda route: route.fulfill(body=form, content_type="text/html"))
        first = {"siteId": "first", "url": "https://first.example.test/", "credentials": {"username": "first-user", "password": "first-pass"}}
        second = {"siteId": "second", "url": "https://second.example.test/", "credentials": {"username": "second-user", "password": "second-pass"}}
        self.session.site = first
        self.session.sites_by_origin = {"https://first.example.test": first, "https://second.example.test": second}
        self.session.last_good_url = first["url"] + "workspace"
        self.session.last_good_urls = {"https://first.example.test": self.session.last_good_url}
        await self.page.goto(second["url"])
        await recover(self.session)
        self.assertEqual(self.page.url, second["url"])
        self.assertEqual(await self.page.locator('input[name="user"]').input_value(), "")
        await login(self.session)
        self.assertEqual(await self.page.locator('input[name="user"]').input_value(), "second-user")
        self.assertEqual(await self.page.locator('input[type="password"]').input_value(), "second-pass")
        self.assertEqual(self.session.current_site["siteId"], "second")

    async def test_snapshot_distinguishes_checkbox_value_from_selection_and_marks_modal(self):
        await self.page.set_content('<input type="checkbox" value="on" aria-label="First declaration">'
            '<input type="checkbox" checked value="on" aria-label="Second declaration">'
            '<dialog open><button>Close synthetic notice</button></dialog>')
        snapshot = await observe(self.session)
        controls = [line for line in snapshot.text.splitlines() if line.startswith("[")]
        first = next(line for line in controls if "First declaration" in line)
        second = next(line for line in controls if "Second declaration" in line)
        close = next(line for line in controls if "Close synthetic notice" in line)
        self.assertIn("value=on UNCHECKED", first)
        self.assertIn("value=on CHECKED", second)
        self.assertIn("MODAL", close)

    async def test_snapshot_preserves_text_boundaries_without_splitting_inline_text(self):
        await self.page.set_content(
            '<div>RFQ-SYNTH-842</div>'
            '<div><h2>Next synthetic heading</h2>'
            '<span>MAT-</span><span>OMEGA-613</span></div>'
            '<dl><dt>LOT-SYNTH-274</dt><dd>Quantity detail</dd></dl>'
            '<div>Before break<br>DOC-SYNTH-509</div>'
            '<div style="display:none">HIDDEN-SYNTH-991</div>'
        )

        snapshot = await observe(self.session)
        lines = snapshot.text.splitlines()

        for expected in (
            "RFQ-SYNTH-842",
            "Next synthetic heading",
            "MAT-OMEGA-613",
            "LOT-SYNTH-274",
            "Quantity detail",
            "Before break",
            "DOC-SYNTH-509",
        ):
            with self.subTest(expected=expected):
                self.assertEqual(lines.count(expected), 1)
        self.assertNotIn("HIDDEN-SYNTH-991", snapshot.text)


if __name__ == "__main__":
    unittest.main()
