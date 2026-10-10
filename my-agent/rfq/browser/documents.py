"""Open and extract a model-selected document without persisting it in the checkout."""

from __future__ import annotations

import io
import os
import tempfile
import zipfile
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .session import origin

MAX_BYTES = 15 * 1024 * 1024
MAX_MEMBERS = 30
MAX_CHARS = 100_000
TEXT_SUFFIXES = {".txt", ".text", ".md", ".html", ".htm", ".csv", ".json", ".xml", ".rtf"}


class _TextHTML(HTMLParser):
    """Extract readable HTML text while preserving common document/table boundaries."""

    _BLOCK = {"address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "h1", "h2", "h3", "h4",
              "li", "ol", "p", "section", "table", "tr", "ul"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._suppressed += 1
        if not self._suppressed and tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._suppressed:
            self._suppressed -= 1
        if not self._suppressed and tag in self._BLOCK:
            self.parts.append("\n")
        if not self._suppressed and tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_data(self, data: str) -> None:
        if not self._suppressed and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _charset(content_type: str) -> str:
    message = Message()
    message["content-type"] = content_type
    return message.get_content_charset() or "utf-8"


def _html_text(data: bytes, content_type: str = "") -> str:
    try:
        decoded = data.decode(_charset(content_type), errors="replace")
    except LookupError:
        decoded = data.decode("utf-8", errors="replace")
    parser = _TextHTML()
    parser.feed(decoded)
    parser.close()
    return parser.text()


def _pdf_text(data: bytes) -> str:
    import pdfplumber

    parts: list[str] = []
    size = 0
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                value = page.extract_text() or ""
                if value:
                    parts.append(value)
                    size += len(value)
                    if size >= MAX_CHARS:
                        break
    except Exception as exc:
        raise ValueError("The selected PDF could not be parsed") from exc
    return "\n".join(parts)[:MAX_CHARS]


def _parse_one(data: bytes, name: str, content_type: str = "") -> str:
    if len(data) > MAX_BYTES:
        raise ValueError("Selected document exceeds the size limit")
    suffix = Path(name.split("?", 1)[0]).suffix.lower()
    kind = content_type.split(";", 1)[0].strip().lower()
    if data.startswith(b"%PDF-") or suffix == ".pdf" or kind == "application/pdf":
        return _pdf_text(data)
    if zipfile.is_zipfile(io.BytesIO(data)) or suffix == ".zip" or kind in {"application/zip", "application/x-zip-compressed"}:
        return _zip_text(data)
    if kind == "text/html" or suffix in {".html", ".htm"} or data.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<body")):
        return _html_text(data, content_type)
    if kind.startswith("text/") or suffix in TEXT_SUFFIXES or not kind or kind == "application/octet-stream":
        try:
            return data.decode(_charset(content_type), errors="replace")[:MAX_CHARS]
        except LookupError:
            return data.decode("utf-8", errors="replace")[:MAX_CHARS]
    raise ValueError("Selected document format is not supported")


def _zip_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_MEMBERS:
                raise ValueError("Selected archive contains too many members")
            total_size = sum(member.file_size for member in members if not member.is_dir())
            if total_size > MAX_BYTES:
                raise ValueError("Selected archive expands beyond the size limit")
            parts: list[str] = []
            size = 0
            for member in members:
                if member.is_dir() or Path(member.filename).suffix.lower() not in TEXT_SUFFIXES | {".pdf"}:
                    continue
                # Read the member directly from the archive; its name is never used as a filesystem path.
                content = bundle.read(member)
                if Path(member.filename).suffix.lower() == ".pdf":
                    extracted = _pdf_text(content)
                elif Path(member.filename).suffix.lower() in {".html", ".htm"}:
                    extracted = _html_text(content)
                else:
                    extracted = content.decode("utf-8", errors="replace")
                if extracted:
                    parts.append(f"[{member.filename}]\n{extracted}")
                    size += len(extracted)
                    if size >= MAX_CHARS:
                        break
    except zipfile.BadZipFile as exc:
        raise ValueError("The selected archive could not be parsed") from exc
    return "\n\n".join(parts)[:MAX_CHARS]


def _allowed_url(session, url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Selected document URL is not permitted")
    if origin(url) not in session.allowed_origins:
        raise ValueError("Selected document is outside this run's allowed websites")
    return url


def _filename(url: str, headers: dict[str, str]) -> str:
    disposition = headers.get("content-disposition", "")
    message = Message()
    message["content-disposition"] = disposition
    name = message.get_filename()
    return name or Path(urlsplit(url).path).name or "document"


async def _direct(session, url: str) -> str:
    response = None
    for redirect_count in range(6):
        response = await session.context.request.get(url, timeout=30_000, max_redirects=0)
        if response.status not in {301, 302, 303, 307, 308}:
            break
        headers = {key.lower(): value for key, value in response.headers.items()}
        location = headers.get("location")
        if not location or redirect_count == 5:
            raise ValueError("Selected document redirect could not be followed")
        url = _allowed_url(session, urljoin(url, location))
    assert response is not None
    if response.status < 200 or response.status >= 300:
        raise ValueError(f"Selected document request failed (HTTP {response.status})")
    headers = {key.lower(): value for key, value in response.headers.items()}
    try:
        content_length = int(headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    if content_length > MAX_BYTES:
        raise ValueError("Selected document exceeds the size limit")
    data = await response.body()
    if len(data) > MAX_BYTES:
        raise ValueError("Selected document exceeds the size limit")
    return _parse_one(data, _filename(url, headers), headers.get("content-type", ""))


async def _download_control(session, item: dict, element_id: int) -> str:
    tag, kind = item.get("tag"), str(item.get("type", "")).lower()
    if tag not in {"button", "input"} and kind not in {"button", "submit"}:
        raise ValueError("Selected control is not a download button")
    frame = item["frame"]
    page = frame.page
    locator = frame.locator(f'[data-rfq-observe="{element_id}"]')
    async with page.expect_download(timeout=30_000) as pending:
        await locator.click()
    download = await pending.value
    temp_path: Path | None = None
    try:
        _allowed_url(session, download.url)
        try:
            path = Path(await download.path())
        except Exception:
            handle, name = tempfile.mkstemp(prefix="rfq-document-", suffix=".bin")
            os.close(handle)
            temp_path = Path(name)
            await download.save_as(temp_path)
            path = temp_path
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Selected document exceeds the size limit")
        data = path.read_bytes()
        return _parse_one(data, download.suggested_filename, "")
    finally:
        await download.delete()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


async def open_document(session, element_id: int) -> str:
    """Open only the currently observed document link/control selected by the model."""
    try:
        item = session.elements[int(element_id)]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Unknown document element; use the latest observed element id") from exc
    href = item.get("href")
    if href:
        frame_url = item["frame"].url
        return await _direct(session, _allowed_url(session, urljoin(frame_url, href)))
    return await _download_control(session, item, int(element_id))
