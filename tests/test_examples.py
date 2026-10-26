"""Static checks of the examples: nothing here launches a browser or a container.

- every example script compiles;
- worker.py and run_pool.py import, and their argument parsing and job
  handling work without starting anything;
- the compose file parses (PyYAML, skipped when absent) and passes
  ``docker compose config --quiet`` (skipped when docker is absent);
- the worker image runs entrypoint.sh as a non-root user.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
PARALLEL = EXAMPLES / "parallel"
COMPOSE = PARALLEL / "docker-compose.yml"
DOCKERFILE = PARALLEL / "Dockerfile.worker"
JOBS = PARALLEL / "jobs" / "example.json"
SCRIPTS = sorted(EXAMPLES.rglob("*.py"))


def load(name: str) -> ModuleType:
    """Import ``examples/parallel/<name>.py`` under a private module name."""
    module_name = f"_example_parallel_{name}"
    spec = importlib.util.spec_from_file_location(module_name, PARALLEL / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[module_name]
        raise
    return module


@pytest.fixture(scope="module")
def worker() -> ModuleType:
    return load("worker")


@pytest.fixture(scope="module")
def run_pool() -> ModuleType:
    return load("run_pool")


def test_examples_exist() -> None:
    names = {path.name for path in SCRIPTS}
    assert {"worker.py", "run_pool.py", "headless_pool.py"} <= names


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_example_compiles(script: Path, tmp_path: Path) -> None:
    py_compile.compile(str(script), cfile=str(tmp_path / "out.pyc"), doraise=True)


def test_headless_pool_imports_and_parses() -> None:
    module = load("headless_pool")
    args = module.build_parser().parse_args(["--threads", "3", "https://example.com/"])
    assert args.threads == 3 and args.urls == ["https://example.com/"]
    assert module.load_urls(JOBS)[0] == "https://example.com/"


def test_worker_parses_arguments(worker: ModuleType, monkeypatch) -> None:
    for name in (
        "JOB_FILE",
        "RESULTS_DIR",
        "SHARD",
        "RUN_ID",
        "JOB_TIMEOUT",
        "HEADLESS",
    ):
        monkeypatch.delenv(name, raising=False)
    args = worker.build_parser().parse_args(
        [str(JOBS), "--shard", "1/3", "--run-id", "a b/c", "--headless"]
    )
    assert args.job_file == JOBS
    assert args.shard == worker.Shard(1, 3)
    assert args.run_id == "a-b-c"
    assert args.headless is True
    assert args.job_timeout == 60.0
    assert args.results == Path("results")


def test_worker_reads_environment(worker: ModuleType, monkeypatch) -> None:
    monkeypatch.setenv("JOB_FILE", str(JOBS))
    monkeypatch.setenv("SHARD", "claim")
    monkeypatch.setenv("JOB_TIMEOUT", "12.5")
    monkeypatch.setenv("HEADLESS", "1")
    args = worker.build_parser().parse_args([])
    assert args.job_file == JOBS
    assert args.shard.claim
    assert args.job_timeout == 12.5
    assert args.headless is True


@pytest.mark.parametrize("text", ["3/3", "-1/2", "1/0", "x", "1/2/3"])
def test_worker_rejects_bad_shard(worker: ModuleType, text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        worker.parse_shard(text)


def test_worker_without_job_file_is_a_usage_error(
    worker: ModuleType, monkeypatch
) -> None:
    monkeypatch.delenv("JOB_FILE", raising=False)
    with pytest.raises(SystemExit) as exc:
        worker.main([])
    assert exc.value.code == 2


def test_worker_shards_cover_the_job_list(worker: ModuleType) -> None:
    jobs = worker.load_jobs(JOBS)
    assert 3 <= len(jobs) <= 5
    assert all(job.url.startswith("https://") for job in jobs)
    shards = [worker.Shard(i, 2).select(jobs) for i in range(2)]
    assert sorted(job.job_id for shard in shards for job in shard) == sorted(
        job.job_id for job in jobs
    )
    assert worker.Shard(claim=True).select(jobs) == jobs


def test_worker_rejects_bad_job_lists(worker: ModuleType, tmp_path: Path) -> None:
    for content in ('{"url": "https://example.com/"}', '[{"id": "x"}]', '["ftp://x"]'):
        bad = tmp_path / "bad.json"
        bad.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            worker.load_jobs(bad)


def test_worker_deadline_runs_out(worker: ModuleType) -> None:
    deadline = worker.Deadline(30)
    assert deadline.left(5) == 5
    with pytest.raises(TimeoutError):
        worker.Deadline(0).left()


def test_job_file_is_valid_json() -> None:
    items = json.loads(JOBS.read_text(encoding="utf-8"))
    assert isinstance(items, list) and items


def test_run_pool_builds_commands(run_pool: ModuleType) -> None:
    parser = run_pool.build_parser()
    local = parser.parse_args(["--mode", "local", "-n", "3", "--run-id", "r1"])
    command, name = run_pool.worker_command(local, 2)
    assert name is None
    assert command[1].endswith("worker.py")
    assert ["--headless", "--shard", "2/3"] == command[3:6]

    docker = parser.parse_args(["--mode", "docker", "-n", "2", "--run-id", "r1"])
    command, name = run_pool.worker_command(docker, 1)
    assert name == "emunium-pool-r1-1"
    assert command[:2] == ["docker", "compose"]
    assert "SHARD=1/2" in command and "JOB_FILE=/jobs/example.json" in command
    assert command[-1] == "worker"


def test_run_pool_rejects_jobs_outside_the_mount(
    run_pool: ModuleType, tmp_path
) -> None:
    outside = tmp_path / "jobs.json"
    outside.write_text("[]", encoding="utf-8")
    parser = run_pool.build_parser()
    args = parser.parse_args(["--mode", "docker", "-n", "1", "--jobs", str(outside)])
    with pytest.raises(SystemExit):
        run_pool.check_args(parser, args)
    with pytest.raises(SystemExit):
        run_pool.check_args(parser, parser.parse_args(["--mode", "local", "-n", "0"]))


def test_compose_file_parses() -> None:
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service = config["services"]["worker"]
    assert service["build"]["context"] == "../.."
    assert service["build"]["dockerfile"] == "examples/parallel/Dockerfile.worker"
    assert service["init"] is True
    assert service["shm_size"] == "1gb"
    assert "./jobs:/jobs:ro" in service["volumes"]
    assert "./results:/results" in service["volumes"]
    # --scale needs compose-generated names.
    assert "container_name" not in service


def test_compose_config_is_valid() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is not on PATH")
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--quiet"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0 and "is not a docker command" in result.stderr:
        pytest.skip("the docker compose plugin is not installed")
    assert result.returncode == 0, result.stderr


def test_dockerfile_runs_entrypoint_as_non_root() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "entrypoint.sh" in text
    users = [
        line.split()[1]
        for line in text.splitlines()
        if line.strip().upper().startswith("USER ")
    ]
    assert users and users[-1] not in {"root", "0"}
    assert (
        (PARALLEL / "entrypoint.sh").read_text(encoding="utf-8").startswith("#!/bin/sh")
    )
