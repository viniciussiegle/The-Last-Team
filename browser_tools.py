"""Model-directed browser actions for the challenge's live websites.

Each observation covers only a page the model chose to open. Document extraction
accepts only links or frames observed on that page; it never crawls a site.
"""

import asyncio
import base64
import html
import io
import math
import re
import zipfile
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urljoin, urlparse

from pypdf import PdfReader


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


class BrowserTools:
    def __init__(self, task: dict, pages: dict):
        self.sites = {site["siteId"]: site for site in task["access"]}
        self.allowed_hosts = {urlparse(site["url"]).hostname for site in task["access"]}
        self.pages = pages
        self.controls = {site: [] for site in pages}
        self.observed_urls = {site: {item["url"]} for site, item in self.sites.items()}
        self.documents = {}
        self.dialog_pages = set()
        for page in pages.values():
            self._handle_dialogs(page)

    def _handle_dialogs(self, page):
        # The model chooses the submit button; accepting its browser confirmation
        # is the final part of that same click.
        if page not in self.dialog_pages:
            self.dialog_pages.add(page)
            page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))

    def _page(self, site: str):
        if site not in self.pages:
            raise ValueError(f"Unknown site {site!r}; choose one of {list(self.pages)}")
        return self.pages[site]

    async def navigate(self, site: str, url: str = "") -> dict:
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
        await page.goto(destination, wait_until="domcontentloaded", timeout=25000)
        return await self.inspect(site)

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
                body = await frame.locator("body").inner_text(timeout=3000)
                if body.strip():
                    text_parts.append(f"[frame {frame_number}: {frame.url}]\n{body}")
                locator = frame.locator(SELECTOR)
                handles = await locator.element_handles()
                # Bind ids to the observed nodes. A live nth() locator can point
                # at a different button if a banner is inserted before a click.
                descriptors = await frame.evaluate("""els => els.map((e, i) => ({
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
                    options: e.tagName === 'SELECT' ? Array.from(e.options).map(o => ({value:o.value, text:o.text})).slice(0,25) : undefined
                }))""", handles)
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
        compact_controls = [{key: value for key, value in descriptor.items()
                             if key not in ("i", "visible") and value is not None
                             and not (key in ("disabled", "readonly") and value is False)
                             and not (key == "checked" and descriptor.get("type") not in ("checkbox", "radio"))
                             and value != "" and value != []}
                            for _, descriptor in controls]
        document_urls = [url for url in urls if re.search(r"\.(pdf|zip|docx?|xlsx?|csv|txt|html?)(?:\?|$)", url, re.I)
                         or url in [frame.url for frame in page.frames[1:]]]
        return {
            "site": site, "url": page.url, "title": await page.title(), "tabs": tabs,
            "text": full_text[offset:offset + max_chars], "text_length": len(full_text), "offset": offset,
            "controls": compact_controls,
            "document_urls": sorted(document_urls)[:40],
            "visual_regions": await page.locator("canvas,img,svg").evaluate_all("""els => els
                .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
                .slice(0,20).map(e => ({tag:e.tagName.toLowerCase(),label:e.getAttribute('aria-label')||e.getAttribute('alt')||'',
                    x:Math.round(e.getBoundingClientRect().x),y:Math.round(e.getBoundingClientRect().y),
                    width:Math.round(e.getBoundingClientRect().width),height:Math.round(e.getBoundingClientRect().height)}))"""),
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
        await self._control(site, element).click(timeout=12000)
        await page.wait_for_timeout(350)
        new_tabs = [tab for tab in page.context.pages if tab not in old_tabs]
        if new_tabs:
            self.pages[site] = new_tabs[-1]
            self._handle_dialogs(new_tabs[-1])
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

    async def click_at(self, site: str, x: int, y: int) -> dict:
        await self._page(site).mouse.click(x, y)
        await self._page(site).wait_for_timeout(350)
        return await self.inspect(site)

    async def switch_tab(self, site: str, index: int) -> dict:
        tabs = self._page(site).context.pages
        if index < 0 or index >= len(tabs):
            raise ValueError(f"Tab index must be 0 to {len(tabs)-1}")
        self.pages[site] = tabs[index]
        self._handle_dialogs(tabs[index])
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
