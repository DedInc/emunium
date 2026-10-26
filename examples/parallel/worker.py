# examples/parallel/worker.py
"""One emunium worker: run a list of page jobs in one browser, one JSON result each.

A job file is a JSON list. Each item is a URL string or an object::

    [
      "https://example.com/",
      {"id": "iana-reserved", "url": "https://www.iana.org/domains/reserved",
       "click": false}
    ]

For every job the worker opens the URL, waits for the page to settle, reads
the title and the first ``h1``, and (unless ``"click": false`` or
``--headless``) clicks the first visible link with a real, verified, human
mouse movement. A page without a link is reported as a skipped click, not as
a failure. The result goes to ``RESULTS_DIR/RUN_ID/<job id>.json``.

Headed (the default) needs a display of its own: native input is per desktop
and emunium's X11 key remapping is per process, so run one headed worker per
X display (the container in this folder brings its own). ``--headless`` needs
no display and skips the click, since a headless browser refuses native input.

Configuration comes from arguments, falling back to the environment:

=================  ==============  =========================================
argument           environment     meaning
=================  ==============  =========================================
``job_file``       ``JOB_FILE``    JSON job list
``--results``      ``RESULTS_DIR`` result directory (default ``./results``)
``--shard i/N``    ``SHARD``       take jobs ``i, i+N, ...`` (default 0/1);
                                   ``claim``: share the list with other
                                   workers of the same ``RUN_ID`` by claiming
                                   each job through an exclusive file
``--run-id``       ``RUN_ID``      results sub-directory (default: a timestamp)
``--job-timeout``  ``JOB_TIMEOUT`` seconds per job (default 60)
``--headless``     ``HEADLESS=1``  headless browser, no click
=================  ==============  =========================================

Exit status: 0 when every job passed, 1 when any failed, 2 on bad input,
143 when stopped by SIGTERM.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import socket
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from emunium import (
    BridgeError,
    Browser,
    EmuniumError,
    LaunchError,
    NavigationTimeoutError,
    TargetClosedError,
    WaitTimeoutError,
)

LOG = logging.getLogger("worker")
CLAIM = "claim"
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_TERMINATED = 143


class JobFileError(ValueError):
    """The job file is unreadable or a job in it is malformed."""


class JobTimeout(TimeoutError):
    """The job used up its time budget."""


@dataclass(frozen=True)
class Job:
    job_id: str
    url: str
    click: bool


@dataclass(frozen=True)
class Shard:
    """Which jobs of the list this worker takes."""

    index: int = 0
    count: int = 1
    claim: bool = False

    def __str__(self) -> str:
        return CLAIM if self.claim else f"{self.index}/{self.count}"

    def select(self, jobs: list[Job]) -> list[Job]:
        if self.claim:
            return list(jobs)
        return jobs[self.index :: self.count]


class Deadline:
    """A job's time budget; every wait inside the job gets what is left."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds
        self.seconds = seconds

    def left(self, cap: float | None = None) -> float:
        remaining = self._end - time.monotonic()
        if remaining <= 0:
            raise JobTimeout(f"job exceeded its {self.seconds:g} s budget")
        return remaining if cap is None else min(cap, remaining)


def parse_shard(text: str) -> Shard:
    """``"i/N"`` with ``0 <= i < N``, or ``"claim"``."""
    text = text.strip()
    if text == CLAIM:
        return Shard(claim=True)
    match = re.fullmatch(r"(\d+)/(\d+)", text)
    if match is None:
        raise argparse.ArgumentTypeError(
            f"shard must be 'i/N' or 'claim', not {text!r}"
        )
    index, count = int(match.group(1)), int(match.group(2))
    if count < 1 or index >= count:
        raise argparse.ArgumentTypeError(f"shard {text!r} needs 0 <= i < N")
    return Shard(index, count)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:80] or "job"


