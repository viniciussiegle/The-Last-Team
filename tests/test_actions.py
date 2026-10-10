from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

PACKAGE = Path(__file__).resolve().parents[1] / "my-agent"
sys.path.insert(0, str(PACKAGE))

from rfq.browser.actions import Blocked, act, act_many  # noqa: E402
from rfq.browser.session import BrowserSession  # noqa: E402
from rfq.browser.snapshot import PageUnresponsive, observe  # noqa: E402
from rfq.browser.guards import abandon, login, recover  # noqa: E402


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

    async def test_fill_accepts_one_observed_id_and_text(self):
        await self.page.set_content('<input type="text" aria-label="synthetic single field">')
        field_id = await self.observed_id("input")

        await act(self.session, {"kind": "fill", "id": field_id, "text": "synthetic value"})

        self.assertEqual(await self.page.locator("input").input_value(), "synthetic value")

    async def test_fill_submit_confirmation_is_scoped_and_resets_authority(self):
        await self.page.set_content(
            '<input type="text" aria-label="synthetic form value">'
            '<button type="button" aria-label="synthetic final action" '
            'onclick="if(confirm(\'Synthetic final confirmation\')) document.body.dataset.submitted=\'yes\'">'
            'Finish</button>'
        )
        field_id = await self.observed_id("input")
        submit_id = await self.observed_id("button")

        await act(self.session, {"kind": "fill", "fields": [{"id": field_id, "text": "synthetic value"}],
                                 "submit_id": submit_id})
        self.assertEqual(self.session.dialogs, ["Synthetic final confirmation"])
        self.assertIsNone(await self.page.locator("body").get_attribute("data-submitted"))
        self.assertFalse(self.session.confirm_final)

        await act(self.session, {"kind": "fill", "fields": [{"id": field_id, "text": "synthetic value"}],
                                 "submit_id": submit_id, "confirm_final": True})
        self.assertEqual(self.session.dialogs,
                         ["Synthetic final confirmation", "Synthetic final confirmation"])
        self.assertEqual(await self.page.locator("body").get_attribute("data-submitted"), "yes")
        self.assertFalse(self.session.confirm_final)

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

    async def test_snapshot_excludes_hidden_iframes_but_keeps_visible_nested_frame(self):
        await self.page.set_content('<iframe id="hidden-frame" style="display:none"></iframe>'
                                    '<iframe id="visible-parent"></iframe>')
        await self.page.locator("#hidden-frame").evaluate(
            "element => element.srcdoc = '<p>HIDDEN-ROOT-SYNTH-114</p>'")
        await self.page.locator("#visible-parent").evaluate(
            "element => element.srcdoc = '<p>VISIBLE-PARENT-SYNTH-226</p>' + "
            "'<iframe id=\"nested-hidden\" style=\"display:none\"></iframe>' + "
            "'<iframe id=\"nested-visible\"></iframe>'")
        parent = None
        for frame in self.page.frames:
            if frame.parent_frame is self.page.main_frame and \
                    "VISIBLE-PARENT-SYNTH-226" in await frame.locator("body").inner_text():
                parent = frame
                break
        self.assertIsNotNone(parent)
        await parent.locator("#nested-hidden").evaluate(
            "element => element.srcdoc = '<p>HIDDEN-NESTED-SYNTH-338</p>'")
        await parent.locator("#nested-visible").evaluate(
            "element => element.srcdoc = '<p>VISIBLE-NESTED-SYNTH-449</p>'")
        await self.page.wait_for_timeout(50)

        snapshot = await observe(self.session)

        self.assertIn("VISIBLE-PARENT-SYNTH-226", snapshot.text)
        self.assertIn("VISIBLE-NESTED-SYNTH-449", snapshot.text)
        self.assertNotIn("HIDDEN-ROOT-SYNTH-114", snapshot.text)
        self.assertNotIn("HIDDEN-NESTED-SYNTH-338", snapshot.text)

    async def test_snapshot_marks_overlay_obstruction_and_clears_it_when_hidden(self):
        await self.page.set_content(
            '<button id="synthetic-target">Synthetic target</button>'
            '<div id="synthetic-overlay" style="position:fixed;inset:0;z-index:10;'
            'background:rgba(0,0,0,.2)">Synthetic overlay</div>'
        )

        obscured = await observe(self.session)
        button_line = next(line for line in obscured.text.splitlines()
                           if line.startswith("[") and "Synthetic target" in line)
        self.assertIn("OBSCURED by Synthetic overlay", button_line)

        await self.page.locator("#synthetic-overlay").evaluate("element => element.remove()")
        clear = await observe(self.session)
        button_line = next(line for line in clear.text.splitlines()
                           if line.startswith("[") and "Synthetic target" in line)
        self.assertNotIn("OBSCURED", button_line)

    async def test_batch_keeps_its_targets_when_a_notice_appears_and_renumbers_controls(self):
        await self.page.set_content(
            '<main><button id="first" aria-label="synthetic first" '
            'onclick="const d=document.createElement(\'section\');d.innerHTML='
            '\'<button>synthetic notice close</button><a href=/synthetic-notice>synthetic notice link</a>\';'
            'document.body.prepend(d)">One</button>'
            '<input id="field" aria-label="synthetic field">'
            '<button id="second" aria-label="synthetic second" onclick="document.body.dataset.second=\'yes\'">Two</button></main>'
        )
        first = await self.observed_id("#first")
        field = int(await self.page.locator("#field").get_attribute("data-rfq-observe"))
        second = int(await self.page.locator("#second").get_attribute("data-rfq-observe"))

        result = await act_many(self.session, [{"kind": "click", "id": first},
                                              {"kind": "type", "id": field, "text": "synthetic text"},
                                              {"kind": "click", "id": second}])

        self.assertEqual(await self.page.locator("#field").input_value(), "synthetic text")
        self.assertEqual(await self.page.locator("body").get_attribute("data-second"), "yes")
        self.assertNotIn("skipped", result)
        # The inserted controls really did shift the numbering the model saw.
        self.assertNotEqual(int(await self.page.locator("#second").get_attribute("data-rfq-observe")), second)

    async def test_batch_follows_dependent_dropdowns_through_rerenders(self):
        await self.page.set_content(
            '<form id="f"></form><script>'
            'const sizes={"11":["210","340"],"13":["560"]};let size="",depth="";'
            'function draw(){document.getElementById("f").innerHTML='
            '`<select aria-label="synthetic size"><option value="">choose</option>${Object.keys(sizes).map(s=>`<option ${s===size?"selected":""}>${s}</option>`).join("")}</select>`+'
            '`<select aria-label="synthetic depth" ${size?"":"disabled"}><option value="">choose</option>${(sizes[size]||[]).map(d=>`<option ${d===depth?"selected":""}>${d}</option>`).join("")}</select>`+'
            '`<button type="button" aria-label="synthetic apply">Apply</button>`;'
            'document.querySelector("button").onclick=()=>{size=document.querySelectorAll("select")[0].value;'
            'depth=document.querySelectorAll("select")[1].value;draw()};}draw();</script>'
        )
        size = await self.observed_id('select[aria-label="synthetic size"]')
        depth = int(await self.page.locator('select[aria-label="synthetic depth"]').get_attribute("data-rfq-observe"))
        apply = int(await self.page.locator("button").get_attribute("data-rfq-observe"))

        result = await act_many(self.session, [{"kind": "select", "id": size, "option": "13"},
                                              {"kind": "click", "id": apply},
                                              {"kind": "select", "id": depth, "option": "560"},
                                              {"kind": "click", "id": apply}])

        self.assertNotIn("skipped", result)
        self.assertEqual(await self.page.locator('select[aria-label="synthetic depth"]').input_value(), "560")

    async def test_batch_never_retargets_a_lookalike_link_after_reordering(self):
        await self.page.set_content(
            '<button id="shuffle" aria-label="synthetic shuffle" onclick="const p=document.getElementById(\'links\');'
            'p.append(p.firstElementChild)">Shuffle</button>'
            '<p id="links"><a href="#synthetic-real" onclick="document.body.dataset.opened=\'real\'">Open</a>'
            '<a href="#synthetic-decoy" onclick="document.body.dataset.opened=\'decoy\'">Open</a></p>'
        )
        shuffle = await self.observed_id("#shuffle")
        real = int(await self.page.locator('a[href="#synthetic-real"]').get_attribute("data-rfq-observe"))

        await act_many(self.session, [{"kind": "click", "id": shuffle}, {"kind": "click", "id": real}])

        self.assertEqual(await self.page.locator("body").get_attribute("data-opened"), "real")

    async def test_batch_stops_when_a_target_is_gone(self):
        await self.page.set_content(
            '<button id="remove" aria-label="synthetic remove" onclick="document.getElementById(\'gone\').remove()">Remove</button>'
            '<button id="gone" aria-label="synthetic gone" onclick="document.body.dataset.clicked=\'yes\'">Gone</button>'
            '<button id="after" aria-label="synthetic after" onclick="document.body.dataset.after=\'yes\'">After</button>'
        )
        remove = await self.observed_id("#remove")
        gone = int(await self.page.locator("#gone").get_attribute("data-rfq-observe"))
        after = int(await self.page.locator("#after").get_attribute("data-rfq-observe"))

        result = await act_many(self.session, [{"kind": "click", "id": remove}, {"kind": "click", "id": gone},
                                              {"kind": "click", "id": after}])

        self.assertIn("no longer present", result)
        self.assertIn("skipped", result)
        self.assertIsNone(await self.page.locator("body").get_attribute("data-after"))

    async def test_covered_control_is_refused_quickly_and_names_what_covers_it(self):
        await self.page.set_content(
            '<button id="target" aria-label="synthetic target">Target</button>'
            '<dialog open aria-label="Synthetic survey" style="position:fixed;inset:0;width:100%;height:100%">'
            '<button>Synthetic close</button></dialog>'
        )
        target = await self.observed_id("#target")
        started = time.monotonic()

        with self.assertRaises(Blocked) as refused:
            await act(self.session, {"kind": "click", "id": target})

        self.assertLess(time.monotonic() - started, 6)
        self.assertIn("Synthetic survey", str(refused.exception))
        self.assertLess(len(str(refused.exception)), 300)

    async def test_temporary_failure_page_is_retried_through_its_own_control(self):
        await self.page.set_content(
            '<h1>Dienst vorübergehend nicht verfügbar</h1>'
            '<a href="#" onclick="document.body.innerHTML=\'<h1>Synthetic work area</h1>\';return false">Erneut versuchen</a>'
        )

        await recover(self.session)

        self.assertEqual(await self.page.locator("h1").inner_text(), "Synthetic work area")
        self.assertIn("A temporary error page was retried automatically.", self.session.notices)

    async def test_form_page_mentioning_an_outage_is_left_untouched(self):
        await self.page.set_content(
            '<p>Service unavailable for the synthetic lookup; enter the value manually.</p>'
            '<input aria-label="synthetic entry"><a href="#" onclick="document.body.dataset.retried=\'yes\'">Retry</a>'
        )
        await self.page.locator("input").fill("synthetic unsaved value")

        await recover(self.session)

        self.assertEqual(await self.page.locator("input").input_value(), "synthetic unsaved value")
        self.assertIsNone(await self.page.locator("body").get_attribute("data-retried"))
        self.assertEqual(self.session.notices, [])

    async def test_busy_page_fails_observation_within_the_limit_instead_of_blocking(self):
        await self.page.set_content('<p>synthetic busy page</p>')
        await self.page.evaluate("setTimeout(() => { for (;;) {} }, 50)")
        await self.page.wait_for_timeout(200)
        started = time.monotonic()

        with self.assertRaises(PageUnresponsive):
            await observe(self.session)

        self.assertLess(time.monotonic() - started, 12)

    async def test_replacement_tab_is_not_treated_as_a_website_popup(self):
        self.context.on("page", self.session.on_page)  # as BrowserSession.connect registers it
        self.session.last_good_url = (
            "data:text/html,<button onclick=\"document.body.dataset.answer=confirm('Synthetic question')\">Ask</button>")

        await abandon(self.session)
        await self.session.page.wait_for_timeout(300)  # time for any wrongly scheduled popup capture to run
        await self.session.page.locator("button").click()
        snapshot = await observe(self.session)

        self.assertEqual(self.session.dialogs, ["Synthetic question"])
        self.assertNotIn("Popup:", snapshot.text)
        self.assertEqual(await self.session.page.locator("body").get_attribute("data-answer"), "false")

    async def test_website_popup_is_still_captured(self):
        self.context.on("page", self.session.on_page)
        # A served address: page scripts are not allowed to open data: addresses as top-level tabs.
        await self.context.route("https://synthetic.example.test/**", lambda route: route.fulfill(
            status=200, content_type="text/html",
            body="<p>synthetic popup text</p>" if route.request.url.endswith("/popup") else
                 '<button onclick="window.open(\'/popup\')">Open</button>'))
        await self.page.goto("https://synthetic.example.test/opener")

        await self.page.locator("button").click()
        for _ in range(30):
            if any(notice.startswith("Popup") for notice in self.session.notices):
                break
            await self.page.wait_for_timeout(100)
        snapshot = await observe(self.session)

        self.assertEqual(len(self.context.pages), 2)
        self.assertIn("Popup: synthetic popup text", snapshot.text)

    async def test_entry_site_is_retried_on_a_fresh_tab_when_a_load_gets_no_answer(self):
        attempts = []

        async def unanswered_once(route):
            attempts.append(route.request.url)
            if len(attempts) == 1:
                return  # leave the first request without any response
            await route.fulfill(status=200, content_type="text/html", body="<p>synthetic entry page</p>")
        await self.context.route("https://synthetic.example.test/**", unanswered_once)
        first_tab = self.session.page
        started = time.monotonic()

        await self.session.open_entry(attempts=3, limit_ms=1500)
        snapshot = await observe(self.session)

        self.assertEqual(len(attempts), 2)
        self.assertLess(time.monotonic() - started, 12)
        self.assertIsNot(self.session.page, first_tab)
        self.assertIn("synthetic entry page", snapshot.text)

    async def test_unresponsive_tab_is_replaced_by_a_working_one(self):
        await self.page.set_content('<p>synthetic busy page</p>')
        await self.page.evaluate("setTimeout(() => { for (;;) {} }, 50)")
        await self.page.wait_for_timeout(200)
        stuck = self.session.page
        self.session.last_good_url = "data:text/html,<p>synthetic restored page</p>"
        started = time.monotonic()

        await abandon(self.session)
        snapshot = await observe(self.session)

        self.assertLess(time.monotonic() - started, 25)
        self.assertIsNot(self.session.page, stuck)
        self.assertIn("synthetic restored page", snapshot.text)
        self.assertIn("stopped responding", snapshot.text)
        self.assertTrue(stuck.is_closed())


if __name__ == "__main__":
    unittest.main()
