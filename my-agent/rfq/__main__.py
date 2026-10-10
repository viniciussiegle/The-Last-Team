"""M0 CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from typing import Any

from .config import Settings
from .platform import load_run

_REDACT = re.compile(r"credential|password|passwd|secret|token|api[_-]?key|authorization|cookie|username|user[_-]?name", re.I)
_INLINE_CREDENTIAL = re.compile(r"(?im)^(\s*(?:username|password|benutzername|passwort)\s*:\s*)[^\r\n]+")


def _redact(value: Any, key: str = "") -> Any:
    if _REDACT.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rfq")
    parser.add_argument("--dry", action="store_true", help="read and summarize the current run")
    parser.add_argument("--orient", action="store_true", help="run the model-guided site orientation pass")
    args = parser.parse_args(argv)
    if not args.dry and not args.orient:
        print("RFQ orchestrator is not wired yet; use --dry to inspect the current run.", file=sys.stderr)
        return 2
    try:
        settings = Settings.from_env()
        run = load_run(settings)
    except (ValueError, RuntimeError) as exc:
        print(f"Cannot load run: {exc}", file=sys.stderr)
        return 1
    if args.orient:
        from .agents.orchestrator import orient
        from .telemetry.events import EventBus

        bus = EventBus(settings.data_dir, str(run.get("run_id") or "local"))
        asyncio.run(orient(run, settings, bus))
        return 0

    summary = {
        key: run.get(key)
        for key in ("run_id", "started_at", "expires_at", "submission", "answer_schema")
        if key in run
    }
    summary["access"] = [
        {key: site.get(key) for key in ("siteId", "url") if key in site}
        for site in run.get("access", []) if isinstance(site, dict)
    ]
    print("Run summary:")
    print(json.dumps(_redact(summary), ensure_ascii=False, indent=2, default=str))
    print("Runtime brief:")
    brief = run.get("brief", "")
    print(_INLINE_CREDENTIAL.sub(r"\1[REDACTED]", str(_redact(brief))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
