"""Model-directed browser actions for the challenge's live websites.

Each observation covers only a page the model chose to open. Document extraction
accepts only links or frames observed on that page; it never crawls a site.
"""

import asyncio
import base64
import html
import io
import json
import math
import re
import zipfile
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urljoin, urlparse

from pypdf import PdfReader


# Readable page text: includes collapsed <details>, tooltips and canvas/image labels, which
# innerText omits, but skips content hidden from assistive technology (aria-hidden/hidden).
READABLE_TEXT = r"""root => {
  // Readable text of a page including collapsed <details>, tooltips and canvas/img labels,
  // excluding content hidden from assistive technology (aria-hidden / hidden attribute).
  const BLOCK = new Set(['P','DIV','TR','LI','H1','H2','H3','H4','H5','H6','SECTION','ARTICLE','TABLE','THEAD','TBODY','UL','OL','DETAILS','SUMMARY','HEADER','FOOTER','MAIN','ASIDE','FORM','FIELDSET','LEGEND','DL','DT','DD','BR','NAV','DIALOG','LABEL','BLOCKQUOTE','PRE']);
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','svg']);
  const out = [];
  const walk = node => {
    if (node.nodeType === 3) { out.push(node.nodeValue); return; }
    if (node.nodeType !== 1) return;
    const el = node;
    if (SKIP.has(el.tagName)) return;
    if (el.getAttribute('aria-hidden') === 'true') return;
    if (el.hasAttribute('hidden') && !el.matches('aside,[role=status],[role=alert]')) return;
    const st = el.getAttribute('style') || '';
    if (/display\s*:\s*none\s*!important|visibility\s*:\s*hidden/i.test(st)) return;
    if (el.tagName === 'DIALOG' && !el.open) return;
    if (el.tagName === 'CANVAS' || el.tagName === 'IMG' || (el.getAttribute('role') === 'img' && !el.children.length)) {
      const label = el.getAttribute('aria-label') || el.getAttribute('alt') || el.textContent || '';
      out.push(' ' + label.replace(/[​-‍﻿]/g, '') + ' ');
      return;
    }
    if (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA' || el.tagName === 'BUTTON') {
      const t = (el.innerText || el.value || '').trim();
      if (el.tagName === 'BUTTON' && t) out.push(' ' + t + ' ');
      return;
    }
    if (el.tagName === 'TD' || el.tagName === 'TH') out.push(' | ');
    const block = BLOCK.has(el.tagName);
    if (block) out.push('\n');
    if (el.getAttribute('role') === 'tooltip') out.push(' [');
    for (const c of el.childNodes) walk(c);
    if (el.getAttribute('role') === 'tooltip') out.push('] ');
    if (el.tagName === 'IFRAME') out.push(' [iframe ' + el.src + '] ');
    if (block) out.push('\n');
  };
  walk(root || document.body);
  return out.join('').replace(/[​-‍﻿]/g, '').replace(/[ \t ]+/g, ' ').replace(/ *\n[ \n]*/g, '\n').trim();
}"""

SELECTOR = "a,button,summary,input:not([type=hidden]),textarea,select,[role=button],[role=link],[role=checkbox],[role=tab],[tabindex],[contenteditable=true]"


def _plain_html(data: bytes) -> str:
    source = data.decode("utf-8", errors="replace")
    source = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", source)
    source = re.sub(r"(?i)</?(?:p|div|h[1-6]|tr|li|br|table|section)\b[^>]*>", "\n", source)
    return html.unescape(re.sub(r"<[^>]+>", " ", source))


