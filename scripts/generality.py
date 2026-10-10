"""Fresh-seed, external-output synthetic navigation/extraction holdout."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from random import Random

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "my-agent"
PACKAGE_SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", "node_modules", "runs", "runtime", ".pytest_cache"}
PACKAGE_SKIP_NAMES = {".env", ".env.local", ".env.production", ".dev_spend.json"}
HARNESS_PATHS = {
    "scripts/generality.py": ROOT / "scripts" / "generality.py",
    "tests/mock_sites/generality.py": ROOT / "tests" / "mock_sites" / "generality.py",
}

sys.dont_write_bytecode = True
sys.path.insert(0, str(PACKAGE))
sys.path.insert(0, str(ROOT / "tests" / "mock_sites"))
from generality import generate_case, start_server  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_sha(value: object) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def checkout_root() -> Path:
    return ROOT.resolve()


def validate_output_dir(path: Path, checkout: Path | None = None) -> Path:
    output = path.expanduser().resolve()
    root = (checkout or checkout_root()).resolve()
    if output == root or root in output.parents:
        raise ValueError("Generality output directory must be outside the checkout")
    return output


def default_output_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    return validate_output_dir(base / "atira-rfq" / "generality")


def _package_files(package: Path):
    for path in sorted(package.rglob("*")):
        if any(part in PACKAGE_SKIP_DIRS for part in path.relative_to(package).parts):
            continue
        if path.is_file() and not path.is_symlink() and path.name not in PACKAGE_SKIP_NAMES \
                and not path.name.startswith(".env") and path.suffix not in {".pyc", ".pyo", ".tar", ".gz", ".zip"}:
            yield path


def package_hashes(package: Path = PACKAGE) -> dict[str, str]:
    return {path.relative_to(package).as_posix(): _sha(path.read_bytes()) for path in _package_files(package)}


def freeze_state(package: Path, model: str, configuration: dict,
                 harness_paths: dict[str, Path] | None = None) -> dict:
    files = package_hashes(package)
    prompts = {name: digest for name, digest in files.items() if "/prompts/" in name or name.startswith("prompts/")}
    harness_files = {name: _sha(path.read_bytes()) for name, path in (harness_paths or HARNESS_PATHS).items()}
    return {
        "package_files": files,
        "package_hash": _json_sha(files),
        "prompt_files": prompts,
        "prompt_hash": _json_sha(prompts),
        "evaluation_harness_files": harness_files,
        "evaluation_harness_hash": _json_sha(harness_files),
        "model": model,
        "configuration": configuration,
        "configuration_hash": _json_sha(configuration),
    }


def hashes_unchanged(before: dict, after: dict) -> tuple[bool, list[str], list[str]]:
    old, new = before.get("package_files", {}), after.get("package_files", {})
    changed = sorted((set(old) ^ set(new)) | {name for name in set(old) & set(new) if old[name] != new[name]})
    old_harness, new_harness = before.get("evaluation_harness_files", {}), after.get("evaluation_harness_files", {})
    changed_harness = sorted((set(old_harness) ^ set(new_harness)) |
                             {name for name in set(old_harness) & set(new_harness)
                              if old_harness[name] != new_harness[name]})
    unchanged = not changed and not changed_harness \
        and before.get("configuration_hash") == after.get("configuration_hash") \
        and before.get("model") == after.get("model")
    return unchanged, changed, changed_harness


def report_result(status: str, case_results: list[dict], frozen: dict, after: dict, cost: float) -> dict:
    unchanged, changed, changed_harness = hashes_unchanged(frozen, after)
    coverage = {layout: sum(row.get("layout") == layout for row in case_results)
                for layout in ("table", "cards", "nested_frame")}
    passed = (status == "evaluated" and len(case_results) >= 3 and all(coverage.values()) and unchanged
              and all(row.get("passed") for row in case_results))
    return {
        "status": status,
        "passed": passed,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": frozen["model"],
        "frozen_hashes": frozen,
        "final_hashes": after,
        "hashes_unchanged": unchanged,
        "changed_package_files": changed,
        "changed_evaluation_harness_files": changed_harness,
        "variant_coverage": coverage,
        "total_cost": round(cost, 8),
        "cases": case_results,
    }


def _fairplay() -> None:
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "fairplay_check.py"), str(PACKAGE)],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode:
        raise RuntimeError("fair-play check failed before holdout generation")


def _retire_seed(manifest: Path, index: int, seed: bytes) -> None:
    record = {"case": index, "seed_sha256": _sha(seed), "retired_at": datetime.now(timezone.utc).isoformat()}
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _case_public(case: dict) -> dict:
    return case


def _equal_records(expected: list[dict], actual: list[dict]) -> bool:
    canonical = lambda rows: sorted(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows)
    return canonical(expected) == canonical(actual)


def _navigation_events(events: list[dict]) -> list[dict]:
    current_url = None
    navigations = []
    for event in events:
        data = event.get("data", {})
        if event.get("type") == "observation":
            current_url = data.get("page_url", current_url)
        elif event.get("type") == "agent_step":
            action = data.get("action", {})
            next_url = data.get("page_url")
            if action.get("kind") in {"click", "goto"} and current_url and next_url and current_url != next_url:
                navigations.append({"kind": action["kind"], "from": current_url, "to": next_url})
            if next_url:
                current_url = next_url
    return navigations


async def _evaluate_case(case: dict, index: int, output: Path, model, settings) -> dict:
    from rfq.agents.base import BrowserAgent
    from rfq.browser.session import BrowserSession
    from rfq.evidence.ledger import Ledger
    from rfq.telemetry.events import EventBus
    from playwright.async_api import async_playwright

    started = time.monotonic()
    server, thread, url = start_server(case)
    bus = EventBus(output / "events", f"holdout-{index:03d}")
    run = {
        "brief": f"This synthetic exercise concerns tender reference {case['reference']}. Extract records only from the assigned local browser site.",
        "access": [{"siteId": f"holdout-{index:03d}", "url": url}],
    }
    task = (f"Locate the exact tender reference {case['reference']} using the site's navigation. "
            "Record every TenderPosition for that reference with position_id, description, quantity, unit, "
            "and requirement_ref. Ignore other references. Use only observed page evidence and finish with done.")
    ledger = Ledger(bus)
    before_cost = model.total_cost
    failure = None
    result = None
    try:
        async with async_playwright() as playwright:
            async def run_agent():
                session = await BrowserSession.connect(playwright, settings, run["access"][0], 0, run["access"])
                try:
                    agent = BrowserAgent(f"holdout-{index:03d}", session, model, bus, ledger, run, settings, {})
                    return await agent.run(task, max_steps=settings.max_steps, until=deadline)
                finally:
                    await session.close()

            deadline = time.monotonic() + settings.run_budget_s
            try:
                result = await asyncio.wait_for(run_agent(), timeout=settings.run_budget_s)
            except asyncio.TimeoutError:
                failure = "TimeoutError"
    except Exception as error:
        failure = failure or type(error).__name__
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    actual = [record.model_dump(mode="json") for record in ledger.of("TenderPosition")]
    expected = [{key: row[key] for key in ("position_id", "description", "quantity", "unit", "requirement_ref")}
                for row in case["positions"]]
    event_file = bus.events_path
    events = []
    if event_file.exists():
        with event_file.open(encoding="utf-8") as stream:
            events = [json.loads(line) for line in stream if line.strip()]
    browser_events = _navigation_events(events)
    exact = _equal_records(expected, actual)
    passed = exact and len(browser_events) >= 2 and failure is None
    return {
        "case": index,
        "layout": case["layout"],
        "passed": passed,
        "exact_records": exact,
        "expected_records": expected,
        "actual_records": actual,
        "browser_navigation_events": len(browser_events),
        "navigations": browser_events,
        "navigation_gate": len(browser_events) >= 2,
        "failure_type": failure,
        "agent_result": result,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "cost": round(model.total_cost - before_cost, 8),
    }


async def _evaluate(cases: list[dict], output: Path, model_name: str) -> tuple[list[dict], float]:
    from rfq.config import Settings
    from rfq.llm.clients import QwenClient
    from rfq.telemetry.events import EventBus

    settings = Settings.from_env({
        "HEADLESS": "1",
        "RFQ_DATA_DIR": str(output / "runtime"),
        "QWEN_MODEL": model_name,
        "MAX_STEPS": "24",
        "RUN_BUDGET_S": "420",
        "SUBMIT_MARGIN_S": "0",
    })
    bootstrap = EventBus(output / "events", "holdout-model")
    model = QwenClient(settings, bootstrap)
    results = []
    stdout_log = (output / "event_stdout.log").open("a", encoding="utf-8", newline="\n")
    try:
        with contextlib.redirect_stdout(stdout_log):
            for index, case in enumerate(cases, 1):
                results.append(await _evaluate_case(case, index, output, model, settings))
    finally:
        stdout_log.close()
        await model.client.close()
    return results, model.total_cost


def run(args: argparse.Namespace) -> dict:
    base = validate_output_dir(args.out or default_output_dir())
    _fairplay()
    run_id = secrets.token_hex(8)
    output = validate_output_dir(base / f"evaluation-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{run_id}")
    output.mkdir(parents=True, exist_ok=False)
    model_name = os.environ.get("QWEN_MODEL", "qwen3.8-flash-next")
    configuration = {
        "headless": True,
        "max_steps": 24,
        "run_budget_s": 420,
        "site_workers": 1,
        "layout_families": ["table", "cards", "nested_frame"],
        "qwen_base_url_hash": _sha(os.environ.get("QWEN_BASE_URL", "").encode()),
    }
    frozen = freeze_state(PACKAGE, model_name, configuration)
    _write_json(output / "frozen.json", frozen)

    cases = []
    manifest = output / f"retired_seeds-{run_id}.jsonl"
    for index in range(1, args.cases + 1):
        seed = secrets.token_bytes(32)
        _retire_seed(manifest, index, seed)
        layout = ("table", "cards", "nested_frame")[(index - 1) % 3]
        language = "de" if index % 2 == 0 else "en"
        cases.append(generate_case(Random(int.from_bytes(seed, "big")), index, layout, language))
        del seed

    if args.prepare_only:
        _write_json(output / "prepared_cases.json", [_case_public(case) for case in cases])
        after = freeze_state(PACKAGE, model_name, configuration)
        report = report_result("prepared_only", [{"case": i, "layout": case["layout"], "passed": False,
                                                  "status": "not_evaluated"}
                                                 for i, case in enumerate(cases, 1)], frozen, after, 0.0)
    else:
        if not os.environ.get("QWEN_BASE_URL") or not os.environ.get("QWEN_API_KEY"):
            raise RuntimeError("Qwen model environment is unavailable")
        _write_json(output / "prepared_cases.json", [_case_public(case) for case in cases])
        results, cost = asyncio.run(_evaluate(cases, output, model_name))
        final_model = os.environ.get("QWEN_MODEL", "qwen3.8-flash-next")
        final_configuration = {
            **configuration,
            "qwen_base_url_hash": _sha(os.environ.get("QWEN_BASE_URL", "").encode()),
        }
        after = freeze_state(PACKAGE, final_model, final_configuration)
        coverage = {layout: sum(case["layout"] == layout for case in cases)
                    for layout in ("table", "cards", "nested_frame")}
        status = "evaluated" if len(cases) >= 3 and all(coverage.values()) else "evaluated_partial"
        report = report_result(status, results, frozen, after, cost)
    report["run_id"] = run_id
    report["output_dir"] = str(output)
    _write_json(output / "report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.cases <= 25:
        parser.error("--cases must be between 1 and 25")
    try:
        report = run(args)
    except Exception as error:
        print(f"Generality holdout failed: {type(error).__name__}", file=sys.stderr)
        return 2
    layouts = sorted({case.get("layout", "unknown") for case in report["cases"]})
    passed = sum(bool(case.get("passed")) for case in report["cases"])
    state = "PASS" if report["passed"] else ("PREPARED ONLY" if report["status"] == "prepared_only"
                                                else "PARTIAL (not a pass)" if report["status"] == "evaluated_partial"
                                                else "FAIL")
    print(f"Generality {state}: {passed}/{len(report['cases'])} cases; layouts={','.join(layouts)}; "
          f"hashes_unchanged={report['hashes_unchanged']}; cost=${report['total_cost']:.6f}; "
          f"report={report['output_dir']}")
    return 0 if report["passed"] or report["status"] == "prepared_only" else 1


if __name__ == "__main__":
    raise SystemExit(main())
