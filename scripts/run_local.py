"""Load scout environment safely, then run the package with its own interpreter."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "my-agent"
ALLOWED = {
    "CHALLENGE_API_URL", "CHALLENGE_RUN_TOKEN", "CHALLENGE_SCOUTING", "CDP_URL", "CDP_URLS",
    "HEADLESS", "QWEN_BASE_URL", "QWEN_API_KEY", "CLEF_BASE_URL", "CLEF_API_KEY",
}


def _parse_assignments(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("export "):
            candidate = candidate[7:].strip()
        if candidate.startswith("$env:"):
            match = re.fullmatch(r"\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", candidate)
            if not match:
                continue
            key, raw = match.groups()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
                value = raw[1:-1]
            else:
                value = raw
        else:
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", candidate)
            if not match:
                continue
            key, raw = match.groups()
            try:
                parts = shlex.split(raw, posix=True)
                value = parts[0] if len(parts) == 1 else ""
            except ValueError:
                continue
        if key in ALLOWED:
            values[key] = value
    return values


def _cli_path() -> str | None:
    override = os.environ.get("ATIRA_CLI")
    if override:
        return override
    found = shutil.which("atira")
    if found:
        return found
    if os.name == "nt":
        standard = Path(os.environ.get("LOCALAPPDATA", "")) / "atira" / "bin" / "atira.cmd"
        if standard.is_file():
            return str(standard)
    return None


def _scout_env() -> dict[str, str]:
    cli = _cli_path()
    if not cli:
        raise RuntimeError("Atira CLI was not found; set ATIRA_CLI or provide CHALLENGE_SCOUTING")
    command = [cli, "scout", "--challenge", "rfq", "--env"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"Atira scout command failed (exit {result.returncode})")
    parsed = _parse_assignments(result.stdout)
    if not parsed:
        raise RuntimeError("Atira scout output contained no recognized environment assignments")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--orient", action="store_true")
    parser.add_argument("--budget-s", type=int)
    args = parser.parse_args(argv)
    child_env = os.environ.copy()
    if not (child_env.get("CHALLENGE_SCOUTING") or child_env.get("CHALLENGE_RUN_TOKEN")):
        try:
            child_env.update(_scout_env())
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.budget_s is not None:
        child_env["RUN_BUDGET_S"] = str(args.budget_s)
    uv = shutil.which("uv")
    command = ([uv] if uv else [sys.executable, "-m", "uv"]) + ["run", "--no-sync", "python", "-m", "rfq"]
    if args.orient:
        command.append("--orient")
    elif args.dry:
        command.append("--dry")
    return subprocess.run(command, cwd=PACKAGE, env=child_env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
