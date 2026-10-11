"""The browser layer: what a model sees of a page and the actions it can take there.

`Tab.observe()` renders every frame of the page as compact text in which each control appears inline as
`[id:kind label …]`, so a model can refer to controls by id. Actions are executed for a model's decision only;
helpers here never decide where to go: they sign in with the run's credentials, retry flaky pages, read pages a
model selected and follow a pagination control a model chose.
"""
import asyncio
import hashlib
import io
import re
import time
import zipfile
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

OBSERVE_JS = r"""(start) => {
  const BLOCK = new Set(['ADDRESS','ARTICLE','ASIDE','BLOCKQUOTE','BR','DD','DETAILS','DIALOG','DIV','DL','DT','FIELDSET',
    'FIGCAPTION','FIGURE','FOOTER','FORM','H1','H2','H3','H4','H5','H6','HEADER','HR','LI','MAIN','NAV','OL','P','PRE',
    'SECTION','SUMMARY','TABLE','TBODY','THEAD','TFOOT','TR','UL','CAPTION','LEGEND']);
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','HEAD','META','LINK','SVG']);
  const ROLES = new Set(['button','link','tab','menuitem','checkbox','radio','option','switch','combobox','textbox',
    'slider','spinbutton','treeitem','searchbox','menuitemcheckbox','menuitemradio']);
  const ZW = /[​-‏⁠﻿­]/g;
  let id = start, cur = '', row = 0, password = false;
  const lines = [], controls = [];
  const clean = (t) => (t || '').replace(ZW, '').replace(/\s+/g, ' ').trim();
  const flush = () => { if (row) { cur += ' '; return; } const t = cur.replace(/[ \t]+/g, ' ').trim(); if (t) lines.push(t); cur = ''; };
  const visible = (el, st) => {
    if (st.display === 'none' || st.visibility === 'hidden' || st.visibility === 'collapse') return false;
    if (parseFloat(st.opacity) === 0 && el.tagName !== 'INPUT') return false;
    const r = el.getBoundingClientRect();
    return !(r.width === 0 && r.height === 0 && st.overflow !== 'visible' && el.children.length === 0);
  };
  const interactive = (el, st) => {
    const tag = el.tagName;
    if (tag === 'A') return el.hasAttribute('href') || el.hasAttribute('onclick');
    if (['BUTTON', 'SELECT', 'TEXTAREA', 'SUMMARY'].includes(tag)) return true;
    if (tag === 'INPUT') return el.type !== 'hidden';
    const role = el.getAttribute('role');
    if (role && ROLES.has(role)) return true;
    if (el.hasAttribute('onclick') || el.getAttribute('contenteditable') === 'true') return true;
    return st.cursor === 'pointer' && (!el.parentElement || getComputedStyle(el.parentElement).cursor !== 'pointer')
      && (el.innerText || '').length < 100 && !el.querySelector('a[href],button,input,select,textarea');
  };
  const pseudo = (el, which) => {  // text a stylesheet draws with content: "…" or attr(…)
    const content = getComputedStyle(el, which).content;
    if (!content || content === 'none' || content === 'normal') return '';
    return content.startsWith('"') ? content.slice(1, -1).replace(/\\"/g, '"') : '';
  };
  const ownText = (label) => {  // a <label> wrapping its control: only the label's own words
    const copy = label.cloneNode(true);
    copy.querySelectorAll('select,input,textarea,option,style,script').forEach(n => n.remove());
    return copy.textContent + ' ' + pseudo(label, '::before') + ' ' + pseudo(label, '::after');
  };
  const labelOf = (el) => {
    const tag = el.tagName;
    let t = el.getAttribute('aria-label') || '';
    if (!t && el.labels && el.labels.length) t = Array.from(el.labels).map(ownText).join(' ');
    if (!t && el.getAttribute('aria-labelledby'))
      t = el.getAttribute('aria-labelledby').split(/\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' ');
    if (!['INPUT', 'SELECT', 'TEXTAREA'].includes(tag)) t = t || el.innerText || '';
    if (!t) t = el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || el.getAttribute('alt') || '';
    if (!t && tag === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type)) t = el.value;
    if (!t && el.querySelector) { const img = el.querySelector('img[alt]'); if (img) t = img.alt; }
    return clean(t).slice(0, 80);
  };
  const control = (el) => {
    const tag = el.tagName, role = el.getAttribute('role');
    const c = { id: id, kind: tag === 'A' ? 'link' : (role || tag.toLowerCase()), label: labelOf(el), href: '', extra: '' };
    if (tag === 'INPUT') {
      c.kind = (!el.type || el.type === 'text') ? 'input' : el.type;
      if (['checkbox', 'radio'].includes(el.type)) { c.checked = el.checked; c.extra += el.checked ? ' (checked)' : ' (unchecked)'; }
      else if (!['submit', 'button', 'reset', 'file', 'image'].includes(el.type)) {
        c.value = el.value || '';
        if (el.value) c.extra += ` = "${clean(el.value).slice(0, 60)}"`;
        if (el.type === 'range') { c.min = el.min || '0'; c.max = el.max || '100'; c.extra += ` [${c.min}..${c.max}]`; }
      }
      if (el.readOnly) c.readonly = true;
      if (el.placeholder && c.label !== clean(el.placeholder)) c.extra += ` placeholder "${clean(el.placeholder).slice(0, 40)}"`;
    }
    if (tag === 'TEXTAREA') { c.value = el.value || ''; if (el.value) c.extra += ` = "${clean(el.value).slice(0, 60)}"`; }
    if (tag === 'SELECT') {
      c.options = Array.from(el.options).map(o => clean(o.text));
      c.value = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : '';
      const shown = Array.from(el.options).map(o => (o.selected ? '*' : '') + clean(o.text).slice(0, 40));
      c.extra += ' options: ' + shown.slice(0, 40).join(' | ') + (shown.length > 40 ? ` | …(${shown.length})` : '');
    }
    if (['checkbox', 'switch', 'tab', 'option', 'radio'].includes(role)) {
      const s = el.getAttribute('aria-checked') || el.getAttribute('aria-selected');
      if (s) { c.checked = s === 'true'; c.extra += c.checked ? ' (selected)' : ' (unselected)'; }
    }
    if (el.matches(':disabled') || el.closest('[inert]') || el.getAttribute('aria-disabled') === 'true') { c.disabled = true; c.extra += ' (disabled)'; }
    const desc = el.getAttribute('aria-describedby');
    if (desc) {
      const d = clean(desc.split(/\s+/).map(i => (document.getElementById(i) || {}).textContent || '').join(' '));
      if (d) c.extra += ` (info: ${d.slice(0, 120)})`;
    }
    if (el.title && clean(el.title) !== c.label) c.extra += ` (title: ${clean(el.title).slice(0, 80)})`;
    if (tag === 'SUMMARY') c.extra += el.parentElement && el.parentElement.open ? ' (open)' : ' (closed)';
    if (tag === 'A') {
      const href = el.getAttribute('href') || '';
      if (href && href !== '#' && !href.startsWith('javascript')) c.href = el.href;
      if (c.href) c.extra += ' →' + c.href.replace(location.origin, '').slice(0, 90);
    }
    return c;
  };
  const walk = (node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      let t = node.textContent.replace(ZW, '');
      const ws = node.parentElement ? getComputedStyle(node.parentElement).whiteSpace : '';
      if (row || !(ws.startsWith('pre') || ws === 'break-spaces')) t = t.replace(/\s+/g, ' ');
      cur += t; return;
    }
    if (node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) { for (const child of node.childNodes) walk(child); return; }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node;
    if (SKIP.has(el.tagName)) return;
    const st = getComputedStyle(el);
    if (!visible(el, st)) return;
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
      flush(); lines.push(`[frame ${clean(el.getAttribute('title') || el.getAttribute('name') || el.src).slice(0, 80)}]`); return;
    }
    if (el.tagName === 'CANVAS') {
      const label = el.getAttribute('aria-label') || clean(el.textContent);
      cur += ` [canvas${label ? ': ' + clean(label) : ' without text label: use a screenshot'}] `; return;
    }
    if (el.tagName === 'IMG' && el.alt) cur += ` [image ${clean(el.alt).slice(0, 60)}] `;
    if (el.tagName === 'INPUT' && el.type === 'password') password = true;
    const block = BLOCK.has(el.tagName);
    const role = el.getAttribute('role');
    const modal = (el.tagName === 'DIALOG' && el.open) || role === 'dialog' || role === 'alertdialog' || el.getAttribute('aria-modal') === 'true';
    if (interactive(el, st)) {
      const c = control(el);
      el.setAttribute('data-aid', String(id++));
      controls.push(c);
      if (block) flush();
      cur += ` [${c.id}:${c.kind}${c.label ? ' ' + c.label : ''}${c.extra}] `;
      const leaf = ['SUMMARY', 'SELECT', 'TEXTAREA', 'INPUT'].includes(el.tagName)
        || !el.querySelector('a[href],button,input,select,textarea,[role=button],[onclick]');
      if (leaf) { if (block) flush(); return; }
    }
    const before = pseudo(el, '::before'), after = pseudo(el, '::after');
    if (before.trim()) cur += ' ' + before + ' ';
    const cell = el.tagName === 'TD' || el.tagName === 'TH' || role === 'cell' || role === 'gridcell';
    const isRow = el.tagName === 'TR' || role === 'row';
    if (block || modal) flush();
    if (modal) lines.push(`[dialog ${clean(el.getAttribute('aria-label') || '')}]`);
    if (isRow) row++;
    if (el.shadowRoot) walk(el.shadowRoot);
    for (const child of el.childNodes) walk(child);
    if (after.trim()) cur += ' ' + after + ' ';
    if (cell) cur += ' | ';
    if (isRow) row--;
    if (block || modal) flush();
    if (modal) lines.push('[/dialog]');
  };
  document.querySelectorAll('[data-aid]').forEach(e => e.removeAttribute('data-aid'));
  if (document.body) walk(document.body);
  flush();
  const out = [];
  for (const l of lines) { const t = l.replace(/^(\s*\|\s*)+|(\s*\|\s*)+$/g, ''); if (t && out[out.length - 1] !== t) out.push(t); }
  return { text: out.join('\n'), controls, next: id, title: document.title, password };
}"""

