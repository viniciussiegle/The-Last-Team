"""Generated, loopback-only tender fixtures used by the holdout runner."""

from __future__ import annotations

import html
import random
import string
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


def _token(rng: random.Random, length: int = 9) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(rng.choice(alphabet) for _ in range(length))


def generate_case(rng: random.Random, index: int, layout: str | None = None,
                  language: str | None = None) -> dict:
    """Create facts independently from the browser/ledger extraction path."""
    variant = layout or rng.choice(("table", "cards", "nested_frame"))
    if variant not in {"table", "cards", "nested_frame"}:
        raise ValueError("Unknown synthetic layout")
    locale = language or rng.choice(("en", "de"))
    if locale not in {"en", "de"}:
        raise ValueError("Unknown synthetic label language")
    reference = f"QRF-{rng.randrange(100000, 999999)}-{rng.randrange(100, 999)}"
    decoy_reference = f"QRF-{rng.randrange(100000, 999999)}-{rng.randrange(100, 999)}"
    positions = []
    for _ in range(rng.randint(2, 3)):
        positions.append({
            "position_id": f"LOT-{_token(rng, 6).upper()}",
            "description": f"Synthetic {_token(rng, 5)} assembly {rng.randrange(10, 99)}",
            "quantity": str(rng.randint(1, 80)),
            "unit": rng.choice(("piece", "set")),
            "requirement_ref": f"REQ-{_token(rng, 6).upper()}",
        })
    decoy_position = {
        "position_id": f"LOT-{_token(rng, 6).upper()}",
        "description": f"Synthetic {_token(rng, 5)} decoy {rng.randrange(10, 99)}",
        "quantity": str(rng.randint(1, 80)),
        "unit": rng.choice(("piece", "set")),
        "requirement_ref": f"REQ-{_token(rng, 6).upper()}",
    }
    displayed_positions = list(positions)
    rng.shuffle(displayed_positions)
    return {
        "index": index,
        "layout": variant,
        "reference": reference,
        "positions": positions,
        "displayed_positions": displayed_positions,
        "decoy_reference": decoy_reference,
        "decoy_position": decoy_position,
        "routes": {
            "landing": "/",
            "catalog": f"/{_token(rng, 7)}/{_token(rng, 5)}/",
            "target": f"/{_token(rng, 6)}/item-{_token(rng, 8)}/",
            "decoy": f"/{_token(rng, 5)}/archive-{_token(rng, 7)}/",
            "frame_outer": f"/{_token(rng, 8)}/record-panel/",
            "frame": f"/{_token(rng, 7)}/line-schedule/",
        },
        "labels": {
            "catalog": rng.choice(("Register öffnen", "Ausschreibungen anzeigen", "Vergabeübersicht")
                                  if locale == "de" else
                                  ("Open register", "Browse opportunities", "Tender index")),
            "target": rng.choice(("Akte ansehen", "Vergabe öffnen", "Details anzeigen") if locale == "de" else
                                 ("Review record", "Open tender file", "View opportunity")),
            "decoy": rng.choice(("Archivkopie", "Andere Ausschreibung", "Frühere Bekanntmachung")
                                if locale == "de" else ("Archive copy", "Other tender", "Previous notice")),
        },
        "language": locale,
        "catalog_order": rng.choice(("target_first", "decoy_first")),
    }


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _facts(case: dict, reference: str, positions: list[dict]) -> str:
    header = f"<h1>Tender file {_e(reference)}</h1>"
    if case["layout"] == "table":
        rows = "".join("<tr>" + "".join(f"<td>{_e(row[key])}</td>" for key in
                    ("position_id", "description", "quantity", "unit", "requirement_ref")) + "</tr>"
                    for row in positions)
        return header + ("<table><thead><tr><th>Position</th><th>Item</th><th>Amount</th><th>Unit</th>"
                         "<th>Requirement</th></tr></thead><tbody>" + rows + "</tbody></table>")
    cards = "".join(
        "<article><h2>Line item</h2>"
        f"<p>Identifier: {_e(row['position_id'])}</p><p>Product: {_e(row['description'])}</p>"
        f"<p>Requested: {_e(row['quantity'])} {_e(row['unit'])}</p>"
        f"<p>Specification link: {_e(row['requirement_ref'])}</p></article>"
        for row in positions
    )
    return header + cards


def _page(case: dict, route: str) -> tuple[int, str]:
    paths = case["routes"]
    if route == paths["landing"]:
        body = (f"<h1>Supplier workspace</h1><p>Use the public tender register to locate an assigned file.</p>"
                f"<a href='{_e(paths['catalog'])}'>{_e(case['labels']['catalog'])}</a>")
    elif route == paths["catalog"]:
        target = (f"<p>{_e(case['reference'])} <a href='{_e(paths['target'])}'>"
                  f"{_e(case['labels']['target'])}</a></p>")
        decoy = (f"<p>{_e(case['decoy_reference'])} <a href='{_e(paths['decoy'])}'>"
                 f"{_e(case['labels']['decoy'])}</a></p>")
        links = decoy + target if case["catalog_order"] == "decoy_first" else target + decoy
        body = f"<h1>Current register</h1>{links}"
    elif route == paths["target"]:
        if case["layout"] == "nested_frame":
            body = (f"<h1>File {_e(case['reference'])}</h1><p>Open the embedded line schedule.</p>"
                    f"<iframe title='Tender record panel' src='{_e(paths['frame_outer'])}'></iframe>")
        else:
            body = _facts(case, case["reference"], case["displayed_positions"])
    elif route == paths["frame_outer"]:
        body = ("<h1>Record panel</h1><p>The line schedule is embedded below.</p>"
                f"<iframe title='Tender line schedule' src='{_e(paths['frame'])}'></iframe>")
    elif route == paths["frame"]:
        body = _facts(case, case["reference"], case["displayed_positions"])
    elif route == paths["decoy"]:
        body = _facts(case, case["decoy_reference"], [case["decoy_position"]])
    else:
        return 404, "<h1>Not found</h1>"
    return 200, "<!doctype html><html><head><title>Supplier workspace</title></head><body>" + body + "</body></html>"


def start_server(case: dict) -> tuple[ThreadingHTTPServer, Thread, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, content = _page(case, self.path.split("?", 1)[0])
            data = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}/"
