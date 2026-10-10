"""Optional model-driven transfer checks on generated, unrelated RFQ websites.

Requires the model proxy and RUN_MODEL_TESTS=1. No public scenario data is used.
Every expected answer is generated afresh and exposed only through fixture pages.
"""

import asyncio
import os
import random
import time
import unittest
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from playwright.async_api import async_playwright

import rfq_agent


@unittest.skipUnless(os.environ.get("RUN_MODEL_TESTS") == "1", "Set RUN_MODEL_TESTS=1 with the model proxy running")
class Transfer(unittest.IsolatedAsyncioTestCase):
    async def test_generated_websites_and_values(self):
        rng = random.SystemRandom()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                for variant in range(2):
                    with self.subTest(variant=variant):
                        context = await browser.new_context()
                        token = uuid4().hex[:12]
                        ids = ["site_" + uuid4().hex[:8] for _ in range(3)]
                        origins = [f"https://{uuid4().hex}.example.test" for _ in range(3)]
                        detail, form, submit = ["/" + uuid4().hex for _ in range(3)]
                        quantity = rng.randint(3, 16)
                        price = Decimal(rng.randint(400, 2500)) / 100
                        weeks = rng.randint(1, 6)
                        reference, material, tan = "SYN-" + token, "PART-" + uuid4().hex[:8], str(rng.randint(100000, 999999))
                        expected = {"reference": reference, "material": material, "quantity": str(quantity),
                                    "unit_price": str(price), "lead_time": str(weeks),
                                    "total": str(price * quantity), "tan": tan}
                        captured = []
                        spec = (
                            f"<h1>Current specification and supplier register</h1><p>Reference {reference}. "
                            f"One position: 1. Quantity {quantity} pieces. Required bore 43 mm, stroke 170 mm, "
                            "pressure 155 bar, mounting type L, certification C.</p>"
                            f"<p>Material {material}: ACTIVE, bore 43 mm, stroke 170 mm, pressure 155 bar, "
                            f"mounting L, certification C. Valid price EUR {price} per piece for all quantities. "
                            f"Stock 100 pieces. Stock lead time {weeks} weeks. Production lead time 20 weeks.</p>"
                            f"<p>Material OLD-{token}: INACTIVE, same technical attributes.</p>"
                            "<p>No discounts, surcharges, fees, packaging charges or bonuses apply. These are the "
                            "complete current records for this small synthetic task.</p>"
                        )
                        fields = [("reference", "Tender reference"), ("material", "Material number"),
                                  ("quantity", "Quantity in pieces"), ("unit_price", "Unit price in EUR"),
                                  ("lead_time", "Lead time in weeks"), ("total", "Total net in EUR"), ("tan", "Signing TAN")]
                        if variant:
                            rng.shuffle(fields)
                        form_html = f'<h1>Bid for {reference}</h1><form action="{submit}">' + "".join(
                            f'<label>{label}<input name="{key}" required></label><br>' for key, label in fields
                        ) + '<button type="submit">Sign and submit bid</button></form>'

                        async def serve(route):
                            parsed = urlparse(route.request.url)
                            origin = f"{parsed.scheme}://{parsed.netloc}"
                            if origin == origins[0]:
                                body = spec if parsed.path == detail else f'<h1>Records workspace</h1><a href="{detail}">Current tender specification and supplier register</a>'
                            elif origin == origins[1]:
                                body = f"<h1>Signing mailbox</h1><p>Current signing TAN for {reference}: {tan}</p>"
                            elif parsed.path == submit:
                                values = {key: items[0] for key, items in parse_qs(parsed.query).items()}
                                captured.append(values)
                                valid = all(key in values and (Decimal(values[key]) == Decimal(value)
                                            if key in ("quantity", "unit_price", "lead_time", "total")
                                            else values[key] == value) for key, value in expected.items())
                                body = f"<h1>Submission received</h1><p>Signed bid accepted. Receipt SYN-{token}</p>" if valid else (
                                    '<h1>Validation failed</h1><p>Some fields do not match the current records. Return to the form.</p>' + form_html)
                            else:
                                body = form_html if parsed.path == form else f'<h1>Procurement workspace</h1><a href="{form}">Prepare a signed bid</a>'
                            await route.fulfill(content_type="text/html", body=body)

                        for origin in origins:
                            await context.route(origin + "/**", serve)
                        task = {
                            "brief": f"Synthetic transfer check. Prepare ONE signed bid for {reference}. "
                                     f"{ids[0]} contains the current specification and active supplier records; "
                                     f"{ids[1]} is the signing mailbox; {ids[2]} accepts the bid. "
                                     "These sites need no login. Follow their visible links. Match all technical "
                                     "attributes and active status, use the current EUR price and stock lead time. "
                                     "Compute total as quantity times unit price. This fixture has no additional "
                                     "business records or Q&A. Complete the fields actually present in its form "
                                     "and enter the TAN from the mailbox to sign. Stop only at a visible receipt.",
                            "access": [{"siteId": site, "url": origin + "/", "credentials": {"username": "unused", "password": "unused"}}
                                       for site, origin in zip(ids, origins)],
                            "submission": {"kind": "site", "siteId": ids[2], "url": origins[2]},
                            "expires_at": int((time.time() + 19 * 60) * 1000),
                        }
                        async def new_page():
                            return await context.new_page()
                        contexts = {site: new_page for site in ids}
                        result = await asyncio.wait_for(rfq_agent.run(task, contexts), timeout=19 * 60)
                        self.assertTrue(result and result.get("submitted"))
                        self.assertTrue(captured)
                        self.assertEqual(captured[-1]["material"], material)
                        self.assertEqual(Decimal(captured[-1]["total"]), price * quantity)
                        await context.close()
            finally:
                await browser.close()