OPEN_DETAILS_JS = """e => { for (let d = e.closest('details'); d; d = d.parentElement && d.parentElement.closest('details')) d.open = true;
  e.scrollIntoView({block: 'center'}); }"""


@dataclass
class Control:
    id: int
    kind: str
    label: str
    frame: int
    href: str = ""
    extra: str = ""
    value: str | None = None
    options: list[str] | None = None
    checked: bool | None = None
    disabled: bool = False
    readonly: bool = False
    context: str = ""  # the text line the control sits in, for choosing among look-alike controls

    @property
    def fillable(self) -> bool:
        return not self.disabled and not self.readonly and self.kind in (
            "input", "search", "email", "number", "tel", "password", "textarea", "textbox", "searchbox", "select",
            "range", "checkbox", "radio", "date", "datetime-local", "combobox", "slider", "switch")

    def describe(self) -> str:
        text = f"{self.kind} '{self.label}'{self.extra}"
        return text + (f" (row: {self.context})" if self.context else "")


@dataclass
class Observation:
    url: str
    title: str
    text: str
    controls: dict[int, Control] = field(default_factory=dict)
    password: bool = False

    @property
    def signature(self) -> str:
        body = re.sub(r"\[\d+:", "[", self.text)
        return hashlib.sha1((self.url.split("#")[0] + body).encode()).hexdigest()[:16]

    def clip(self, limit: int) -> str:
        head = f"URL: {self.url}\nTitle: {self.title}\n"
        return head + (self.text if len(self.text) <= limit else self.text[:limit] + "\n[… page text truncated]")


