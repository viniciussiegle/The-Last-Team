"""Run the agent locally with Atira's public scouting environment.

Start ``atira models proxy`` in another terminal, then run ``python dev.py``.
This parses the CLI's exported values as data, without shell evaluation.
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path


def main() -> None:
    result = subprocess.run(["atira", "scout", "--challenge", "rfq", "--env"],
                            capture_output=True, text=True, timeout=30, check=True)
    environment = os.environ.copy()
    for line in result.stdout.splitlines():
        if line.startswith("export "):
            assignment = shlex.split(line)[1]
            key, value = assignment.split("=", 1)
            environment[key] = value
    if "CHALLENGE_SCOUTING" not in environment:
        raise SystemExit("Atira did not return the public scouting task")
    environment.setdefault("HEADLESS", "1")
    agent = str(Path(__file__).with_name("agent.py"))
    os.execvpe(sys.executable, [sys.executable, agent], environment)


if __name__ == "__main__":
    main()
