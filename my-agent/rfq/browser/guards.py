from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit


def _origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


async def consent(session):
    """Dismiss generic cookie consent using only consent-related page text."""
    for frame in session.page.frames:
        try:
            text = await frame.locator("body").inner_text(timeout=800)
            if len(text) > 3500 or not re.search(r"cookie|cookies|datenschutz|consent", text, re.I):
                continue
            button = frame.get_by_role("button", name=re.compile(r"(?:notwendige|necessary|essential).*cookies.*(?:akzeptieren|accept)|(?:alle akzeptieren|accept all|zustimmen)", re.I))
            if await button.count() == 1 and await button.is_visible():
                await button.click(timeout=3500)
                session.notices.append("Generic cookie consent accepted.")
                return
        except Exception:
            pass


async def login(session) -> str:
    """Fill a model-selected login form using generic credential field types."""
    for frame in session.page.frames:
        passwords = frame.locator('input[type="password"]:visible')
        if not await passwords.count():
            continue
        # An assigned worker may follow evidence to another run-provided site.
        site = getattr(session, "sites_by_origin", {}).get(_origin(frame.url),
                getattr(session, "current_site", session.site))
        credentials = site.get("credentials") or {}
        form = passwords.first.locator("xpath=ancestor::form[1]")
        scope = form if await form.count() else frame
        users = scope.locator('input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="checkbox"]):not([type="radio"]):not([readonly]):not([disabled]):visible')
        if not await users.count():
            return "Login needs a username field selected explicitly."
        await users.first.fill(credentials.get("username", ""))
        await passwords.first.fill(credentials.get("password", ""))
        await passwords.first.press("Enter")
        await asyncio.sleep(0.2)
        return "Login credentials entered; verify the next observation."
    return "No visible password form found."


async def recover(session, bus=None, agent=""):
    if session.page.is_closed():
        remaining = [page for page in session.context.pages if not page.is_closed()]
        if remaining:
            session.page = remaining[0]
            await session.page.bring_to_front()
        else:
            session.page = await session.context.new_page()
            session.install_handlers(session.page)
            await session.page.goto(session.last_good_url or session.site["url"], wait_until="domcontentloaded")
        session.notices.append("Selected tab closed; restored a remaining working tab.")
    await consent(session)
    # These are generic browser failure patterns, never site paths.
    text = ""
    try:
        text = await session.page.locator("body").inner_text(timeout=1200)
    except Exception:
        return
    if len(text) < 3000 and any(s in text.casefold() for s in ("service unavailable", "bad gateway", "temporarily unavailable")):
        for attempt in range(4):
            if bus:
                bus.emit("retry", agent, {"attempt": attempt + 1, "reason": "temporary page failure"})
            await asyncio.sleep(min(2 ** attempt, 8))
            await session.page.reload(wait_until="domcontentloaded")
            text = await session.page.locator("body").inner_text()
            if not any(s in text.casefold() for s in ("service unavailable", "bad gateway", "temporarily unavailable")):
                break
    # Reauthentication is allowed only after this session successfully observed work.
    current_origin = _origin(session.page.url)
    previous = getattr(session, "last_good_urls", {}).get(current_origin)
    if not previous and _origin(session.last_good_url) == current_origin:
        previous = session.last_good_url
    if previous:
        for frame in session.page.frames:
            if await frame.locator('input[type="password"]:visible').count():
                message = await login(session)
                await consent(session)
                if bus:
                    bus.emit("relogin", agent, {"reason": "session expired"})
                try:
                    await session.page.goto(previous, wait_until="domcontentloaded")
                except Exception:
                    pass
                session.notices.append("Session renewal attempted: " + message)
                break
    else:
        # The model initiates first login; it sees consent walls in the DOM.
        return
