from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit


def _origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


# Generic wording of transient server failures and of a retry control, in the languages the sites may use.
TEMPORARY_FAILURE = re.compile(
    r"service unavailable|bad gateway|gateway time-?out|temporarily unavailable|try again later"
    r"|vor[uü]bergehend nicht verf[uü]gbar|derzeit nicht verf[uü]gbar|zurzeit nicht verf[uü]gbar"
    r"|sp[aä]ter erneut versuchen", re.I)
RETRY_CONTROL = re.compile(r"^\s*(erneut versuchen|nochmals? versuchen|wiederholen|retry|try again)\s*$", re.I)


async def count(locator, limit: float = 2) -> int:
    """How many elements match, or none when their frame does not answer: a frame whose document never arrives
    would otherwise keep the caller waiting for good."""
    try:
        return await asyncio.wait_for(locator.count(), limit)
    except Exception:
        return 0


def temporary_failure(text: str) -> bool:
    """A short page that only reports a transient failure, not a working page that mentions one."""
    return len(text) < 1500 and bool(TEMPORARY_FAILURE.search(text))


async def consent(session):
    """Dismiss generic cookie consent using only consent-related page text."""
    for frame in session.page.frames:
        try:
            text = await frame.locator("body").inner_text(timeout=800)
            if len(text) > 3500 or not re.search(r"cookie|cookies|datenschutz|consent", text, re.I):
                continue
            button = frame.get_by_role("button", name=re.compile(r"(?:notwendige|necessary|essential).*cookies.*(?:akzeptieren|accept)|(?:alle akzeptieren|accept all|zustimmen)", re.I))
            if await count(button) == 1 and await button.is_visible():
                await button.click(timeout=3500)
                session.notices.append("Generic cookie consent accepted.")
                return
        except Exception:
            pass


# An informational notice: a dialog without form fields that offers its own close control. Its more prominent
# control may lead away from the work, so the close control is used, and only that one.
NOTICE_JS = r"""() => {
  const shown = e => { const r=e.getBoundingClientRect(), s=getComputedStyle(e); return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'; };
  const closed = [];
  for (const box of document.querySelectorAll('dialog[open],[role=dialog],[aria-modal=true]')) {
    if (!shown(box) || Array.from(box.querySelectorAll('input,select,textarea')).some(shown)) continue;
    const close = Array.from(box.querySelectorAll('button,[role=button]')).filter(shown).find(e =>
      /^(schlie(ß|ss)en|close|dismiss|×|✕|x)$/i.test(((e.getAttribute('aria-label')||'')||e.innerText||'').trim()));
    if (close) { closed.push((box.getAttribute('aria-label')||box.innerText||'').replace(/\s+/g,' ').trim().slice(0,80)); close.click(); }
  }
  return closed;
}"""


async def close_notices(session):
    for frame in session.page.frames:
        try:
            for label in await asyncio.wait_for(frame.evaluate(NOTICE_JS), 2):
                session.notices.append(f"An informational notice was closed: {label}")
        except Exception:
            pass


async def login(session) -> str:
    """Fill a model-selected login form using generic credential field types."""
    for frame in session.page.frames:
        passwords = frame.locator('input[type="password"]:visible')
        if not await count(passwords):
            continue
        # An assigned worker may follow evidence to another run-provided site.
        site = getattr(session, "sites_by_origin", {}).get(_origin(frame.url),
                getattr(session, "current_site", session.site))
        credentials = site.get("credentials") or {}
        form = passwords.first.locator("xpath=ancestor::form[1]")
        scope = form if await count(form) else frame
        users = scope.locator('input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="checkbox"]):not([type="radio"]):not([readonly]):not([disabled]):visible')
        if not await count(users):
            return "Login needs a username field selected explicitly."
        await users.first.fill(credentials.get("username", ""))
        await passwords.first.fill(credentials.get("password", ""))
        await passwords.first.press("Enter")
        await asyncio.sleep(0.2)
        return "Login credentials entered; verify the next observation."
    return "No visible password form found."


async def abandon(session, bus=None, agent=""):
    """Replace a tab that stopped responding with a fresh one at the last working address."""
    stuck = session.page
    session.page = await session.new_tab()
    # Back to where the work was; if that very page is what does not load, start again from the site's entry.
    session.replaced_in_a_row += 1
    target = session.last_good_urls.get(_origin(session.last_good_url), session.last_good_url) or session.site["url"]
    if session.replaced_in_a_row > 1:
        target = session.site["url"]
    try:
        await session.page.goto(target, wait_until="domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        # Closing is a browser-level request, so it does not depend on the stuck tab answering.
        await asyncio.wait_for(stuck.close(), 3)
    except Exception:
        pass
    session.notices.append("The previous tab stopped responding and was replaced; unsaved entries in it are lost.")
    if bus:
        bus.emit("retry", agent, {"reason": "unresponsive tab replaced"})


async def recover(session, bus=None, agent=""):
    if session.page.is_closed():
        remaining = [page for page in session.context.pages if not page.is_closed()]
        if remaining:
            session.page = remaining[0]
            await session.page.bring_to_front()
        else:
            session.page = await session.new_tab()
            await session.page.goto(session.last_good_url or session.site["url"], wait_until="domcontentloaded")
        session.notices.append("Selected tab closed; restored a remaining working tab.")
    await consent(session)
    await close_notices(session)
    # These are generic browser failure patterns, never site paths.
    text = ""
    try:
        text = await session.page.locator("body").inner_text(timeout=1200)
    except Exception:
        return
    # A page with form fields is someone's work in progress: never retry or reload it here, whatever it says.
    try:
        # count() has no timeout of its own; an unresponsive tab must reach the bounded observation instead.
        has_form = await asyncio.wait_for(
            session.page.locator("input:not([type=hidden]):visible, select:visible, textarea:visible").count(), 3) > 0
    except Exception:
        has_form = True
    if temporary_failure(text) and not has_form:
        for attempt in range(4):
            if bus:
                bus.emit("retry", agent, {"attempt": attempt + 1, "reason": "temporary page failure"})
            await asyncio.sleep(min(0.5 * 2 ** attempt, 4))
            try:
                # Prefer the page's own single retry control; reloading can repeat a failed form post.
                again = session.page.get_by_role("link", name=RETRY_CONTROL).or_(
                    session.page.get_by_role("button", name=RETRY_CONTROL))
                if await count(again) == 1:
                    await again.click(timeout=3500)
                    await session.page.wait_for_load_state("domcontentloaded", timeout=5000)
                else:
                    await session.page.reload(wait_until="domcontentloaded")
                text = await session.page.locator("body").inner_text(timeout=3000)
            except Exception:
                break
            if not temporary_failure(text):
                session.notices.append("A temporary error page was retried automatically.")
                break
    # Reauthentication is allowed only after this session successfully observed work.
    current_origin = _origin(session.page.url)
    previous = getattr(session, "last_good_urls", {}).get(current_origin)
    if not previous and _origin(session.last_good_url) == current_origin:
        previous = session.last_good_url
    if previous:
        for frame in session.page.frames:
            if await count(frame.locator('input[type="password"]:visible')):
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
