from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from playwright.async_api import async_playwright

PACKAGE = Path(__file__).resolve().parents[1] / "my-agent"
import sys
sys.path.insert(0, str(PACKAGE))

from rfq.browser.documents import MAX_CHARS, MAX_MEMBERS, open_document
from rfq.browser.guards import login
from rfq.browser.session import origin
from rfq.browser.snapshot import observe


class SyntheticBrowser(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        fixture = Path(__file__).parent / "snapshot" / "portal.html"
        await self.page.goto(fixture.resolve().as_uri())
        self.session = SimpleNamespace(page=self.page, context=self.context, nav_hashes=set(), notices=[],
                                       elements={}, last_snapshot=None,
                                       site={"credentials": {"username": "synthetic-user", "password": "synthetic-pass"}})

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_snapshot_covers_nested_iframe_controls_and_tables(self):
        snapshot = await observe(self.session)
        self.assertEqual(len(self.page.frames), 3)
        self.assertIn("Synthetic details frame", snapshot.text)
        self.assertIn("Synthetic login panel", snapshot.text)
        self.assertIn("Sample cylinder | Current", snapshot.text)
        self.assertIn("75 mm", snapshot.text)
        self.assertNotIn("Hidden stale notice", snapshot.text)
        controls = list(snapshot.elements.values())
        self.assertTrue(any(item.get("href") == "documents/spec.html" for item in controls))
        password = next(item for item in controls if item.get("type") == "password")
        self.assertEqual(password["value"], "")
        self.assertIn("frame/2", password["frame_path"])
        self.assertNotIn("synthetic-pass", snapshot.text)

    async def test_generic_login_fills_only_the_nested_visible_form(self):
        await observe(self.session)
        result = await login(self.session)
        self.assertIn("credentials entered", result)
        nested = self.page.frames[2]
        self.assertEqual(await nested.locator('input[name="saved-address"]').input_value(), "synthetic saved address")
        self.assertEqual(await nested.locator('input[name="username"]').input_value(), "synthetic-user")
        self.assertEqual(await nested.locator('input[name="password"]').input_value(), "synthetic-pass")
        snapshot = await observe(self.session)
        password = next(item for item in snapshot.elements.values() if item.get("type") == "password")
        self.assertEqual(password["value"], "[secret]")
        self.assertNotIn("synthetic-pass", snapshot.text)


class _Response:
    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8", status: int = 200):
        self._body = body
        self.headers = {"content-type": content_type}
        self.status = status

    async def body(self):
        return self._body


class _Request:
    def __init__(self, response):
        self.responses = response if isinstance(response, list) else [response]
        self.urls = []

    async def get(self, url, timeout, max_redirects=None):
        self.urls.append(url)
        return self.responses.pop(0)


class DocumentParsing(unittest.IsolatedAsyncioTestCase):
    def session(self, item, response=None):
        request = _Request(response) if response else None
        context = SimpleNamespace(request=request)
        session = SimpleNamespace(elements={4: item}, context=context, allowed_origins={"https://supplier.example.test"})
        return session, request

    async def test_relative_observed_link_resolves_against_its_frame_and_extracts_html(self):
        request = _Request(_Response(b"<html><h1>Selected specification</h1><script>ignore()</script><table><tr><th>Field</th><th>Value</th></tr><tr><td>Stroke</td><td>75 mm</td></tr></table></html>"))
        session = SimpleNamespace(
            elements={4: {"href": "../documents/spec.html", "frame": SimpleNamespace(url="https://supplier.example.test/section/frame")}},
            context=SimpleNamespace(request=request), allowed_origins={"https://supplier.example.test"})
        result = await open_document(session, 4)
        self.assertEqual(request.urls, ["https://supplier.example.test/documents/spec.html"])
        self.assertIn("Selected specification", result)
        self.assertIn("Stroke | 75 mm", result)
        self.assertNotIn("ignore", result)

    async def test_link_outside_allowed_origins_is_rejected_before_request(self):
        request = _Request(_Response(b"unused"))
        session = SimpleNamespace(
            elements={4: {"href": "https://other.example.test/doc.pdf", "frame": SimpleNamespace(url="https://supplier.example.test/page")}},
            context=SimpleNamespace(request=request), allowed_origins={"https://supplier.example.test"})
        with self.assertRaisesRegex(ValueError, "outside"):
            await open_document(session, 4)
        self.assertEqual(request.urls, [])

    async def test_external_redirect_is_blocked_before_following_it(self):
        redirect = _Response(b"", status=302)
        redirect.headers["location"] = "https://other.example.test/redirected.txt"
        request = _Request(redirect)
        session = SimpleNamespace(
            elements={4: {"href": "spec.txt", "frame": SimpleNamespace(url="https://supplier.example.test/docs/page")}},
            context=SimpleNamespace(request=request), allowed_origins={"https://supplier.example.test"})
        with self.assertRaisesRegex(ValueError, "outside"):
            await open_document(session, 4)
        self.assertEqual(request.urls, ["https://supplier.example.test/docs/spec.txt"])

    async def test_proxy_request_context_uses_browser_cookies_and_is_disposed(self):
        redirect = _Response(b"", status=302)
        redirect.headers["location"] = "/files/selected.txt"
        request = _Request([redirect, _Response(b"Synthetic proxied document", "text/plain")])

        class TemporaryContext:
            def __init__(self):
                self.request = request
                self.disposed = False

            async def get(self, *args, **kwargs):
                return await self.request.get(*args, **kwargs)

            async def dispose(self):
                self.disposed = True

        temporary = TemporaryContext()

        class Factory:
            def __init__(self):
                self.options = None

            async def new_context(self, **options):
                self.options = options
                return temporary

        class BrowserContext:
            request = SimpleNamespace()

            async def storage_state(self):
                return {"cookies": [{"name": "synthetic-session", "value": "fixture"}], "origins": []}

        factory = Factory()
        item = {"href": "selected.txt", "frame": SimpleNamespace(url="https://supplier.example.test/docs/page")}
        session = SimpleNamespace(elements={4: item}, context=BrowserContext(), request_factory=factory,
                                  allowed_origins={"https://supplier.example.test"})
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy.synthetic.test:9000", "HTTP_PROXY": ""}, clear=False):
            result = await open_document(session, 4)

        self.assertEqual(result, "Synthetic proxied document")
        self.assertEqual(request.urls, ["https://supplier.example.test/docs/selected.txt",
                                        "https://supplier.example.test/files/selected.txt"])
        self.assertEqual(factory.options["proxy"], {"server": "http://proxy.synthetic.test:9000"})
        self.assertEqual(factory.options["storage_state"]["cookies"][0]["name"], "synthetic-session")
        self.assertTrue(temporary.disposed)

    async def test_proxy_request_context_is_disposed_after_http_failure(self):
        request = _Request(_Response(b"unavailable", "text/plain", status=503))

        class TemporaryContext:
            disposed = False

            async def get(self, *args, **kwargs):
                return await self.request.get(*args, **kwargs)

            async def dispose(self):
                self.disposed = True

        temporary = TemporaryContext()
        temporary.request = request

        class Factory:
            async def new_context(self, **_options):
                return temporary

        class BrowserContext:
            request = SimpleNamespace()

            async def storage_state(self):
                return {"cookies": [], "origins": []}

        item = {"href": "selected.txt", "frame": SimpleNamespace(url="https://supplier.example.test/docs/page")}
        session = SimpleNamespace(elements={4: item}, context=BrowserContext(), request_factory=Factory(),
                                  allowed_origins={"https://supplier.example.test"})
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy.synthetic.test:9000"}, clear=False):
            with self.assertRaisesRegex(ValueError, "HTTP 503"):
                await open_document(session, 4)

        self.assertTrue(temporary.disposed)

    async def test_zip_members_are_read_in_memory_without_extracting_paths(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("nested/../selected.txt", "Synthetic archived text")
            bundle.writestr("notes.html", "<h1>Archive note</h1><p>Readable body</p>")
        item = {"href": "bundle.zip", "frame": SimpleNamespace(url="https://supplier.example.test/docs/index")}
        session, _ = self.session(item, _Response(archive.getvalue(), "application/zip"))
        result = await open_document(session, 4)
        self.assertIn("Synthetic archived text", result)
        self.assertIn("Archive note", result)
        self.assertFalse((Path.cwd() / "selected.txt").exists())

    async def test_archive_member_and_text_limits_are_enforced(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            for index in range(MAX_MEMBERS + 1):
                bundle.writestr(f"{index}.txt", "x")
        item = {"href": "bundle.zip", "frame": SimpleNamespace(url="https://supplier.example.test/docs/index")}
        session, _ = self.session(item, _Response(archive.getvalue(), "application/zip"))
        with self.assertRaisesRegex(ValueError, "too many"):
            await open_document(session, 4)

        large = _Response(b"x" * (MAX_CHARS + 3), "text/plain")
        session, _ = self.session({"href": "large.txt", "frame": SimpleNamespace(url="https://supplier.example.test/doc")}, large)
        self.assertEqual(len(await open_document(session, 4)), MAX_CHARS)

    async def test_selected_download_control_is_read_and_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.txt"
            path.write_text("Synthetic control download", encoding="utf-8")

            class Download:
                url = "https://supplier.example.test/files/synthetic.txt"
                suggested_filename = "synthetic.txt"

                async def path(self):
                    return path

                async def delete(self):
                    self.deleted = True

            download = Download()

            class Pending:
                value = self._resolved(download)

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_):
                    return False

            class Locator:
                async def click(self):
                    return None

            class Frame:
                page = SimpleNamespace(expect_download=lambda **kwargs: Pending())

                def locator(self, selector):
                    self.selector = selector
                    return Locator()

            session = SimpleNamespace(elements={4: {"tag": "button", "type": "button", "frame": Frame()}},
                                      allowed_origins={"https://supplier.example.test"})
            self.assertEqual(await open_document(session, 4), "Synthetic control download")
            self.assertTrue(download.deleted)

    @staticmethod
    async def _resolved(value):
        return value


def _synthetic_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(content)
    content.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(content)


class PDFParsing(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_is_parsed_from_memory(self):
        item = {"href": "spec.pdf", "frame": SimpleNamespace(url="https://supplier.example.test/docs/index")}
        request = _Request(_Response(_synthetic_pdf("Synthetic PDF requirement"), "application/pdf"))
        session = SimpleNamespace(elements={4: item}, context=SimpleNamespace(request=request),
                                  allowed_origins={"https://supplier.example.test"})
        result = await open_document(session, 4)
        self.assertIn("Synthetic PDF requirement", result)


if __name__ == "__main__":
    unittest.main()
