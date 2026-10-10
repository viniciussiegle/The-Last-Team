from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


@dataclass
class BrowserSession:
    browser: object
    context: object
    page: object
    site: dict
    allowed_origins: set[str]
    elements: dict = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)
    confirm_final: bool = False
    last_good_url: str = ""
    last_snapshot: object = None
    nav_hashes: set[str] = field(default_factory=set)
    local: bool = False

    def install_handlers(self, page):
        async def dialog_handler(dialog):
            self.dialogs.append(dialog.message)
            if self.confirm_final:
                await dialog.accept()
            else:
                await dialog.dismiss()
                self.notices.append("Dialog dismissed without final-confirm authority: " + dialog.message)
        page.on("dialog", dialog_handler)

    async def capture_popup(self, page):
        self.install_handlers(page)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=4000)
            content = await page.locator("body").inner_text(timeout=1500)
            self.notices.append("Popup: " + content[:6000])
        except Exception:
            self.notices.append("Popup opened and may have closed; inspect tabs or originating page.")

    @classmethod
    async def connect(cls, playwright, settings, site: dict, index: int, sites: list[dict]):
        if settings.cdp_urls:
            if index >= len(settings.cdp_urls):
                raise ValueError("Insufficient platform browsers for site workers")
            browser = await playwright.chromium.connect_over_cdp(settings.cdp_urls[index])
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
        else:
            browser = await playwright.chromium.launch(headless=settings.headless)
            context = await browser.new_context(viewport={"width": 1280, "height": 800}, accept_downloads=True)
        page = context.pages[0] if context.pages else await context.new_page()
        page.set_default_timeout(7000)
        session = cls(browser, context, page, site, {origin(s["url"]) for s in sites}, local=not settings.cdp_urls)
        session.install_handlers(page)
        context.on("page", lambda popup: asyncio.create_task(session.capture_popup(popup)))
        # The run assigns an entry site, not an investigation path.
        await page.goto(site["url"], wait_until="domcontentloaded", timeout=45000)
        return session

    async def close(self):
        if self.local:
            await self.browser.close()