def load_jobs(path: Path) -> list[Job]:
    """Read and validate a job list; raises JobFileError naming the bad item."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise JobFileError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(raw, list):
        raise JobFileError(f"{path}: expected a JSON list of jobs")
    jobs: list[Job] = []
    seen: set[str] = set()
    for position, item in enumerate(raw):
        spec = {"url": item} if isinstance(item, str) else item
        if not isinstance(spec, dict) or not isinstance(spec.get("url"), str):
            raise JobFileError(f"{path}: job {position} needs a 'url' string")
        url = spec["url"]
        if not url.startswith(("http://", "https://")):
            raise JobFileError(f"{path}: job {position} url must be http(s): {url!r}")
        job_id = _slug(str(spec.get("id") or f"{position:03d}-{url.split('//', 1)[1]}"))
        if job_id in seen:
            raise JobFileError(f"{path}: duplicate job id {job_id!r}")
        seen.add(job_id)
        jobs.append(Job(job_id, url, bool(spec.get("click", True))))
    return jobs


def build_parser() -> argparse.ArgumentParser:
    env = os.environ
    parser = argparse.ArgumentParser(
        description="Run page jobs in one emunium browser; one JSON result per job."
    )
    parser.add_argument(
        "job_file",
        nargs="?",
        type=Path,
        default=env.get("JOB_FILE"),
        help="JSON job list (env JOB_FILE)",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=env.get("RESULTS_DIR", "results"),
        help="result directory (env RESULTS_DIR, default ./results)",
    )
    parser.add_argument(
        "--shard",
        type=parse_shard,
        default=env.get("SHARD", "0/1"),
        help="'i/N' to take every N-th job from i, or 'claim' (env SHARD)",
    )
    parser.add_argument(
        "--run-id",
        type=_slug,
        default=env.get("RUN_ID") or datetime.now().strftime("%Y%m%d-%H%M%S"),
        help="results sub-directory (env RUN_ID, default a timestamp)",
    )
    parser.add_argument(
        "--job-timeout",
        type=float,
        default=env.get("JOB_TIMEOUT", "60"),
        help="seconds per job (env JOB_TIMEOUT, default 60)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=env.get("HEADLESS", "") in {"1", "true", "yes"},
        help="headless browser; the click step is skipped (env HEADLESS=1)",
    )
    return parser


def _claim(run_dir: Path, job: Job) -> bool:
    """True if this worker created the job's claim file first."""
    try:
        fd = os.open(
            run_dir / f"{job.job_id}.claim", os.O_CREAT | os.O_EXCL | os.O_WRONLY
        )
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{socket.gethostname()} {os.getpid()}\n")
    return True


def _error(exc: BaseException) -> str:
    text = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {text}"


def click_first_link(browser: Browser, deadline: Deadline) -> dict[str, Any]:
    """Click the first visible link with a human, verified movement.

    No link on the page is a skipped click; a click that fails raises.
    """
    links = browser.get_by_role("link").filter(visible=True)
    if links.count() == 0:
        return {"status": "skipped", "reason": "no visible link on the page"}
    target = links.first
    label = target.text(timeout=deadline.left(5)).strip()[:80]
    href = target.get_attribute("href", timeout=deadline.left(5))
    before = browser.url
    target.click(human=True, timeout=deadline.left(10))
    try:
        after = browser.wait_for_url(
            lambda url: url != before, timeout=deadline.left(15), wait_until="load"
        )
    except NavigationTimeoutError:
        after = None
    return {"status": "clicked", "text": label, "href": href, "url_after": after}


def run_job(
    browser: Browser, job: Job, deadline: Deadline, headless: bool
) -> dict[str, Any]:
    info = browser.goto(job.url, timeout=deadline.left(30), wait_until="load")
    browser.wait_for_idle(silence=0.5, timeout=deadline.left(10))
    result: dict[str, Any] = {"final_url": info.url, "title": browser.title}
    try:
        result["h1"] = browser.locator("h1").text(timeout=deadline.left(5)).strip()
    except WaitTimeoutError:
        result["h1"] = None
    if not job.click:
        result["click"] = {"status": "skipped", "reason": "click disabled for this job"}
    elif headless:
        result["click"] = {"status": "skipped", "reason": "headless: no native input"}
    else:
        result["click"] = click_first_link(browser, deadline)
    deadline.left()
    return result


