"""Reject likely persisted scenario data in package text without echoing it."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

REFERENCE = re.compile(r"\b[A-Z]{2,5}[-_/ ]\d{4}[-_/ ]\d{3,}\b")
LONG_DIGITS = re.compile(r"(?<!\d)\d{7,}(?!\d)")
SECRET = re.compile(r"(?i)(?:\b(?:sk|key|token|pass|secret)[-_][A-Za-z0-9_-]{12,}\b|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----)")
TAN = re.compile(r"(?i)\b(?:tan|one[- ]time code|verification code)\s*[:=]?\s*\d{4,8}\b")
URL = re.compile(r"(?i)https?://[^\s\"'<>]+")
HOST_LITERAL = re.compile(r"[\"'`]((?:[a-z0-9-]+\.)+[a-z]{2,})(?::\d+)?[\"'`]")
HOST_ASSIGNMENT = re.compile(r"(?i)\b(?:hostname|website|site\s+host|domain)\s*[:=]\s*[\"']([a-z0-9-]+(?:\.[a-z0-9-]+)+)[\"']")
GENERIC_HOSTS = {"example.com", "example.org", "example.net", "localhost", "pypi.org", "files.pythonhosted.org"}
FILE_SUFFIXES = {"py", "pyc", "pyo", "json", "jsonl", "toml", "md", "txt", "yaml", "yml", "ini", "cfg", "html", "css", "js", "ts", "tsx", "png", "jpg", "jpeg", "pdf", "zip", "gz", "lock"}
USERNAME = re.compile(r"(?i)\b(?:scouting[_ -]?username|username)\s*[:=]\s*['\"]?([A-Za-z0-9_.@+-]{3,})")
EXCLUDED_DIRS = {".venv", "venv", "__pycache__", ".git", "node_modules", "runs", "runtime"}
EXCLUDED_NAMES = {".env", ".env.local", ".env.production"}
TEXT_SUFFIXES = {".py", ".toml", ".json", ".md", ".txt", ".yaml", ".yml", ".ini", ".cfg", ".html", ".css", ".js", ".ts", ".tsx"}


def _files(root: Path):
    for path in root.rglob("*"):
        if any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and not path.is_symlink() and path.name not in EXCLUDED_NAMES and (path.name == "uv.lock" or path.suffix.lower() in TEXT_SUFFIXES):
            yield path


def _hostnames(line: str) -> set[str]:
    found: set[str] = set()
    for match in URL.finditer(line):
        try:
            hostname = urlsplit(match.group(0).rstrip(")]},.;")).hostname
        except ValueError:
            hostname = None
        if hostname:
            found.add(hostname.lower().rstrip("."))
    for match in HOST_LITERAL.finditer(line):
        hostname = match.group(1).lower().rstrip(".")
        if hostname.rsplit(".", 1)[-1] not in FILE_SUFFIXES:
            found.add(hostname)
    for match in HOST_ASSIGNMENT.finditer(line):
        found.add(match.group(1).lower().rstrip("."))
    return found


def scan(root: Path) -> list[tuple[Path, int, str]]:
    findings: list[tuple[Path, int, str]] = []
    for path in _files(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            checks = ((SECRET, "secret-like literal"),)
            if path.name != "uv.lock":
                checks += ((REFERENCE, "reference-like value"), (LONG_DIGITS, "long digit sequence"),
                           (TAN, "TAN-like literal"))
            for pattern, reason in checks:
                if pattern.search(line):
                    findings.append((path, number, reason))
            if any(name not in GENERIC_HOSTS and not name.endswith(".example.com") and not name.endswith(".example.org")
                   for name in _hostnames(line)):
                findings.append((path, number, "hostname-like value"))
            if USERNAME.search(line):
                findings.append((path, number, "username-like value"))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package_dir", nargs="?", type=Path, default=Path(__file__).resolve().parents[1] / "my-agent")
    root = parser.parse_args(argv).package_dir.resolve()
    if not root.is_dir():
        print("Fair-play check failed: package directory is missing", file=sys.stderr)
        return 2
    findings = scan(root)
    if findings:
        for path, number, reason in findings:
            print(f"{path.relative_to(root)}:{number}: {reason}", file=sys.stderr)
        return 1
    print("Fair-play check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