def text_of_document(name: str, data: bytes, limit: int = 20000) -> str:
    """Text of a downloaded document the model chose: ZIP members, text formats and (if available) PDFs."""
    lower = name.lower()
    if lower.endswith(".zip") or data[:2] == b"PK":
        parts = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.namelist():
                if not member.endswith("/"):
                    parts.append(f"--- {member}\n" + text_of_document(member, archive.read(member), limit // 2))
        return "\n".join(parts)[:limit]
    if lower.endswith(".pdf") or data[:4] == b"%PDF":
        try:
            from pypdf import PdfReader  # optional; present only if installed
            return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)[:limit]
        except Exception as error:
            return f"[PDF {name}: no text extracted ({type(error).__name__}); open it in the browser instead]"
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if lower.endswith((".html", ".htm")) or "<html" in text[:500].lower():
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text)[:limit]


class Tab:
    """One website's browser context: the page a worker drives plus helpers for model-chosen actions."""

    def __init__(self, site: dict, context, page, log=print):
        self.site = site
        self.context = context
        self.page = page
        self.log = log
        self.obs: Observation | None = None
        self.events: list[str] = []  # dialogs, downloads and popups since the last observation
        self.host = urlparse(site["url"]).netloc
        self._watch(page)

    # ---------------------------------------------------------------- events
    def _watch(self, page):
        page.on("dialog", self._on_dialog)
        page.on("download", lambda download: asyncio.ensure_future(self._on_download(download)))
        page.on("popup", self._on_popup)

    async def _on_dialog(self, dialog):
        self.events.append(f"browser {dialog.type} dialog '{dialog.message[:200]}' was accepted")
        try:
            await dialog.accept()
        except Exception:
            pass

    async def _on_download(self, download):
        try:
            path = await download.path()
            data = open(path, "rb").read() if path else b""
            self.events.append(f"downloaded {download.suggested_filename}:\n" + text_of_document(download.suggested_filename, data))
        except Exception as error:
            self.events.append(f"download {download.suggested_filename} failed: {error}")

    def _on_popup(self, popup):
        self.events.append("a new window opened and is now the active page")
        self._watch(popup)
        self.page = popup

    # ------------------------------------------------------------ observing
    async def observe(self) -> Observation:
        if self.page.is_closed():
            open_pages = [p for p in self.context.pages if not p.is_closed()]
            self.page = open_pages[-1] if open_pages else await self.context.new_page()
        obs = await self._render(self.page)
        if self.events:
            obs.text = "\n".join(f"[event: {e[:3000]}]" for e in self.events) + "\n" + obs.text
            self.events.clear()
        self.obs = obs
        return obs

    async def _render(self, page) -> Observation:
        parts, controls, password, next_id, title = [], {}, False, 1, ""
        for index, frame in enumerate(page.frames):
            if frame.is_detached() or (index and frame.url in ("", "about:blank")):
                continue
            try:
                result = await asyncio.wait_for(frame.evaluate(OBSERVE_JS, next_id), 8)
            except Exception:
                continue
            next_id = result["next"]
            password = password or result["password"]
            if index == 0:
                title = result["title"]
                parts.append(result["text"])
            elif result["text"].strip():
                parts.append(f"--- frame {frame.url.replace('https://' + self.host, '')}\n{result['text']}")
            for item in result["controls"]:
                controls[item["id"]] = Control(
                    id=item["id"], kind=item["kind"], label=item["label"], frame=index, href=item.get("href", ""),
                    extra=item.get("extra", ""), value=item.get("value"), options=item.get("options"),
                    checked=item.get("checked"), disabled=bool(item.get("disabled")), readonly=bool(item.get("readonly")))
        text = "\n".join(parts)
        for line in text.splitlines():
            ids = [int(m) for m in re.findall(r"\[(\d+):", line)]
            context = re.sub(r"\s+", " ", re.sub(r"\[\d+:[^\]]*\]", " ", line)).strip()[:90]
            for cid in ids:
                if cid in controls:
                    controls[cid].context = context
        return Observation(url=page.url, title=title, text=text, controls=controls, password=password)

    def _locate(self, cid: int):
        control = self.obs.controls.get(cid) if self.obs else None
        if control is None:
            raise ValueError(f"control {cid} is not on the current page; observe again")
        frames = self.page.frames
        frame = frames[control.frame] if control.frame < len(frames) else self.page.main_frame
        return control, frame.locator(f'[data-aid="{cid}"]').first

    # -------------------------------------------------------------- actions
    async def settle(self, before: str | None = None):
        """Wait briefly for the navigation or DOM update an action started."""
        await asyncio.sleep(0.25)
        for _ in range(2):
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=6000)
            except Exception:
                pass
            for frame in self.page.frames[1:]:
                try:
                    await frame.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
            if before is None or self.page.url != before:
                break
            await asyncio.sleep(0.2)
        await self._retry_error_page()

    async def _retry_error_page(self):
        """Flaky server errors: reload a page whose main document came back with a 5xx status, at most twice."""
        for _ in range(2):
            try:
                status = await self.page.evaluate("() => (performance.getEntriesByType('navigation')[0] || {}).responseStatus || 0")
            except Exception:
                return
            if not status or status < 500:
                return
            self.log(f"[{self.site['siteId']}] server error {status}; reloading")
            await asyncio.sleep(0.4)
            try:
                await self.page.reload(wait_until="domcontentloaded", timeout=10000)
            except Exception:
                return

    async def click(self, cid: int) -> str:
        control, locator = self._locate(cid)
        before = self.page.url
        if control.disabled and control.href:  # a locked overlay disables the link, not its target
            await self.goto(control.href)
            return f"opened the target of {control.describe()[:120]}"
        try:
            await locator.evaluate(OPEN_DETAILS_JS, timeout=3000)
            await locator.click(timeout=3000)
        except Exception:
            try:
                await locator.evaluate("e => e.click()", timeout=3000)
            except Exception:
                if not control.href:
                    raise
                await self.goto(control.href)
        await self.settle(before)
        return f"clicked {control.describe()[:120]}"

    async def fill(self, cid: int, value: str) -> str:
        control, locator = self._locate(cid)
        value = str(value)
        if control.kind in ("range", "slider"):
            await locator.evaluate("""(e, v) => { const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                set.call(e, v); e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true})); }""", value,
                timeout=5000)
            return f"set {control.label} to {value}"
        if control.kind == "select":
            return await self.select(cid, value)
        await locator.evaluate(OPEN_DETAILS_JS, timeout=3000)
        await locator.fill(value, timeout=4000)
        return f"filled {control.label!r} with {value!r}"

    async def select(self, cid: int, option: str) -> str:
        control, locator = self._locate(cid)
        options = control.options or []
        chosen = match_option(option, options)
        if chosen is None:
            raise ValueError(f"no option matches {option!r}; options are {options[:40]}")
        before = self.page.url
        await locator.select_option(label=chosen, timeout=4000)
        await self.settle(before)
        return f"selected {chosen!r} in {control.label!r}"

    async def check(self, cid: int, on: bool = True) -> str:
        control, locator = self._locate(cid)
        try:
            await locator.set_checked(on, timeout=3000)
        except Exception:
            if bool(control.checked) != on:
                await locator.evaluate("e => e.click()", timeout=3000)
        return f"{'checked' if on else 'unchecked'} {control.label!r}"

    async def press(self, key: str, cid: int | None = None) -> str:
        before = self.page.url
        if cid is not None:
            await self._locate(cid)[1].press(key, timeout=3000)
        else:
            await self.page.keyboard.press(key)
        await self.settle(before)
        return f"pressed {key}"

    def resolve(self, url: str) -> str:
        target = urljoin(self.page.url if self.page.url.startswith("http") else self.site["url"], url or "/")
        if urlparse(target).netloc != self.host:
            raise ValueError(f"{target} is not on this website")
        return target

    async def goto(self, url: str) -> str:
        target = self.resolve(url)
        await self.page.goto(target, wait_until="domcontentloaded", timeout=15000)
        await self.settle()
        return f"opened {target}"

    async def recover(self) -> str:
        """The page stopped responding: continue in a fresh tab of the same session, at the same address."""
        url = self.page.url if self.page.url.startswith("http") else self.site["url"]
        try:
            await asyncio.wait_for(self.page.close(run_before_unload=False), 5)
        except Exception:
            pass
        self.page = await self.context.new_page()
        self._watch(self.page)
        self.obs = None
        await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await self.settle()
        return f"reopened {url} in a fresh tab"

    async def back(self) -> str:
        await self.page.go_back(wait_until="domcontentloaded", timeout=10000)
        await self.settle()
        return "went back"

    async def screenshot(self) -> str:
        import base64
        data = await self.page.screenshot(type="jpeg", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(data).decode()

    # -------------------------------------------------------------- helpers
    async def login(self) -> str:
        """Fill the run's credentials into the visible login form and submit it (the model chose to sign in)."""
        credentials = self.site["credentials"]
        for frame in self.page.frames:
            password = frame.locator("input[type=password]:visible").first
            if not await password.count():
                continue
            user = frame.locator("form:has(input[type=password]) input:visible:not([type=password]):not([type=hidden])"
                                 ":not([type=checkbox]):not([type=radio]):not([readonly]):not([disabled])").first
            if not await user.count():
                user = frame.locator("input:visible:not([type=password]):not([type=hidden]):not([type=checkbox])"
                                     ":not([readonly]):not([disabled])").first
            before = self.page.url
            await user.fill(credentials["username"], timeout=3000)
            await password.fill(credentials["password"], timeout=3000)
            await password.press("Enter")
            await self.settle(before)
            return "signed in with the run credentials"
        raise ValueError("no login form on this page")

    async def read_pages(self, urls: list[str], limit: int = 12000) -> list[tuple[str, str]]:
        """Open model-selected pages of this website in parallel tabs (same session) and return their text."""
        async def read(url: str) -> tuple[str, str]:
            page = await self.context.new_page()
            try:
                target = self.resolve(url)
                for attempt in range(3):
                    response = await page.goto(target, wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(0.35)
                    if not response or response.status < 500:
                        break
                    await asyncio.sleep(0.5)
                obs = await self._render(page)
                if obs.password:
                    return url, "[session expired: this page shows a login form; sign in on the main page, then read again]"
                return url, obs.clip(limit)
            except Exception as error:
                return url, f"[could not read: {type(error).__name__}: {str(error)[:200]}]"
            finally:
                await page.close()
        semaphore = asyncio.Semaphore(8)

        async def bounded(url):
            async with semaphore:
                return await read(url)
        return await asyncio.gather(*(bounded(url) for url in urls[:24]))

    async def paginate(self, cid: int, max_pages: int = 8, limit: int = 30000) -> str:
        """Follow the model-chosen 'next page' / 'load more' control through a result set; new lines of each page."""
        control = self.obs.controls.get(cid) if self.obs else None
        if control is None:
            raise ValueError(f"control {cid} is not on the current page")
        key = (control.kind, control.label)
        seen = set(self.obs.text.splitlines())
        pages, total = [], 0
        for _ in range(max_pages):
            await self.click(cid)
            obs = await self.observe()
            fresh = [line for line in obs.text.splitlines() if line not in seen]
            seen.update(fresh)
            pages.append("\n".join(fresh))
            total += len(pages[-1])
            nxt = [c for c in obs.controls.values() if (c.kind, c.label) == key and not c.disabled]
            if not nxt or total > limit or not fresh:
                break
            cid = nxt[0].id
        return "\n--- next page ---\n".join(pages)[:limit]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[​-‏⁠﻿]", "", str(text))).strip().casefold()


def match_option(wanted, options: list[str]) -> str | None:
    """The option a model's value refers to: exact text, a number equal to the value, or a unique containment."""
    want = normalize(wanted)
    if not want:
        return None
    for option in options:
        if normalize(option) == want:
            return option
    number = re.fullmatch(r"-?\d+(?:[.,]\d+)?", want)
    if number:
        for option in options:
            first = re.match(r"\s*(-?\d+(?:[.,]\d+)?)", normalize(option))
            if first and first.group(1).replace(",", ".") == want.replace(",", "."):
                return option
    containing = [o for o in options if want in normalize(o) or (normalize(o) and normalize(o) in want and len(normalize(o)) > 2)]
    return containing[0] if len(containing) == 1 else None


def now() -> float:
    return time.monotonic()
