"""Small read/answer client for the current platform run contract."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings

USER_AGENT = "atira-rfq-agent/1"


def _request(url: str, token: str | None = None, answer: dict | None = None) -> tuple[int, str]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    body = None
    if token:
        headers["x-api-key"] = token
    if answer is not None:
        body = json.dumps(answer, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers)
    try:
        with urlopen(request, timeout=60 if answer is not None else 30) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def load_run(settings: Settings) -> dict:
    if settings.run_token:
        if not settings.api_url:
            raise RuntimeError("CHALLENGE_API_URL is required with a run token")
        url = settings.api_url.rstrip("/") + "/v1/runs/current"
        status, body = 0, ""
        for attempt in range(5):
            try:
                status, body = _request(url, settings.run_token)
            except (URLError, OSError):
                status = 0
            if status == 200:
                try:
                    run = json.loads(body)
                except ValueError as exc:
                    raise RuntimeError("Platform returned invalid run JSON") from exc
                if isinstance(run, dict):
                    return run
                raise RuntimeError("Platform run response must be an object")
            if status in (401, 403, 404):
                break
            if attempt < 4:
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Could not load current run (HTTP {status})")
    if settings.scouting is not None:
        return settings.scouting
    raise RuntimeError("No run is configured; set CHALLENGE_RUN_TOKEN or CHALLENGE_SCOUTING")


def seconds_left(run: dict, now: float | None = None) -> float:
    expiry = run.get("expires_at")
    if expiry is None:
        return float("inf")
    if isinstance(expiry, str):
        try:
            parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(expiry)
            except (TypeError, ValueError):
                return float("inf")
        timestamp = parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    else:
        timestamp = float(expiry)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
    return timestamp - (time.time() if now is None else now)


def submit_api_answer(run: dict, token: str, answer: dict) -> tuple[int, str]:
    submission = run.get("submission") or {}
    if submission.get("kind") != "api":
        raise ValueError("This run does not accept API submission")
    url = run.get("answer_url") or submission.get("url")
    if not url:
        raise ValueError("API submission target is missing")
    return _request(url, token, answer)
