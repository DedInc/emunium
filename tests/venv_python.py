"""Run the project's virtualenv interpreter with the given arguments.

The venv is created with ``uv venv .venv --python 3.10`` so checks run on the
lowest supported Python without touching the global interpreter's packages.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

if __name__ == "__main__":
    if not VENV_PYTHON.exists():
        sys.exit(f"{VENV_PYTHON} is missing; create it with: uv venv .venv --python 3.10")
    sys.exit(subprocess.call([str(VENV_PYTHON), *sys.argv[1:]], cwd=ROOT))
