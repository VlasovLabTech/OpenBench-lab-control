"""Run pytest in CI while preserving a plain-text diagnostic log."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

RESULTS_DIR = Path(".openbench/ci")
JUNIT_PATH = RESULTS_DIR / "pytest-results.xml"
LOG_PATH = RESULTS_DIR / "pytest-output.log"


def main() -> int:
    pytest_args = sys.argv[1:] or ["-q"]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        *pytest_args,
        f"--junitxml={JUNIT_PATH}",
    ]
    environment = os.environ.copy()
    environment.setdefault("PYTHONFAULTHANDLER", "1")

    with LOG_PATH.open("w", encoding="utf-8", newline="") as log_file:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
        except OSError as exc:
            message = f"Unable to start pytest: {exc}\n"
            log_file.write(message)
            sys.stderr.write(message)
            return 1

        assert process.stdout is not None
        for line in process.stdout:
            log_file.write(line)
            log_file.flush()
            sys.stdout.write(line)
            sys.stdout.flush()
        return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
