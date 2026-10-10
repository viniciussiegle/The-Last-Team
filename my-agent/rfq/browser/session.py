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
    request_factory: object = None
    sites_by_origin: dict = field(default_factory=dict)
    last_good_urls: dict = field(default_factory=dict)
    opening_own_tab: bool = False

    @property
    def current_site(self):
        return self.sites_by_origin.get(origin(self.page.url), self.site)

    def install_handlers(self, page):
        page.set_default_timeout(7000)
        async def dialog_handler(dialog):
            self.dialogs.append(dialog.message)
            if self.confirm_final:
                await dialog.accept()
            else:
                await dialog.dismiss()
                self.notices.append("Dialog dismissed without final-confirm authority: " + dialog.message)
        page.on("dialog", dialog_handler)

    async def new_tab(self):
        """Open a tab of the session's own, which is not a popup of the website."""
        self.opening_own_tab = True
        try:
            page = await self.context.new_page()
        finally:
            self.opening_own_tab = False
        self.install_handlers(page)
        return page

    def on_page(self, page):
        # Decided when the browser reports the tab, not later in a task, so the session's own tabs never count.
        if not self.opening_own_tab:
            asyncio.create_task(self.capture_popup(page))

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
        session.request_factory = playwright.request
        session.sites_by_origin = {origin(s["url"]): s for s in sites}
        session.install_handlers(page)
        # The run assigns an entry site, not an investigation path.
        await session.open_entry()
        context.on("page", session.on_page)
        return session

    async def open_entry(self, attempts: int = 4, limit_ms: int = 12000):
        """Load the entry site, on a fresh tab after each attempt that gets no answer.

        A request can occasionally be left without any response while the site serves others normally, and the
        tab that made it stays unusable. One such request must not end the run before it starts.
        """
        for attempt in range(1, attempts + 1):
            try:
                await self.page.goto(self.site["url"], wait_until="domcontentloaded", timeout=limit_ms)
                return
            except Exception:
                if attempt == attempts:
                    raise
                stuck, self.page = self.page, await self.new_tab()
                try:
                    await asyncio.wait_for(stuck.close(), 3)
                except Exception:
                    pass

    async def close(self):
        if self.local:
            await self.browser.close()
