"""Runtime settings loaded from the current platform run or process environment."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _checkout_root() -> Path:
    package = Path(__file__).resolve().parents[1]
    return package.parent if (package.parent / ".git").exists() else package


def _default_data_dir() -> Path:
    configured = os.environ.get("RFQ_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    return base / "atira-rfq" / "runs"


@dataclass
class Settings:
    api_url: str | None
    run_token: str | None
    scouting: dict | None
    cdp_urls: list[str]
    headless: bool
    data_dir: Path
    submit_margin_s: int = 300
    max_steps: int = 70
    run_budget_s: int = 1100
    qwen_model: str = "qwen3.8-flash-next"
    frontier_enabled: bool = False
    frontier_model: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        scouting = None
        raw_scouting = source.get("CHALLENGE_SCOUTING")
        if raw_scouting:
            try:
                decoded = json.loads(raw_scouting)
            except (TypeError, ValueError) as exc:
                raise ValueError("CHALLENGE_SCOUTING must contain a JSON object") from exc
            if not isinstance(decoded, dict):
                raise ValueError("CHALLENGE_SCOUTING must contain a JSON object")
            scouting = decoded
        raw_urls = source.get("CDP_URLS", "")
        try:
            parsed_urls = json.loads(raw_urls) if raw_urls else []
        except ValueError:
            parsed_urls = []
        urls = [value for value in parsed_urls if isinstance(value, str)] if isinstance(parsed_urls, list) else []
        if not urls and source.get("CDP_URL"):
            urls = [source["CDP_URL"]]

        enabled = str(source.get("FRONTIER_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}
        if enabled:
            raise ValueError("Frontier model use is disabled pending human approval")
        data_dir = Path(source.get("RFQ_DATA_DIR") or _default_data_dir()).expanduser().resolve()
        checkout = _checkout_root().resolve()
        if data_dir == checkout or checkout in data_dir.parents:
            raise ValueError("RFQ_DATA_DIR must be outside the checkout")

        return cls(
            api_url=source.get("CHALLENGE_API_URL"),
            run_token=source.get("CHALLENGE_RUN_TOKEN"),
            scouting=scouting,
            cdp_urls=urls,
            headless=str(source.get("HEADLESS", "0")).strip().lower() in {"1", "true", "yes", "on"},
            data_dir=data_dir,
            submit_margin_s=int(source.get("SUBMIT_MARGIN_S", "300")),
            max_steps=int(source.get("MAX_STEPS", "70")),
            run_budget_s=int(source.get("RUN_BUDGET_S", "1100")),
            qwen_model=source.get("QWEN_MODEL", "qwen3.8-flash-next"),
            frontier_enabled=False,
            frontier_model=None,
        )
