from __future__ import annotations

import json
from datetime import datetime, timezone

from pydantic import ValidationError

from .models import PAYLOAD_GUIDE, RECORD_TYPES, Provenance


class Ledger:
    def __init__(self, bus):
        self.bus = bus
        self.records = []
        self.keys = set()
        self.allow_tan = False

    def schema_text(self):
        return json.dumps({k: v.model_json_schema() for k, v in RECORD_TYPES.items()}, ensure_ascii=False) + "\n" + PAYLOAD_GUIDE

    def add(self, kind, data, session, agent, snippet, frame_path="frame/0"):
        if kind == "TAN" and not self.allow_tan:
            return "TAN evidence is accepted only during an explicit signing-code request. Continue the assigned evidence task."
        if kind not in RECORD_TYPES:
            return "Unknown record kind. Expected one of: " + ", ".join(RECORD_TYPES)
        try:
            record = RECORD_TYPES[kind].model_validate(data)
        except ValidationError as error:
            self.bus.emit("record_rejected", agent, {"kind": kind, "reason": str(error)[:1500]})
            return "Validation failed; correct fields from evidence: " + str(error)[:2500]
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
        payload = record.model_dump(mode="json")
        if kind == "TAN":
            if hasattr(self.bus, "add_secret"):
                self.bus.add_secret(record.value)
            payload = {"value": "[ephemeral]"}
        public_provenance = provenance.model_dump(mode="json")
        if kind == "TAN":
            public_provenance["snippet"] = "[ephemeral signing code]"
        self.bus.emit("record_added", agent, {"kind": kind, "record": payload, "provenance": public_provenance})
        return "Recorded " + kind

    def of(self, kind):
        return [record for name, record, _ in self.records if name == kind]

    def summary(self):
        # Structured facts, rather than repeated page history, are shared across agents.
        return json.dumps([{"kind": kind, **record.model_dump(mode="json")} for kind, record, _ in self.records if kind != "TAN"], ensure_ascii=False)
