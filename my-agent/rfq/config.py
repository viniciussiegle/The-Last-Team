"""Runtime settings loaded from the current platform run or process environment."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

# Published simulated prices of the default hosted model, USD per million tokens.
QWEN_PRICES = {"input": 0.15, "cached": 0.016, "output": 0.47}
# Request parameters that switch the default hosted model's reasoning pass off.
QWEN_PLAIN_ANSWERS = {"chat_template_kwargs": {"enable_thinking": False}}


def _json_object(raw: str | None, name: str) -> dict:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain a JSON object") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return decoded


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
    submit_margin_s: int = 420
    max_steps: int = 70
    # A scored run ends after 20 minutes; test runs allow longer but must behave the same.
    run_budget_s: int = 1140
    # The model is chosen by configuration alone; any OpenAI-compatible endpoint behind the run proxy fits.
    model_name: str = "qwen3.8-flash-next"
    model_base_url_env: str = "QWEN_BASE_URL"
    model_api_key_env: str = "QWEN_API_KEY"
    model_extra_body: dict = field(default_factory=dict)
    model_light_body: dict = field(default_factory=dict)
    model_prices: dict = field(default_factory=lambda: dict(QWEN_PRICES))
    model_concurrency: int = 8
    model_stream: bool = True
    model_first_chunk_timeout_s: int = 25
    model_chunk_gap_timeout_s: int = 20
    model_call_timeout_s: int = 180
    model_first_call_timeout_s: int = 360
    model_retries: int = 2
    surcharge_includes_setup: bool = True
    rules_stage: str = "core"
    submit_enabled: bool = True

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

        # Two sets of request parameters: the usual one, and a lighter one for steps that only copy known values
        # into a form. For the default hosted model the lighter one switches the reasoning pass off, which halves
        # the time of a call. Reading and recording values keeps reasoning: without it a full run recorded
        # specification values that were not on the page.
        extra_body = _json_object(source.get("MODEL_EXTRA_BODY"), "MODEL_EXTRA_BODY")
        light_body = _json_object(source.get("MODEL_LIGHT_EXTRA_BODY"), "MODEL_LIGHT_EXTRA_BODY") or dict(extra_body)
        if source.get("MODEL_LIGHT_EXTRA_BODY") is None and source.get("MODEL_BASE_URL_ENV", "QWEN_BASE_URL") == "QWEN_BASE_URL":
            light_body = {**extra_body, **QWEN_PLAIN_ANSWERS}
        prices = {**QWEN_PRICES, **{key: float(value) for key, value in
                                    _json_object(source.get("MODEL_PRICES_USD_PER_M"), "MODEL_PRICES_USD_PER_M").items()}}
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
            submit_margin_s=int(source.get("SUBMIT_MARGIN_S", "420")),
            max_steps=int(source.get("MAX_STEPS", "70")),
            run_budget_s=int(source.get("RUN_BUDGET_S", "1140")),
            model_name=source.get("MODEL_NAME") or source.get("QWEN_MODEL", "qwen3.8-flash-next"),
            model_base_url_env=source.get("MODEL_BASE_URL_ENV", "QWEN_BASE_URL"),
            model_api_key_env=source.get("MODEL_API_KEY_ENV", "QWEN_API_KEY"),
            model_extra_body=extra_body,
            model_light_body=light_body,
            model_prices=prices,
            model_concurrency=int(source.get("MODEL_CONCURRENCY", "8")),
            model_stream=str(source.get("MODEL_STREAM", "true")).strip().lower() in {"1", "true", "yes", "on"},
            model_first_chunk_timeout_s=int(source.get("MODEL_FIRST_CHUNK_TIMEOUT_S", "25")),
            model_chunk_gap_timeout_s=int(source.get("MODEL_CHUNK_GAP_TIMEOUT_S", "20")),
            model_call_timeout_s=int(source.get("MODEL_CALL_TIMEOUT_S", "180")),
            model_first_call_timeout_s=int(source.get("MODEL_FIRST_CALL_TIMEOUT_S", "360")),
            model_retries=int(source.get("MODEL_RETRIES", "2")),
            surcharge_includes_setup=str(source.get("SURCHARGE_INCLUDES_SETUP", "true")).lower() in {"1", "true", "yes"},
            rules_stage=source.get("RULES_STAGE", "core"),
            # Development only: stop after freezing the answer, without touching the submission form.
            submit_enabled=str(source.get("RFQ_SUBMIT", "true")).strip().lower() in {"1", "true", "yes", "on"},
        )