def _document_text(data: bytes, name: str, depth: int = 0) -> str:
    """Extract text from one model-selected document, including ZIP members."""
    suffix = name.lower().split("?", 1)[0]
    if data.startswith(b"%PDF") or suffix.endswith(".pdf"):
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:80])
    if zipfile.is_zipfile(io.BytesIO(data)) and depth < 2:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if suffix.endswith(".docx"):
                parts = [part for part in archive.namelist() if part.startswith("word/") and part.endswith(".xml")]
                return "\n".join(_plain_html(archive.read(part)) for part in parts[:10])
            pieces = []
            for part in archive.namelist()[:40]:
                if part.endswith("/"):
                    continue
                info = archive.getinfo(part)
                if info.file_size > 5_000_000:
                    pieces.append(f"[{part}: too large to extract]")
                    continue
                try:
                    pieces.append(f"\n===== {part} =====\n{_document_text(archive.read(part), part, depth + 1)}")
                except Exception as error:
                    pieces.append(f"[{part}: {type(error).__name__}: {error}]")
            return "".join(pieces)
    if suffix.endswith((".html", ".htm", ".xml")) or b"<html" in data[:500].lower():
        return _plain_html(data)
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return f"[Binary document: {len(data)} bytes]"


def describe(d: dict) -> str:
    """One compact line per control: id, kind, name, state. Costs far fewer tokens than JSON objects."""
    kind = d["tag"] + (f"[{d['type']}]" if d["tag"] == "input" and d.get("type") else "")
    name = d.get("label") or d.get("aria") or d.get("text") or d.get("placeholder") or d.get("title") or ""
    parts = [f"{d['id']} {kind}"]
    if name:
        parts.append(json.dumps(re.sub(r"\s+", " ", name)[:80], ensure_ascii=False))
    if d.get("text") and d.get("text") != name and d["tag"] not in ("select",):
        parts.append("text=" + json.dumps(re.sub(r"\s+", " ", d["text"])[:60], ensure_ascii=False))
    if d.get("value") and d["tag"] in ("input", "textarea", "select") and d.get("type") not in ("checkbox", "radio"):
        parts.append("value=" + json.dumps(d["value"][:60], ensure_ascii=False))
    if d.get("type") in ("checkbox", "radio"):
        parts.append("checked" if d.get("checked") else "unchecked")
    if d.get("type") == "range":
        parts.append(f"range {d.get('min')}..{d.get('max')}")
    if d.get("href") and not d["href"].startswith("javascript"):
        parts.append("href=" + d["href"][:120])
    if d.get("options"):
        texts = [o["text"].strip() for o in d["options"] if o.get("text", "").strip()]
        parts.append("options=" + "|".join(texts[:30]) + ("|..." if len(texts) > 30 else ""))
    for flag in ("disabled", "readonly"):
        if d.get(flag):
            parts.append(flag)
    if d.get("frame"):
        parts.append(f"frame={d['frame']}")
    return " ".join(parts)


