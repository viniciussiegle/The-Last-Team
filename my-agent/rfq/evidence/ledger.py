from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone, timedelta

from pydantic import ValidationError

from .models import PAYLOAD_GUIDE, RECORD_TYPES, Provenance


class Ledger:
    def __init__(self, bus):
        self.bus = bus
        self.records = []
        self.keys = set()
        self.allow_tan = False
        self.tan_context = None
        self.changed = asyncio.Event()

    def schema_text(self):
        return json.dumps({k: v.model_json_schema() for k, v in RECORD_TYPES.items()}, ensure_ascii=False) + "\n" + PAYLOAD_GUIDE

    def add(self, kind, data, session, agent, snippet, frame_path="frame/0"):
        if kind == "TAN" and not self.allow_tan:
            return "TAN evidence is accepted only during an explicit signing-code request. Continue the assigned evidence task."
        if kind not in RECORD_TYPES:
            return "Unknown record kind. Expected one of: " + ", ".join(RECORD_TYPES)
        if kind == "TAN" and isinstance(data, dict) and hasattr(self.bus, "add_secret"):
            self.bus.add_secret(str(data.get("value", "")))
        try:
            record = RECORD_TYPES[kind].model_validate(data)
        except ValidationError as error:
            self.bus.emit("record_rejected", agent, {"kind": kind, "reason": str(error)[:1500]})
            return "Validation failed; correct fields from evidence: " + str(error)[:2500]
        checks, missing = [], []
        if kind == "TAN" and self.tan_context:
            from .resolver import normalized, timestamp
            context = self.tan_context
            if not record.reference or normalized(record.reference) != normalized(context["reference"]):
                return "Signing code needs the exact tender reference from its message."
            checks.append("reference")
            if context.get("request_id"):
                if record.request_id != context["request_id"]:
                    return "Signing message does not match the portal's observed request identifier."
                checks.append("request_id")
            now = datetime.now(timezone.utc)
            try:
                if record.requested_at:
                    requested = timestamp(record.requested_at)
                    if requested > now + timedelta(seconds=5):
                        return "Signing request time is in the future; verify the message timestamp."
                    display_interval = timedelta(seconds=59 if record.requested_at_precision == "minute" else 0)
                    if requested + display_interval < timestamp(context["requested_after"]) - timedelta(seconds=5):
                        return "Signing message predates the current signing flow. Find the current request."
                    checks.append("request_time")
                if record.expires_at:
                    expires = timestamp(record.expires_at)
                    if expires <= now or (record.requested_at and expires <= timestamp(record.requested_at)):
                        return "Signing code is expired or has inconsistent validity metadata."
                    checks.append("expiry")
            except ValueError:
                return "Signing timestamps must include the observed timezone."
            missing = [name for name in ("request_id", "requested_at", "expires_at") if getattr(record, name) is None]
        key = kind + record.model_dump_json()
        if key in self.keys:
            return "Record already present."
        source_site = getattr(session, "current_site", session.site)
        source_url = session.page.url
        if frame_path != "frame/0":
            try:
                index = int(frame_path.removeprefix("frame/"))
                if not frame_path.startswith("frame/") or index < 0:
                    raise ValueError("Invalid frame path")
                source_url = session.page.frames[index].url
                from ..browser.session import origin
                source_site = session.sites_by_origin.get(origin(source_url), source_site)
            except (ValueError, IndexError, AttributeError):
                return "Unknown source frame; use the frame path from the latest observation."
        provenance = Provenance(site_id=source_site["siteId"], url=source_url, frame_path=frame_path,
            title=session.last_snapshot.text.splitlines()[1][:200] if session.last_snapshot else "",
            snippet=snippet[:300], observed_at=datetime.now(timezone.utc), agent=agent)
        self.records.append((kind, record, provenance))
        self.keys.add(key)
        self.changed.set()
        payload = record.model_dump(mode="json")
        if kind == "TAN":
            if hasattr(self.bus, "add_secret"):
                self.bus.add_secret(record.value)
            payload = {"value": "[ephemeral]", "freshness_checks": checks, "missing_metadata": missing}
        public_provenance = provenance.model_dump(mode="json")
        if kind == "TAN":
            public_provenance["snippet"] = "[ephemeral signing code]"
        self.bus.emit("record_added", agent, {"kind": kind, "record": payload, "provenance": public_provenance})
        return "Recorded " + kind

    def of(self, kind):
        return [record for name, record, _ in self.records if name == kind]

    async def wait_for_change(self, kind, count, timeout_s):
        if kind not in RECORD_TYPES:
            raise ValueError("Unknown evidence kind")
        loop = asyncio.get_running_loop()
        end = loop.time() + max(0, timeout_s)
        while len(self.of(kind)) <= count:
            remaining = end - loop.time()
            if remaining <= 0:
                return False
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except asyncio.TimeoutError:
                return False
        return True

    def summary(self, role=None, max_chars=16000):
        """Keep complete dependency records within a bounded, valid JSON view."""
        if max_chars < 512:
            raise ValueError("Evidence summary budget is too small")
        identity = {"TenderInfo": "reference", "TenderPosition": "position_id",
                    "SpecRevision": "revision_id", "Material": "number", "MailRecord": "message_id"}
        compact = {}
        for index, (kind, record, _) in enumerate(self.records):
            if kind == "TAN":
                continue
            data = record.model_dump(mode="json")
            data.pop("raw_text", None)
            key = (kind, data.get(identity.get(kind), index))
            if kind == "QAEntry":
                key = (kind, data["number"], data.get("position_id"), data["kind"])
            prior = compact.get(key)
            if prior and kind == "SpecRevision":
                requirements = {r["requirement_id"]: r for r in prior["requirements"]}
                requirements.update({r["requirement_id"]: r for r in data["requirements"]})
                data["requirements"] = list(requirements.values())
            elif prior and kind == "Material":
                data = {name: value if value is not None else prior.get(name) for name, value in data.items()}
            compact[key] = data
        priorities = {
            "erp": ["SpecRevision", "TenderPosition", "QAEntry", "MailRecord", "Material", "PriceCondition", "TenderInfo"],
            "spec": ["TenderPosition", "SpecRevision", "QAEntry", "MailRecord", "TenderInfo", "Material", "PriceCondition"],
            "tender": ["TenderInfo", "TenderPosition", "QAEntry", "SpecRevision", "MailRecord", "Material", "PriceCondition"],
            "mail": ["TenderInfo", "QAEntry", "SpecRevision", "Material", "PriceCondition", "MailRecord", "TenderPosition"],
        }
        order = priorities.get(role, ["TenderInfo", "TenderPosition", "SpecRevision", "QAEntry", "Material", "PriceCondition", "MailRecord"])
        ranked = []
        for key, data in compact.items():
            kind = key[0]
            rank = order.index(kind) if kind in order else len(order)
            if kind == "SpecRevision" and data["status"] != "current":
                data = {**data, "requirements": []}
                rank += len(order)
            if role == "erp" and kind == "MailRecord" and data["kind"] not in ("technikfreigabe", "devisenfixing"):
                rank += len(order)
            ranked.append((rank, {"record_type": kind, **data}))
        ranked.sort(key=lambda item: item[0])
        result = {"records": [], "omitted": {}}
        encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        # Leave room to disclose omitted counts instead of silently truncating JSON.
        for _, data in ranked:
            result["records"].append(data)
            if len(encode(result)) > max_chars - 256:
                result["records"].pop()
                kind = data["record_type"]
                result["omitted"][kind] = result["omitted"].get(kind, 0) + 1
        return encode(result)
