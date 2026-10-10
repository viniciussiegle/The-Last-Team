"""Optional real-browser regression tests, using synthetic pages only."""

import os
import unittest
from uuid import uuid4

from playwright.async_api import async_playwright

from browser_tools import BrowserTools


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "Set RUN_BROWSER_TESTS=1 after installing Chromium")
class BrowserRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        self.helper = BrowserTools(
            {"access": [{"siteId": "fixture", "url": "https://example.test/"}]},
            {"fixture": self.page},
        )

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_disclosures_zero_id_and_slider(self):
        await self.page.set_content(
            '<details><summary>Read document</summary><p>Visible evidence</p></details>'
            '<input type="range" min="0" max="100" value="0">'
            '<button style="visibility:hidden">Hidden</button>'
            '<input aria-label="Display only" readonly value="Example">'
        )
        state = await self.helper.inspect("fixture")
        self.assertEqual(len(state["controls"]), 3)
        self.assertEqual(state["controls"][0]["id"], 0)
        self.assertEqual(state["controls"][0]["frame"], 0)
        self.assertEqual(state["controls"][0]["tag"], "summary")
        state = await self.helper.click("fixture", 0)
        self.assertIn("Visible evidence", state["text"])
        state = await self.helper.fill("fixture", 1, "100")
        self.assertEqual(state["controls"][1]["value"], "100")
        self.assertTrue(state["controls"][2]["readonly"])
        with self.assertRaises(ValueError):
            await self.helper.fill("fixture", 2, "New value")

    async def test_background_canvas_observation_and_screenshot(self):
        await self.page.set_content(
            '<canvas aria-label="Confirmation code" width="200" height="70"></canvas>'
            '<script>let c=document.querySelector("canvas").getContext("2d");'
            'c.font="30px sans-serif";c.fillText("ABCD",10,45)</script>'
        )
        other = await self.context.new_page()
        await other.bring_to_front()
        state = await self.helper.inspect("fixture")
        self.assertEqual(state["visual_regions"][0]["label"], "Confirmation code")
        screenshot = await self.helper.screenshot("fixture")
        self.assertTrue(screenshot.startswith("data:image/jpeg;base64,"))
        self.assertGreater(len(screenshot), 1000)

    async def test_observed_control_remains_bound_after_dom_insertion(self):
        await self.page.set_content('<button onclick="this.textContent=\'Chosen action completed\'">Chosen action</button>')
        state = await self.helper.inspect("fixture")
        chosen = state["controls"][0]["id"]
        await self.page.evaluate("""() => {
            const banner = document.createElement('button');
            banner.textContent = 'New unrelated banner';
            document.body.prepend(banner);
        }""")
        state = await self.helper.click("fixture", chosen)
        self.assertIn("Chosen action completed", state["text"])

    async def test_changed_hosts_routes_and_multistep_login(self):
        # Generate unrelated fixtures at execution time. Select every control
        # from its observation rather than relying on an index or public route.
        for _ in range(2):
            token = uuid4().hex
            site, origin = "site_" + token, f"https://{token}.example.test"
            path, destination = f"/{uuid4().hex}/entry", f"/{uuid4().hex}/records"
            username, password = "user_" + uuid4().hex, uuid4().hex
            html = (
                '<button disabled>Unrelated action</button>'
                '<input type="email" aria-label="Account address">'
                '<button onclick="document.querySelector(\'section\').hidden=false">Continue</button>'
                '<section hidden><input type="password" aria-label="Access key">'
                f'<a href="{destination}">Open workspace</a></section>'
            )
            async def serve(route):
                body = html if route.request.url.endswith(path) else "<h1>New workspace</h1>"
                await route.fulfill(body=body, content_type="text/html")
            await self.context.route(origin + "/**", serve)
            helper = BrowserTools({"access": [{"siteId": site, "url": origin + path,
                "credentials": {"username": username, "password": password}}]}, {site: self.page})
            state = await helper.navigate(site)
            account = next(c["id"] for c in state["controls"] if c.get("aria") == "Account address")
            await helper.fill_credential(site, account, "username")
            self.assertEqual(await self.page.get_by_label("Account address").input_value(), username)
            next_button = next(c["id"] for c in state["controls"] if c.get("text") == "Continue")
            state = await helper.click(site, next_button)
            secret = next(c["id"] for c in state["controls"] if c.get("type") == "password")
            state = await helper.fill_credential(site, secret, "password")
            self.assertEqual(await self.page.get_by_label("Access key").input_value(), password)
            self.assertNotIn(password, str(state))
            link = next(c for c in state["controls"] if c.get("text") == "Open workspace")
            state = await helper.navigate(site, link["href"])
            self.assertEqual(state["url"], origin + destination)
            self.assertIn("New workspace", state["text"])
            await self.context.unroute(origin + "/**", serve)
