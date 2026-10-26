# Running emunium in parallel

Templates for running many emunium browsers at once. Copy this folder, put
your URLs in `jobs/`, and run one command.

| You need | Use | Scales by |
|---|---|---|
| Real, verified mouse clicks and keystrokes | headed Docker workers (`Dockerfile.worker`, `docker-compose.yml`, `run_pool.py --mode docker`) | one container = one X display = one worker |
| Only reading pages (DOM, text, `execute_script`) | `headless_pool.py` (threads) or `run_pool.py --mode local` (processes) | any number per machine |

## Why one display per headed worker

emunium's native input is the operating system's mouse and keyboard, and a
desktop has exactly one of each. On Linux, emunium also remaps spare X11
keycodes to type Unicode text, and that remap pool belongs to one process.
Two headed workers on one display would fight over the cursor and the
keymap. Run **one headed worker per X display**: each container here starts
its own Xvfb display (`:99` inside the container) and openbox window manager,
so containers run side by side without touching each other or your desktop.

Headless browsers have no window and no native input: `click`, `type`,
`hover` and the other native-input methods raise `NativeInputError` by
design. Navigation, locators, `text()`, `count()`, `expect` and
`execute_script` all work, so many headless browsers can share one machine.

## Headed workers in Docker

```bash
cd examples/parallel
docker compose build                        # installs this checkout into the image
docker compose run --rm worker              # one worker, every job in jobs/example.json
python run_pool.py --mode docker -n 3       # three workers, jobs split 0/3, 1/3, 2/3
```

`run_pool.py` builds the image, starts N `docker compose run --rm -e SHARD=i/N
worker` containers in parallel, streams their logs with a `[wN]` prefix and
prints a table of worker exit codes and job results. Ctrl+C removes every
container. It uses only the standard library.

To install a released emunium instead of the checkout:

```bash
docker compose build --build-arg EMUNIUM_PACKAGE="emunium==3.1.0"
```

`docker compose up --scale` works too. Compose cannot give replicas a shard
number, so the replicas share the list instead: with `SHARD=claim` each job
is claimed through an exclusive file in `results/<RUN_ID>/` before it runs.
Give the batch a `RUN_ID`:

```bash
SHARD=claim RUN_ID=batch-1 docker compose up --scale worker=3
docker compose ps -a                        # exit code of each replica
```

Each container runs `entrypoint.sh`: it starts Xvfb, waits for the display
socket, starts openbox, then runs `worker.py`. `docker stop` (forwarded by
`init: true`) lets the worker close Chromium before the display goes down.
Chromium runs as the non-root user `worker` with its sandbox on: the compose
file lifts Docker's seccomp filter (`seccomp=unconfined`, which the sandbox
needs for user namespaces) and drops every capability, the same setup as the
project's Linux test container.

On a Linux host, create `results/` yourself before the first run (Docker
would create it as root), and build with your uid if it is not 1000:
`docker compose build --build-arg WORKER_UID="$(id -u)"`.

## Headless, no Docker

```bash
python headless_pool.py --threads 4                       # URLs from jobs/example.json
python headless_pool.py -t 2 https://example.com/ https://example.org/
python run_pool.py --mode local -n 2                      # N worker.py --headless processes
```

`headless_pool.py` runs one `Browser(headless=True)` per thread in a
`ThreadPoolExecutor`. That is safe because every `Browser` has its own Chrome
process, profile, extension copy and bridge (its own port, token and
event-loop thread). Keep each browser on the thread that launched it; do not
share one `Browser` between threads. `run_pool.py --mode local` starts
separate `worker.py --headless` processes instead (the click step is skipped
and reported as such).

## Jobs and results

`jobs/example.json` is a JSON list. An item is a URL or an object:

```json
[
  "https://example.com/",
  {"id": "iana-reserved", "url": "https://www.iana.org/domains/reserved", "click": false}
]
```

For each job `worker.py` opens the URL, waits for the page to settle, reads
the title and the first `h1`, and clicks the first visible link with a human,
verified mouse movement (skipped for `"click": false`, in headless mode, and
reported as skipped when the page has no link). Every job gets its own time
budget (`JOB_TIMEOUT`, default 60 s) that all of its waits share. Each job
writes `results/<RUN_ID>/<job id>.json`:

```json
{
  "job_id": "example-com", "url": "https://example.com/", "status": "ok",
  "title": "Example Domain", "final_url": "https://example.com/",
  "click": {"status": "clicked", "text": "Learn more",
            "url_after": "https://www.iana.org/help/example-domains"},
  "error": null, "seconds": 10.09
}
```

A worker exits 0 when every job passed and 1 when any failed. A browser
that crashes is relaunched with a fresh profile for the next job. Every
worker uses its own temporary profile directory, removed when it exits.

| Setting | Argument | Environment | Default |
|---|---|---|---|
| job list | `job_file` | `JOB_FILE` | `/jobs/example.json` in the image |
| result directory | `--results` | `RESULTS_DIR` | `./results` (`/results` in the image) |
| shard | `--shard i/N` or `claim` | `SHARD` | `0/1` (every job) |
| run id | `--run-id` | `RUN_ID` | a timestamp |
| time per job | `--job-timeout` | `JOB_TIMEOUT` | 60 s |
| headless | `--headless` | `HEADLESS=1` | headed |

## Sizing

- A headed Chromium on Xvfb uses about **300-500 MB RAM**; heavy pages need
  more. Budget about 1 GB of host RAM and one CPU core per worker, and
  uncomment `mem_limit` / `cpus` in `docker-compose.yml` to enforce it.
- Chromium's renderers use `/dev/shm`. Docker's 64 MB default crashes tabs,
  so the compose file sets `shm_size: "1gb"` (with plain `docker run`, pass
  `--shm-size=1g`).
- Headless browsers need a little less memory and no display, but the same
  per-browser budget is a safe start.

## Not covered

Antidetect browsers, proxies per worker and job queues (Redis, SQS, ...) are
out of scope. The worker reads a static JSON list. Replace `load_jobs` in
`worker.py` to read from a queue.
