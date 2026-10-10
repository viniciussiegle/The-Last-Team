"""Create a clean package archive after a successful fair-play scan."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "my-agent"
SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", "node_modules", "runs", "runtime", ".pytest_cache"}
SKIP_NAMES = {".env", ".env.local", ".env.production", ".dev_spend.json"}


def _default_output() -> Path:
    configured = os.environ.get("RFQ_DATA_DIR")
    if configured:
        return Path(configured).expanduser() / "package.tar.gz"
    return Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "atira-rfq" / "package.tar.gz"


def _included(path: Path) -> bool:
    parts = path.relative_to(PACKAGE).parts
    if any(part in SKIP_DIRS for part in parts) or path.name in SKIP_NAMES:
        return False
    if path.name.startswith(".env") or path.suffix in {".pyc", ".pyo", ".tar", ".gz", ".zip"}:
        return False
    return path.is_file() and not path.is_symlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path)
    output = (parser.parse_args(argv).out or _default_output()).expanduser().resolve()
    check = subprocess.run([sys.executable, str(ROOT / "scripts" / "fairplay_check.py"), str(PACKAGE)], cwd=ROOT, check=False)
    if check.returncode:
        return check.returncode
    if output == PACKAGE or PACKAGE in output.parents or output == ROOT or ROOT in output.parents:
        print("Archive output must be outside the checkout", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(PACKAGE.rglob("*")):
            if _included(path):
                archive.add(path, arcname=path.relative_to(PACKAGE).as_posix(), recursive=False)
    print(f"Package written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
