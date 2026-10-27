# tests/docker/run_linux.py
"""Build the Linux test image and run the unit and e2e suites in a container.

The build context is a temporary directory holding only the package, the
tests, the examples and the project metadata (no virtualenv, browser
profiles or agent state). The container's full output goes to a log file; its tail is printed
and the script exits with the container's status.

Usage: ``python tests/docker/run_linux.py [--tail N] [--log PATH] [--timeout S]``
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE = "emunium-linux-tests:local"
CONTEXT_DIRS = ("emunium", "tests", "examples")
CONTEXT_FILES = ("pyproject.toml", "README.md", "CHANGELOG.md", "LICENSE")
# "results": local output of examples/parallel runs (gitignored).
IGNORED = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".pytest_cache", "node_modules", "results"
)
DEFAULT_LOG = Path(tempfile.gettempdir()) / "emunium-linux-tests.log"
# Exit status when the build fails or the run times out (the container never
# reported one).
FAILED = 1


def build_context(target: Path) -> None:
    for name in CONTEXT_DIRS:
        shutil.copytree(REPO_ROOT / name, target / name, ignore=IGNORED)
    for name in CONTEXT_FILES:
        shutil.copy2(REPO_ROOT / name, target / name)


def run_logged(command: list[str], log: Path, timeout: float | None) -> int:
    """Run ``command``, appending its output to ``log``; return its status."""
    with log.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(f"$ {' '.join(command)}\n")
        handle.flush()
        process = subprocess.Popen(
            command, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL
        )
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise


def tail(log: Path, lines: int) -> str:
    with log.open(encoding="utf-8", errors="replace") as handle:
        return "".join(deque(handle, maxlen=lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the unit and e2e suites in the Linux test container."
    )
    parser.add_argument("--tail", type=int, default=60, help="log lines to print")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="full log")
    parser.add_argument(
        "--timeout", type=float, default=1800.0, help="container run limit (s)"
    )
    args = parser.parse_args(argv)
    if shutil.which("docker") is None:
        print("docker is not on PATH", file=sys.stderr)
        return FAILED
    log: Path = args.log
    log.write_text("", encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="emunium-docker-") as directory:
        context = Path(directory)
        build_context(context)
        dockerfile = context / "tests" / "docker" / "Dockerfile"
        print(f"building {IMAGE} (log: {log})", flush=True)
        status = run_logged(
            ["docker", "build", "-t", IMAGE, "-f", str(dockerfile), str(context)],
            log,
            None,
        )
    if status != 0:
        print(tail(log, args.tail), end="")
        print(f"docker build failed with status {status}", file=sys.stderr)
        return status or FAILED

    name = f"emunium-tests-{uuid.uuid4().hex[:12]}"
    # Chromium needs more shared memory than Docker's 64 MB default, and its
    # sandbox needs user namespaces, which Docker's default seccomp profile
    # refuses to unprivileged users: lifting the filter keeps the sandbox on
    # (no --no-sandbox) for a non-root user without capabilities.
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--shm-size=1g",
        "--security-opt",
        "seccomp=unconfined",
        "--cap-drop=ALL",
        "--name",
        name,
    ]
    print(f"running the suites in {name}", flush=True)
    try:
        status = run_logged([*command, IMAGE], log, args.timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        print(tail(log, args.tail), end="")
        print(f"container timed out after {args.timeout:.0f} s", file=sys.stderr)
        return FAILED
    print(tail(log, args.tail), end="")
    print(f"container exited with status {status} (full log: {log})")
    return status


if __name__ == "__main__":
    sys.exit(main())
