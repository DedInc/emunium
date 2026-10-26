# examples/parallel/headless_pool.py
"""DOM-only scraping with a thread pool of headless emunium browsers.

Every thread owns one ``Browser(headless=True)`` for its whole life and pulls
URLs from a shared queue. That is safe: each ``Browser`` has its own Chrome
process, profile, extension copy and WebSocket bridge (with its own port,
token and event-loop thread), and a browser is only ever used by the thread
that launched it. Do not hand one ``Browser`` to several threads.

Headless browsers have no window, so every native-input method (``click``,
``type``, ``hover``, ...) raises ``NativeInputError`` by design. Reading the
page (``goto``, locators, ``text()``, ``count()``, ``expect``,
``execute_script``) works. For real clicks at scale use the headed Docker
workers in this folder instead (one X display per worker).

Usage::

    python headless_pool.py                         # jobs/example.json, 2 threads
    python headless_pool.py --threads 4 https://example.com/ https://example.org/
    python headless_pool.py --json results.json     # also write the records

Exit status: 0 when every URL was read, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from emunium import Browser, EmuniumError, WaitTimeoutError

HERE = Path(__file__).resolve().parent
DEFAULT_JOBS = HERE / "jobs" / "example.json"


def load_urls(path: Path) -> list[str]:
    """URLs of a job list in worker.py's format (strings or ``{"url": ...}``)."""
    items = json.loads(path.read_text(encoding="utf-8"))
    return [item if isinstance(item, str) else item["url"] for item in items]


def read_page(browser: Browser, url: str, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    record: dict[str, Any] = {"url": url, "thread": threading.current_thread().name}
    try:
        info = browser.goto(url, timeout=timeout)
        try:
            h1 = browser.locator("h1").text(timeout=5).strip()
        except WaitTimeoutError:
            h1 = None
        record.update(
            status="ok",
            final_url=info.url,
            title=browser.title,
            h1=h1,
            links=browser.get_by_role("link").count(),
            error=None,
        )
    except (EmuniumError, TimeoutError) as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    record["seconds"] = round(time.monotonic() - started, 2)
    return record


def crawl(urls: queue.Queue[str], timeout: float) -> list[dict[str, Any]]:
    """One thread: launch a browser, drain the queue, close the browser."""
    records: list[dict[str, Any]] = []
    browser = Browser(headless=True)
    # launch() and close() explicitly: `with Browser(...)` would launch again.
    try:
        browser.launch()
    except EmuniumError as exc:
        # This thread's browser did not start; the other threads keep going.
        print(f"{threading.current_thread().name}: launch failed: {exc}", flush=True)
        return records
    try:
        while True:
            try:
                url = urls.get_nowait()
            except queue.Empty:
                break
            record = read_page(browser, url, timeout)
            print(
                f"{record['thread']}: {record['status']:<6} {url} ({record['seconds']} s)",
                flush=True,
            )
            records.append(record)
    finally:
        browser.close()
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read pages with a thread pool of headless emunium browsers."
    )
    parser.add_argument("urls", nargs="*", help="URLs (default: jobs/example.json)")
    parser.add_argument("--threads", "-t", type=int, default=2, help="browsers")
    parser.add_argument("--jobs", type=Path, default=DEFAULT_JOBS, help="job list")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per page")
    parser.add_argument("--json", type=Path, help="write the records to this file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    urls = args.urls or load_urls(args.jobs)
    threads = max(1, min(args.threads, len(urls)))
    pending: queue.Queue[str] = queue.Queue()
    for url in urls:
        pending.put(url)

    with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="browser") as pool:
        futures = [pool.submit(crawl, pending, args.timeout) for _ in range(threads)]
        records = [record for future in futures for record in future.result()]

    print()
    for record in records:
        detail = record["error"] or f"{record['title']!r} h1={record['h1']!r}"
        print(f"{record['status']:<7}{record['url']:<45}{detail}")
    done = sum(1 for record in records if record["status"] == "ok")
    print(f"\n{done}/{len(urls)} page(s) read with {threads} headless browser(s)")
    if args.json:
        args.json.write_text(json.dumps(records, indent=2, ensure_ascii=False), "utf-8")
    return 0 if done == len(urls) else 1


if __name__ == "__main__":
    sys.exit(main())