class BrowserTools:
    def __init__(self, task: dict, pages: dict):
        self.sites = {site["siteId"]: site for site in task["access"]}
        self.allowed_hosts = {urlparse(site["url"]).hostname for site in task["access"]}
        self.pages = pages
        self.controls = {site: [] for site in pages}
        self.observed_urls = {site: {item["url"]} for site, item in self.sites.items()}
        self.documents = {}
        self.dialog_pages = set()
        self.history = {site: [page] for site, page in pages.items()}
        for page in pages.values():
            self._handle_dialogs(page)

    def _handle_dialogs(self, page):
        # The model chooses the submit button; accepting its browser confirmation
        # is the final part of that same click. One handler per tab across helpers.
        if page not in self.dialog_pages and not getattr(page, "_rfq_dialogs", False):
            self.dialog_pages.add(page)
            try:
                page._rfq_dialogs = True
            except Exception:
                pass

            async def accept(dialog):
                try:
                    await dialog.accept()
                except Exception:
                    pass
            page.on("dialog", lambda dialog: asyncio.create_task(accept(dialog)))

    def _page(self, site: str):
        if site not in self.pages:
            raise ValueError(f"Unknown site {site!r}; choose one of {list(self.pages)}")
        page = self.pages[site]
        if getattr(page, "is_closed", lambda: False)() is True:
            # A popup (e.g. a search help window) closed itself after a selection:
            # continue in the most recent tab this worker used that is still open.
            for previous in reversed(self.history.get(site, [])):
                if previous.is_closed() is False:
                    self.pages[site] = previous
                    return previous
        return page

    def _use(self, site: str, page):
        self.pages[site] = page
        self._handle_dialogs(page)
        history = self.history.setdefault(site, [])
        if page in history:
            history.remove(page)
        history.append(page)

    def _destination(self, site: str, url: str) -> str:
        page = self._page(site)
        root = self.sites[site]["url"]
        destination = urljoin(page.url if page.url != "about:blank" else root, url or root)
        target = urlparse(destination)
        if target.scheme not in ("http", "https") or target.hostname not in self.allowed_hosts:
            raise ValueError("Choose a URL on one of the task's websites")
        known_paths = {(urlparse(item).hostname, urlparse(item).path.rstrip("/"))
                       for item in self.observed_urls[site] | {page.url, root}}
        if (target.hostname, target.path.rstrip("/")) not in known_paths:
            raise ValueError("This URL path has not appeared in a page observation. Choose a visible link or submit the site's search form; query parameters on an observed path may be changed.")
        return destination

    async def _goto(self, site: str, destination: str):
        page = self._page(site)
        try:
            await page.goto(destination, wait_until="domcontentloaded", timeout=20000)
        except Exception as error:
            if "Timeout" not in type(error).__name__ + str(error):
                raise
            # A hung tab (stuck navigation or blocked script): retry once in a fresh tab of the same session.
            fresh = await page.context.new_page()
            self._use(site, fresh)
            await fresh.goto(destination, wait_until="domcontentloaded", timeout=20000)

    async def navigate(self, site: str, url: str = "") -> dict:
        await self._goto(site, self._destination(site, url))
        return await self.inspect(site)

    async def read_pages(self, site: str, urls: list[str], max_chars: int = 6000) -> dict:
        """Open several model-selected pages (observed paths, chosen query parameters) and extract each."""
        results = []
        for url in urls[:8]:
            try:
                destination = self._destination(site, url)
                await self._goto(site, destination)
                await self._page(site).wait_for_timeout(200)
                text = await self.page_text(site)
                self.observed_urls[site].add(self._page(site).url)
                results.append({"url": self._page(site).url, "text": text[:max(1000, min(max_chars, 12000))]})
            except Exception as error:
                results.append({"url": url, "error": f"{type(error).__name__}: {str(error)[:200]}"})
        observation = await self.inspect(site, max_chars=1000)
        return {"pages": results, "current_controls": observation["controls"],
                "document_urls": observation["document_urls"]}

    async def login(self, site: str) -> dict:
        page = self._page(site)
        if page.url == "about:blank":
            await page.goto(self.sites[site]["url"], wait_until="domcontentloaded", timeout=25000)
        credentials = self.sites[site]["credentials"]
        password = page.locator('input[type="password"]').first
        if not await password.count():
            return {"error": "No password field on this page", "page": await self.inspect(site)}
        form = password.locator("xpath=ancestor::form[1]")
        username = form.locator('input[type="text"],input[type="email"],input[name*="user" i],input[name*="mail" i]').first
        if not await username.count():
            username = page.get_by_label(re.compile("Benutzername|Username|E-Mail|Email|User", re.I)).first
        await username.fill(credentials["username"], timeout=10000)
        await password.fill(credentials["password"], timeout=10000)
        submit = form.locator('button[type="submit"],input[type="submit"]').first
        if not await submit.count():
            submit = page.get_by_role("button", name=re.compile("Anmelden|Einloggen|Login|Sign in", re.I)).first
        await submit.click(timeout=10000)
        await page.wait_for_timeout(400)
        return await self.inspect(site)

    async def fill_credential(self, site: str, element: int, field: str) -> dict:
        """Fill a model-selected login field, including forms with multiple steps."""
        if field not in ("username", "password"):
            raise ValueError("Choose username or password from this site's run credentials")
        control = self._control(site, element)
        target = await control.evaluate("e => ({url:e.ownerDocument.location.href,tag:e.tagName.toLowerCase(),type:e.type})")
        expected = urlparse(self.sites[site]["url"])
        actual = urlparse(target["url"])
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise ValueError("Use this site's credentials only on its configured website")
        if target["tag"] != "input" or target["type"] not in ("text", "email", "password", "tel"):
            raise ValueError("Choose an editable login input from the current observation")
        await self._fill_control(control, self.sites[site]["credentials"][field])
        return await self.inspect(site)

    async def inspect(self, site: str, offset: int = 0, max_chars: int = 10000) -> dict:
        page = self._page(site)
        for control, _ in self.controls[site]:
            await control.dispose()
        self.controls[site] = []
        max_chars = max(1000, min(max_chars, 24000))
        offset = max(0, offset)
        text_parts = []
        controls = []
        urls = self.observed_urls[site]
        urls.add(page.url)
        for frame_number, frame in enumerate(page.frames):
            try:
                body = await asyncio.wait_for(frame.evaluate(f"({READABLE_TEXT})(document.body)"), 5)
                if body.strip():
                    text_parts.append(f"[frame {frame_number}: {frame.url}]\n{body}")
                locator = frame.locator(SELECTOR)
                handles = await asyncio.wait_for(locator.element_handles(), 8)
                # Bind ids to the observed nodes. A live nth() locator can point
                # at a different button if a banner is inserted before a click.
                descriptors = await asyncio.wait_for(frame.evaluate("""els => els.map((e, i) => ({
                    i, visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
                        && e.isConnected
                        && getComputedStyle(e).visibility !== 'hidden'
                        && !e.closest('[hidden],[inert]'),
                    tag: e.tagName.toLowerCase(), type: e.getAttribute('type') || '',
                    text: (e.innerText || '').trim().slice(0, 110),
                    label: (e.labels && e.labels.length ? e.labels[0].innerText : '').trim().slice(0, 100),
                    aria: e.getAttribute('aria-label') || '', placeholder: e.getAttribute('placeholder') || '',
                    title: e.getAttribute('title') || '', href: e.getAttribute('href') || '',
                    value: e.type === 'password' ? '' : (e.value || '').slice(0, 100),
                    checked: !!e.checked,
                    disabled: !!e.disabled, readonly:!!e.readOnly,
                    min:e.getAttribute('min'), max:e.getAttribute('max'),
                    options: e.tagName === 'SELECT' ? Array.from(e.options).map(o => ({value:o.value, text:o.text})).slice(0,60) : undefined
                }))""", handles), 8)
                for descriptor in descriptors:
                    if not descriptor["visible"] or len(controls) >= 180:
                        await handles[descriptor["i"]].dispose()
                        continue
                    descriptor["id"] = len(controls)
                    descriptor["frame"] = frame_number
                    controls.append((handles[descriptor["i"]], descriptor))
                    if descriptor["href"]:
                        urls.add(urljoin(frame.url, descriptor["href"]))
                for resource in await frame.locator("iframe[src],object[data],embed[src]").evaluate_all(
                    "els => els.map(e => e.getAttribute('src') || e.getAttribute('data')).filter(Boolean)"
                ):
                    urls.add(urljoin(frame.url, resource))
            except Exception as error:
                text_parts.append(f"[frame {frame_number}: unable to inspect: {type(error).__name__}]")
        self.controls[site] = controls
        full_text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", "\n\n".join(text_parts))
        tabs = [{"index": i, "url": tab.url} for i, tab in enumerate(page.context.pages)]
        compact_controls = [describe(descriptor) for _, descriptor in controls]
        document_urls = [url for url in urls if re.search(r"\.(pdf|zip|docx?|xlsx?|csv|txt|html?)(?:\?|$)", url, re.I)
                         or url in [frame.url for frame in page.frames[1:]]]
        return {
            "site": site, "url": page.url, "title": await asyncio.wait_for(page.title(), 5), "tabs": tabs,
            "text": full_text[offset:offset + max_chars], "text_length": len(full_text), "offset": offset,
            "controls": compact_controls,
            "document_urls": sorted(document_urls)[:40],
            "visual_regions": await asyncio.wait_for(page.locator("canvas,img,svg").evaluate_all("""els => els
                .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
                .slice(0,20).map(e => ({tag:e.tagName.toLowerCase(),label:e.getAttribute('aria-label')||e.getAttribute('alt')||'',
                    x:Math.round(e.getBoundingClientRect().x),y:Math.round(e.getBoundingClientRect().y),
                    width:Math.round(e.getBoundingClientRect().width),height:Math.round(e.getBoundingClientRect().height)}))"""), 5),
        }

    def _control(self, site: str, element: int):
        controls = self.controls[site]
        if not isinstance(element, int) or element < 0 or element >= len(controls):
            available = [(d['id'], d.get('text') or d.get('label') or d.get('placeholder')) for _, d in controls]
            raise ValueError(f"Unknown control id {element!r}; current controls: {available[:20]}")
        return controls[element][0]

    async def click(self, site: str, element: int) -> dict:
        page = self._page(site)
        old_tabs = set(page.context.pages)
        control = self._control(site, element)
        try:
            await control.click(timeout=5000)
        except Exception as error:
            # A popup covering the page: close it with its own close button, then retry once.
            if "intercepts pointer events" not in str(error) or not await self.dismiss_blocking_dialogs(site):
                raise
            await control.click(timeout=6000)
        try:
            await page.wait_for_timeout(350)
        except Exception:
            pass  # the clicked page closed itself
        new_tabs = [tab for tab in page.context.pages if tab not in old_tabs]
        if new_tabs:
            self._use(site, new_tabs[-1])
        return await self.inspect(site)

    async def fill(self, site: str, element: int, value: str) -> dict:
        control = self._control(site, element)
        await self._fill_control(control, value)
        return await self.inspect(site)

    @staticmethod
    async def _fill_control(control, value):
        # Native range inputs are keyboard-operable, but Playwright.fill excludes
        # them. Use their rendered bounds, then normal keyboard input.
        if await control.get_attribute("readonly") is not None:
            raise ValueError("This control is read-only. Choose an editable form field or navigate using an observed URL.")
        if await control.get_attribute("type") == "range":
            limits = await control.evaluate("e => ({min:Number(e.min||0),max:Number(e.max||100),step:Number(e.step||1)})")
            target = float(value)
            if not limits["min"] <= target <= limits["max"]:
                raise ValueError(f"Slider value must be between {limits['min']} and {limits['max']}")
            if target == limits["max"]:
                await control.press("End")
            else:
                await control.press("Home")
                for _ in range(min(1000, round((target - limits["min"]) / limits["step"]))):
                    await control.press("ArrowRight")
        else:
            await control.fill(str(value), timeout=10000)

    async def fill_many(self, site: str, entries: list[dict]) -> dict:
        results = []
        for entry in entries[:50]:
            if not any(key in entry for key in ("value", "select", "check")):
                raise ValueError("Provide value, select, or check for each control")
            element = entry["element"]
            control = self._control(site, element)
            if await self.dismiss_blocking_dialogs(site):
                pass  # an open popup would intercept the input
            if "select" in entry:
                await control.select_option(label=str(entry["select"]), timeout=10000)
            elif entry.get("check") is not None:
                await control.set_checked(bool(entry["check"]), timeout=10000)
            else:
                await self._fill_control(control, entry.get("value", ""))
            results.append(element)
        return {"ok": True, "filled": results, "page": await self.inspect(site)}

    async def press(self, site: str, key: str, element: int | None = None) -> dict:
        if element is None:
            await self._page(site).keyboard.press(key)
        else:
            await self._control(site, element).press(key, timeout=10000)
        return await self.inspect(site)

    async def scroll(self, site: str, pixels: int = 600) -> dict:
        await self._page(site).mouse.wheel(0, max(-3000, min(pixels, 3000)))
        await self._page(site).wait_for_timeout(250)
        return await self.inspect(site)

    async def dismiss_blocking_dialogs(self, site: str) -> list[str]:
        """Close open modal popups through their own close button (never their accept/navigate links)."""
        closed = []
        for frame in self._page(site).frames:
            try:
                closed += await frame.evaluate("""() => {
                    const names = /^(×|x|✕|schließen|schliessen|close|ausblenden|später|later|abbrechen)$/i;
                    const out = [];
                    for (const d of document.querySelectorAll('dialog[open],[role=dialog],[aria-modal=true]')) {
                        if (!(d.offsetWidth || d.offsetHeight)) continue;
                        const button = [...d.querySelectorAll('button,[role=button]')].find(b =>
                            b.hasAttribute('data-close') || names.test((b.getAttribute('aria-label') || '').trim())
                            || names.test((b.innerText || '').trim()));
                        if (button) { button.click(); out.push(d.getAttribute('aria-label') || d.id || 'dialog'); }
                    }
                    return out;
                }""")
            except Exception:
                pass
        if closed:
            await self._page(site).wait_for_timeout(150)
        return closed

    async def _locate(self, site: str, kind: str, name: str, timeout: float = 8.0):
        """A live control on the current page by its visible label or name, waiting for it to be enabled."""
        page = self._page(site)
        deadline = asyncio.get_running_loop().time() + timeout
        pattern = re.compile(r"^\s*" + re.escape(name.strip()) + r"\s*$", re.I)
        while True:
            for frame in page.frames:
                if kind == "click":
                    candidates = [frame.get_by_role(role, name=pattern) for role in ("button", "link", "tab", "menuitem")]
                    candidates += [frame.get_by_role(role, name=name) for role in ("button", "link")]
                    candidates.append(frame.get_by_text(pattern))
                elif kind == "open":
                    candidates = [frame.locator("summary").filter(has_text=name)]
                else:
                    candidates = [frame.get_by_label(pattern), frame.get_by_label(name),
                                  frame.get_by_placeholder(name), frame.get_by_role("textbox", name=name)]
                for locator in candidates:
                    try:
                        count = await locator.count()
                    except Exception:
                        continue
                    for index in range(min(count, 6)):
                        item = locator.nth(index)
                        try:
                            if await item.is_visible() and (kind == "open" or await item.is_enabled()):
                                return item
                        except Exception:
                            continue
            if asyncio.get_running_loop().time() > deadline:
                raise ValueError(f"No visible enabled control named {name!r} for {kind}")
            await page.wait_for_timeout(250)

    async def form_sequence(self, site: str, steps: list[dict]) -> dict:
        """Run a model-chosen sequence of form steps, re-finding each control by its label after every
        page update. Steps: {field, select} | {field, value} | {field, check} | {click} | {open}."""
        page = self._page(site)
        done = []
        for number, step in enumerate(steps[:60]):
            try:
                await self.dismiss_blocking_dialogs(site)
                if "click" in step:
                    control = await self._locate(site, "click", str(step["click"]))
                    old_tabs = set(page.context.pages)
                    await control.click(timeout=8000)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(250)
                    new_tabs = [tab for tab in page.context.pages if tab not in old_tabs]
                    if new_tabs:
                        self._use(site, new_tabs[-1])
                        page = new_tabs[-1]
                elif "open" in step:
                    summary = await self._locate(site, "open", str(step["open"]))
                    if not await summary.evaluate("s => s.parentElement && s.parentElement.open"):
                        await summary.click(timeout=5000)
                elif "select" in step:
                    control = await self._locate(site, "field", str(step["field"]))
                    wanted = str(step["select"]).strip()
                    value = await control.evaluate("""(select, wanted) => {
                        const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        const options = [...(select.options || [])];
                        const w = norm(wanted);
                        const hit = options.find(o => norm(o.text) === w) || options.find(o => norm(o.value) === w)
                            || options.find(o => norm(o.text).replace(/[^0-9a-z,.-]/g, '') === w.replace(/[^0-9a-z,.-]/g, ''))
                            || options.find(o => norm(o.text).startsWith(w + ' '));
                        return hit ? hit.value : null;
                    }""", wanted)
                    if value is None:
                        options = await control.evaluate("s => [...(s.options||[])].map(o => o.text).slice(0, 60)")
                        raise ValueError(f"Option {wanted!r} not offered for {step['field']!r}; options: {options}")
                    await control.select_option(value=value, timeout=8000)
                elif "check" in step:
                    control = await self._locate(site, "field", str(step["field"]))
                    await control.set_checked(bool(step["check"]), timeout=8000)
                elif "value" in step:
                    control = await self._locate(site, "field", str(step["field"]))
                    await self._fill_control(control, step["value"])
                else:
                    raise ValueError("Each step needs click, open, or field with select/value/check")
                done.append(number)
            except Exception as error:
                observation = await self.inspect(site, max_chars=6000)
                return {"ok": False, "completed_steps": done, "failed_step": number, "step": step,
                        "error": f"{type(error).__name__}: {str(error)[:400]}", "page": observation}
        return {"ok": True, "completed_steps": done, "page": await self.inspect(site, max_chars=8000)}

    async def page_text(self, site: str) -> str:
        parts = []
        for frame in self._page(site).frames:
            try:
                parts.append(await asyncio.wait_for(frame.evaluate(f"({READABLE_TEXT})(document.body)"), 5))
            except Exception:
                pass
        return "\n\n".join(part for part in parts if part)

    async def paginate(self, site: str, element: int, max_pages: int = 8) -> dict:
        """Follow a model-selected next-page or load-more control through one result set."""
        page = self._page(site)
        descriptor = self.controls[site][element][1] if 0 <= element < len(self.controls[site]) else None
        if descriptor is None:
            self._control(site, element)
        signature = lambda d: (d["tag"], d.get("text"), d.get("aria"), d.get("frame"))
        wanted = signature(descriptor)
        texts = [await self.page_text(site)]
        for _ in range(max(1, min(max_pages, 12))):
            await self._control(site, element).click(timeout=12000)
            await page.wait_for_timeout(600)
            await self.inspect(site, max_chars=1000)
            text = await self.page_text(site)
            if text == texts[-1]:
                break
            previous = set(texts[-1].splitlines())
            if len(previous & set(text.splitlines())) >= 0.9 * len(previous):
                texts[-1] = text  # rows were appended to the same view
            else:
                texts.append(text)
            matches = [d["id"] for _, d in self.controls[site] if signature(d) == wanted]
            if not matches:
                break
            element = matches[0]
        # Drop page chrome repeated on every page, keeping it once.
        common = set(texts[0].splitlines())
        for text in texts[1:]:
            common &= set(text.splitlines())
        combined = [texts[0]] + ["\n".join(line for line in text.splitlines() if line not in common)
                                 for text in texts[1:]]
        joined = "\n\n----- next page -----\n".join(combined)
        observation = await self.inspect(site, max_chars=1000)
        return {"pages": len(texts), "text": joined[:60000], "url": page.url,
                "controls": observation["controls"], "document_urls": observation["document_urls"]}

    async def click_at(self, site: str, x: int, y: int) -> dict:
        await self._page(site).mouse.click(x, y)
        await self._page(site).wait_for_timeout(350)
        return await self.inspect(site)

    async def switch_tab(self, site: str, index: int) -> dict:
        tabs = self._page(site).context.pages
        if index < 0 or index >= len(tabs):
            raise ValueError(f"Tab index must be 0 to {len(tabs)-1}")
        self._use(site, tabs[index])
        return await self.inspect(site)

    async def read_document(self, site: str, url: str, offset: int = 0, max_chars: int = 24000) -> dict:
        page = self._page(site)
        address = urljoin(page.url, url)
        if address not in self.observed_urls[site] and address not in [frame.url for frame in page.frames]:
            raise ValueError("The model must first observe this link or frame on the selected page")
        if address not in self.documents:
            # The supplied browser owns the sandbox's website proxy and session.
            # Fetch through that browser rather than the agent's network stack.
            response = await asyncio.wait_for(page.evaluate("""async address => {
                const r = await fetch(address, {credentials:'include', signal:AbortSignal.timeout(25000)});
                if (!r.ok) return {status:r.status};
                const bytes = new Uint8Array(await r.arrayBuffer());
                if (bytes.length > 25000000) throw new Error('Document exceeds 25 MB');
                let binary='';
                for (let i=0;i<bytes.length;i+=32768)
                    binary += String.fromCharCode(...bytes.subarray(i,i+32768));
                return {status:r.status,url:r.url,type:r.headers.get('content-type')||'',data:btoa(binary)};
            }""", address), timeout=30)
            if not 200 <= response["status"] < 300:
                raise ValueError(f"Document request returned HTTP {response['status']}")
            data = base64.b64decode(response["data"])
            if len(data) > 25_000_000:
                raise ValueError("Document exceeds 25 MB")
            # HTML document viewers need browser interaction. Never cache a login
            # or error page as if it were the document's contents.
            if "text/html" in response["type"]:
                return {"url": address, "response_url": response["url"],
                        "text": _plain_html(data)[:24000],
                        "hint": "This is an HTML page. Navigate to this URL to inspect controls or sign in again; then open the actual document from the viewer."}
            self.documents[address] = _document_text(data, address)
        content = self.documents[address]
        return {"url": address, "length": len(content), "offset": offset,
                "text": content[max(0, offset):max(0, offset) + min(max_chars, 50000)]}

    async def screenshot(self, site: str) -> str:
        page = self._page(site)
        await page.bring_to_front()
        try:
            image = await page.screenshot(type="jpeg", quality=65, timeout=8000)
            encoded = base64.b64encode(image).decode()
        except Exception:
            # Some CDP browser builds stall the high-level screenshot call on
            # background tabs. Capture the compositor surface directly instead.
            session = await page.context.new_cdp_session(page)
            try:
                result = await asyncio.wait_for(session.send("Page.captureScreenshot", {
                    "format": "jpeg", "quality": 65, "fromSurface": True,
                    "captureBeyondViewport": False}), timeout=8)
                encoded = result["data"]
            finally:
                await session.detach()
        return "data:image/jpeg;base64," + encoded


def calculate(expression: str) -> str:
    """Decimal arithmetic for model-selected values; no Python code execution."""
    import ast

    if len(expression) > 5000:
        raise ValueError("Expression is too long")

    def evaluate(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
            return Decimal(str(node.value))
        if isinstance(node, ast.List):
            return [evaluate(item) for item in node.elts]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            args = [evaluate(arg) for arg in node.args]
            if node.func.id == "sum" and len(args) == 1 and isinstance(args[0], list):
                return sum(args[0], Decimal(0))
            if node.func.id == "min":
                return min(args)
            if node.func.id == "max":
                return max(args)
            if node.func.id == "ceil" and len(args) == 1:
                return Decimal(math.ceil(args[0]))
            if node.func.id == "round2" and len(args) == 1:
                return args[0].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        raise ValueError("Allowed: numbers, + - * /, lists, sum, min, max, ceil, round2")

    return str(evaluate(ast.parse(expression, mode="eval").body))
