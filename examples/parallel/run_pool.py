# examples/parallel/run_pool.py
"""Run a job list across N emunium workers and print one summary table.

Standard library only. Two modes:

``--mode docker`` (default)
    N headed workers, each in its own container with its own X display:
    ``docker compose run --rm -e SHARD=i/N worker`` per worker, in parallel.
    The job file must be inside ``jobs/`` (mounted read-only at ``/jobs``).

``--mode local``
    N ``worker.py --headless`` processes on this machine, no Docker. Headless
    browsers need no display and refuse native input, so the click step is
    skipped; use it for DOM-only work or to check a job list quickly.

Worker ``i`` takes jobs ``i, i+N, i+2N, ...``. Every worker writes one JSON
file per job to ``results/<run id>/``; the table at the end is read from
there. Ctrl+C stops every worker (containers are removed by name).

Examples::

    python run_pool.py --mode docker --workers 3
    python run_pool.py --mode local --workers 2 --jobs jobs/example.json

Exit status: 0 when every worker exited 0, 1 otherwise, 2 on bad arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPOSE_FILE = HERE / "docker-compose.yml"
JOBS_DIR = HERE / "jobs"
WORKER = HERE / "worker.py"
STOP_GRACE = 15.0

_print_lock = threading.Lock()


def say(line: str) -> None:
    with _print_lock:
        print(line, flush=True)


@dataclass
class Outcome:
    index: int
    status: int | None = None
    seconds: float = 0.0
    note: str = ""


@dataclass
class Pool:
    """Running worker processes, so Ctrl+C can stop all of them."""

    procs: dict[int, subprocess.Popen[str]] = field(default_factory=dict)
    containers: dict[int, str] = field(default_factory=dict)
    stopping: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Split a job list across N emunium workers (docker or local)."
    )
    parser.add_argument("--mode", choices=("docker", "local"), default="docker")
    parser.add_argument("--workers", "-n", type=int, default=2, help="worker count")
    parser.add_argument(
        "--jobs",
        type=Path,
        default=JOBS_DIR / "example.json",
        help="JSON job list (docker mode: a file inside jobs/)",
    )
    parser.add_argument(
        "--results", type=Path, default=HERE / "results", help="result directory"
    )
    parser.add_argument(
        "--run-id",
        default=datetime.now().strftime("pool-%Y%m%d-%H%M%S"),
        help="results sub-directory for this run",
    )
    parser.add_argument(
        "--job-timeout", type=float, default=60.0, help="seconds per job"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        help="seconds before a whole worker is stopped",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="docker mode: skip `docker compose build`",
    )
    return parser


def compose(*args: str) -> list[str]:
    return ["docker", "compose", "-f", str(COMPOSE_FILE), *args]


def worker_command(
    args: argparse.Namespace, index: int
) -> tuple[list[str], str | None]:
    """The command for worker ``index`` and, in docker mode, its container name."""
    shard = f"{index}/{args.workers}"
    if args.mode == "local":
        return [
            sys.executable,
            str(WORKER),
            str(args.jobs),
            "--headless",
            "--shard",
            shard,
            "--run-id",
            args.run_id,
            "--results",
            str(args.results),
            "--job-timeout",
            str(args.job_timeout),
        ], None
    name = f"emunium-pool-{args.run_id}-{index}"
    return compose(
        "run",
        "--rm",
        "--name",
        name,
        "-e",
        f"SHARD={shard}",
        "-e",
        f"RUN_ID={args.run_id}",
        "-e",
        f"JOB_FILE=/jobs/{args.jobs.relative_to(JOBS_DIR).as_posix()}",
        "-e",
        f"JOB_TIMEOUT={args.job_timeout:g}",
        "worker",
    ), name


def remove_container(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def stop(pool: Pool, index: int) -> None:
    with pool.lock:
        proc = pool.procs.get(index)
        name = pool.containers.get(index)
    if name is not None:
        # Stopping the docker CLI does not stop the container; remove it.
        remove_container(name)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_worker(args: argparse.Namespace, pool: Pool, index: int) -> Outcome:
    outcome = Outcome(index)
    if pool.stopping.is_set():
        outcome.note = "not started"
        return outcome
    command, name = worker_command(args, index)
    started = time.monotonic()
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=HERE,
    )
    with pool.lock:
        pool.procs[index] = proc
        if name is not None:
            pool.containers[index] = name
    timer = threading.Timer(args.timeout, stop, (pool, index))
    timer.daemon = True
    timer.start()
    try:
        for line in proc.stdout or ():
            say(f"[w{index}] {line.rstrip()}")
        outcome.status = proc.wait()
    finally:
        timer.cancel()
    outcome.seconds = time.monotonic() - started
    if time.monotonic() - started >= args.timeout:
        outcome.note = f"stopped after {args.timeout:g} s"
    elif pool.stopping.is_set():
        outcome.note = "interrupted"
    return outcome


def check_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    args.jobs = args.jobs.resolve()
    args.results = args.results.resolve()
    if not args.jobs.is_file():
        parser.error(f"job file not found: {args.jobs}")
    if args.mode == "docker":
        if shutil.which("docker") is None:
            parser.error("docker is not on PATH (use --mode local without Docker)")
        if not args.jobs.is_relative_to(JOBS_DIR):
            parser.error(f"docker mode reads jobs from {JOBS_DIR}; move the file there")


def summarize(args: argparse.Namespace, outcomes: list[Outcome]) -> None:
    say("")
    say(f"{'worker':<8}{'exit':>6}{'seconds':>10}  note")
    for item in outcomes:
        status = "-" if item.status is None else str(item.status)
        say(f"w{item.index:<7}{status:>6}{item.seconds:>10.1f}  {item.note}")
    run_dir = args.results / args.run_id
    records = []
    for path in sorted(run_dir.glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            say(f"unreadable result {path.name}: {exc}")
    say("")
    say(f"{'job':<24}{'status':<8}{'sec':>6}  {'click':<9}title / error")
    for record in records:
        click = (record.get("click") or {}).get("status", "-")
        detail = record.get("error") or record.get("title") or ""
        say(
            f"{record.get('job_id', '?')[:23]:<24}{record.get('status', '?'):<8}"
            f"{record.get('seconds', 0):>6.1f}  {click:<9}{detail[:60]}"
        )
    passed = sum(1 for r in records if r.get("status") == "ok")
    say(f"\n{passed}/{len(records)} job(s) ok; results in {run_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    check_args(parser, args)
    # Created here so Docker does not create the bind mount as root.
    args.results.mkdir(parents=True, exist_ok=True)
    if args.mode == "docker" and not args.no_build:
        say("building the worker image (docker compose build)")
        if subprocess.call(compose("build", "worker"), cwd=HERE) != 0:
            say("docker compose build failed")
            return 1
    say(f"run {args.run_id}: {args.workers} {args.mode} worker(s) on {args.jobs.name}")
    pool = Pool()
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(run_worker, args, pool, i) for i in range(args.workers)
        ]
        try:
            # Polled: on Windows a blocking result() does not see Ctrl+C.
            while wait(futures, timeout=0.5).not_done:
                pass
        except KeyboardInterrupt:
            say("interrupted: stopping every worker")
            pool.stopping.set()
            for index in range(args.workers):
                stop(pool, index)
        outcomes = [future.result() for future in futures]
    summarize(args, outcomes)
    return 0 if all(item.status == 0 for item in outcomes) else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
