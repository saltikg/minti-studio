#!/home/ubuntu/apps/minti_studio/.venv/bin/python
"""Post-pull import smoke test for production deploys.

Deploy sequence:
  ./scripts/safe_to_deploy.py   (before pull — is it safe to deploy now?)
  git pull
  ./scripts/verify_imports.py   (after pull, before restart — will it boot?)
  sudo systemctl restart both services

This script intentionally does not restart services or mutate application state.
It shells out to the service venv's Python and imports the real WSGI entrypoint
(`wsgi`) in an isolated subprocess, then reports success/failure by exit code.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROD_VENV_PYTHON = Path("/home/ubuntu/apps/minti_studio/.venv/bin/python")


def _resolve_python() -> Path:
    candidates = [
        PROD_VENV_PYTHON,
        ROOT / ".venv" / "bin" / "python",
        ROOT / "venv" / "bin" / "python",
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("No executable Python interpreter found for import verification.")


def main() -> int:
    python_bin = _resolve_python()
    result = subprocess.run(
        [str(python_bin), "-c", "import wsgi"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("IMPORTS_OK")
        return 0

    print("IMPORTS_FAILED")
    stdout = (result.stdout or "").rstrip()
    stderr = (result.stderr or "").rstrip()
    if stdout:
        print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)
    return result.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