class BrowserSlot:
    """The worker's browser, relaunched with a fresh profile after a crash."""

    def __init__(self, profile_root: Path, headless: bool) -> None:
        self._profile_root = profile_root
        self._headless = headless
        self._launches = 0
        self.browser: Browser | None = None

    def get(self) -> Browser:
        if self.browser is None:
            self._launches += 1
            profile = self._profile_root / f"profile-{self._launches}"
            LOG.info("launching browser (profile %s)", profile)
            self.browser = Browser(headless=self._headless, user_data_dir=str(profile))
            self.browser.launch()
        return self.browser

    def discard(self) -> None:
        if self.browser is not None:
            try:
                self.browser.close()
            except EmuniumError as exc:
                LOG.warning("closing the browser failed: %s", _error(exc))
            self.browser = None


def _write_result(run_dir: Path, record: dict[str, Any]) -> None:
    target = run_dir / f"{record['job_id']}.json"
    partial = target.with_suffix(".json.part")
    partial.write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(partial, target)


def run(args: argparse.Namespace) -> int:
    jobs = args.shard.select(load_jobs(args.job_file))
    run_dir: Path = args.results / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    LOG.info(
        "shard %s: %d job(s), results in %s, %s",
        args.shard,
        len(jobs),
        run_dir,
        "headless"
        if args.headless
        else f"headed on DISPLAY={os.environ.get('DISPLAY', '-')}",
    )
    failures = 0
    done = 0
    with tempfile.TemporaryDirectory(prefix="emunium-worker-") as profile_root:
        slot = BrowserSlot(Path(profile_root), args.headless)
        try:
            for job in jobs:
                if args.shard.claim and not _claim(run_dir, job):
                    continue
                done += 1
                started = time.monotonic()
                record: dict[str, Any] = {
                    "run_id": args.run_id,
                    "job_id": job.job_id,
                    "url": job.url,
                    "worker": socket.gethostname(),
                    "shard": str(args.shard),
                    "headless": args.headless,
                    "started_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                }
                try:
                    deadline = Deadline(args.job_timeout)
                    record.update(run_job(slot.get(), job, deadline, args.headless))
                    record.update(status="ok", error=None)
                except (EmuniumError, TimeoutError, AssertionError) as exc:
                    failures += 1
                    record.update(status="failed", error=_error(exc))
                    # The browser itself is gone or never came up: start over.
                    if isinstance(exc, (BridgeError, TargetClosedError, LaunchError)):
                        slot.discard()
                record["seconds"] = round(time.monotonic() - started, 2)
                _write_result(run_dir, record)
                LOG.info(
                    "%-6s %s %s (%.1f s)%s",
                    record["status"],
                    job.job_id,
                    record.get("title", ""),
                    record["seconds"],
                    f" - {record['error']}" if record["error"] else "",
                )
        finally:
            slot.discard()
    LOG.info("finished: %d job(s), %d failed", done, failures)
    return EXIT_FAILED if failures else EXIT_OK


def _on_sigterm(signum: int, frame: object) -> None:
    # SystemExit unwinds through the `finally` blocks, so Chrome and the
    # temporary profile are removed before the process ends.
    raise SystemExit(EXIT_TERMINATED)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.job_file is None:
        parser.error("no job file: pass a path or set JOB_FILE")
    if args.job_timeout <= 0:
        parser.error("--job-timeout must be positive")
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s worker[{args.shard}] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        return run(args)
    except (OSError, ValueError) as exc:
        LOG.error("cannot run: %s", exc)
        return EXIT_USAGE
    except KeyboardInterrupt:
        LOG.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
