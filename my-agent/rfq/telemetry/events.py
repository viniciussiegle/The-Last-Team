"""Append-only, credential-scrubbed run event output."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

_SENSITIVE = re.compile(r"credential|password|passwd|secret|(?:^|_)(?:run_token|access_token|token)$|api[_-]?key|authorization|cookie|username|user[_-]?name", re.I)


def _safe(value: Any, key: str = "", secrets=()) -> Any:
    if _SENSITIVE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k), secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


class EventBus:
    def __init__(self, data_dir: Path, run_id: str):
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(run_id)) or "run"
        root = Path(data_dir).expanduser().resolve()
        from ..config import _checkout_root
        checkout = _checkout_root()
        if root == checkout or checkout in root.parents:
            raise ValueError("Event data directory must be outside the checkout")
        self.run_dir = root / safe_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.secrets = set()

    def add_secret(self, value):
        if isinstance(value, str) and value:
            self.secrets.add(value)

    def emit(self, type: str, agent: str, data: dict) -> dict:
        event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_dir.name,
            "type": type,
            "agent": agent,
            "data": _safe(data, secrets=sorted(self.secrets, key=len, reverse=True)),
        }
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
        with self.events_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
        print("@@EVT " + line, flush=True)
        return event
