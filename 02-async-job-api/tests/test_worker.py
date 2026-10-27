"""Tests for the worker process, driven through ``run_once`` rather than the loop."""

import io
import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app import worker as worker_module
from app.db import create_session_factory
from app.logging import configure_logging
from app.worker import STOP_EVENTS, Worker, make_worker_id, run_once
from tests.db_helpers import clear_jobs, dispose, new_session_factory, read_job, seed_job


@pytest.fixture(autouse=True)
def clean_database(migrated_engine: Engine) -> Iterator[None]:
    clear_jobs(migrated_engine)
    yield
    clear_jobs(migrated_engine)


@pytest.fixture
def database_url(migrated_engine: Engine) -> str:
    return migrated_engine.url.render_as_string(hide_password=False)


@pytest.fixture
def session_factory(database_url: str) -> Iterator[sessionmaker[Session]]:
    factory = new_session_factory(database_url)
    yield factory
    dispose(factory)


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    logger = configure_logging(stream)
    yield stream
    logger.handlers.clear()


def logged_events(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# End to end through run_once
# ---------------------------------------------------------------------------


def test_run_once_processes_echo_job(
    session_factory: sessionmaker[Session], migrated_engine: Engine, log_stream: io.StringIO
) -> None:
    payload = {"msg": "hello", "n": 1}
    job_id = seed_job(migrated_engine, job_type="echo", payload=payload)

    assert run_once(session_factory, lease_seconds=60, worker_id="worker-e2e") is True

    row = read_job(migrated_engine, job_id)
    assert row.status == "SUCCEEDED"
    assert row.result == {"echoed": payload}
    assert row.attempt == 1
    assert row.finished_at is not None
    assert row.lease_owner is None

    assert [event["event"] for event in logged_events(log_stream)] == [
        "job.claimed",
        "job.succeeded",
    ]


def test_run_once_on_empty_queue_returns_false(
    session_factory: sessionmaker[Session], log_stream: io.StringIO
) -> None:
    assert run_once(session_factory, worker_id="worker-idle") is False
    assert logged_events(log_stream) == []


def test_handler_failure_marks_job_failed_and_worker_continues(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_id = seed_job(migrated_engine, job_type="boom", payload={})
    following_id = seed_job(migrated_engine, job_type="echo", payload={"ok": True})

    def _boom(payload: dict) -> dict:
        raise RuntimeError("handler exploded")

    monkeypatch.setitem(worker_module.handlers.HANDLERS, "boom", _boom)

    assert run_once(session_factory, worker_id="worker-boom") is True
    failed = read_job(migrated_engine, failing_id)
    assert failed.status == "FAILED"
    assert failed.last_error == "RuntimeError: handler exploded"
    assert failed.finished_at is not None
    assert failed.lease_owner is None

    # The loop survives: the next job runs normally on the same worker.
    assert run_once(session_factory, worker_id="worker-boom") is True
    succeeded = read_job(migrated_engine, following_id)
    assert succeeded.status == "SUCCEEDED"
    assert succeeded.result == {"echoed": {"ok": True}}


def test_unknown_type_fails_job_without_raising(
    session_factory: sessionmaker[Session], migrated_engine: Engine, log_stream: io.StringIO
) -> None:
    job_id = seed_job(migrated_engine, job_type="no.such.handler", payload={"a": 1})

    assert run_once(session_factory, worker_id="worker-unknown") is True

    row = read_job(migrated_engine, job_id)
    assert row.status == "FAILED"
    assert row.last_error == "no handler registered for type 'no.such.handler'"
    assert row.finished_at is not None
    assert "job.failed" in [event["event"] for event in logged_events(log_stream)]


def test_long_error_message_is_truncated_to_2000_chars(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = seed_job(migrated_engine, job_type="chatty", payload={})

    def _chatty(payload: dict) -> dict:
        raise ValueError("x" * 5000)

    monkeypatch.setitem(worker_module.handlers.HANDLERS, "chatty", _chatty)
    assert run_once(session_factory, worker_id="worker-chatty") is True

    row = read_job(migrated_engine, job_id)
    assert row.status == "FAILED"
    assert len(row.last_error) == 2000
    assert row.last_error.startswith("ValueError: ")
    assert "Traceback" not in row.last_error


def test_lease_lost_on_completion_is_logged_not_retried(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the lease is taken over mid-handler, completion logs job.lease_lost and stops."""
    job_id = seed_job(migrated_engine, job_type="steal", payload={})

    def _steal(payload: dict) -> dict:
        with migrated_engine.connect() as conn:
            conn.execute(
                text("UPDATE jobs SET lease_owner = 'worker-thief' WHERE id = :id"),
                {"id": job_id},
            )
            conn.commit()
        return {"ignored": True}

    monkeypatch.setitem(worker_module.handlers.HANDLERS, "steal", _steal)

    assert run_once(session_factory, worker_id="worker-original") is True

    row = read_job(migrated_engine, job_id)
    assert row.status == "RUNNING"
    assert row.lease_owner == "worker-thief"
    assert row.result is None
    assert row.finished_at is None

    events = logged_events(log_stream)
    assert events[-1]["event"] == "job.lease_lost"
    assert events[-1]["job_id"] == str(job_id)
    assert events[-1]["worker_id"] == "worker-original"
    assert events[-1]["level"] == "warning"


def test_claim_database_error_does_not_raise(log_stream: io.StringIO) -> None:
    """A dead database during claim is survivable: the loop retries, it does not exit."""
    engine = create_engine("postgresql+psycopg://postgres:postgres@127.0.0.1:19999/nope")
    try:
        assert run_once(create_session_factory(engine), worker_id="worker-dead") is False
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Loop behaviour and shutdown
# ---------------------------------------------------------------------------


def _wait_until(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_run_forever_drains_queue_and_stops(
    session_factory: sessionmaker[Session], migrated_engine: Engine, log_stream: io.StringIO
) -> None:
    ids = [seed_job(migrated_engine, payload={"i": index}) for index in range(3)]
    stop = threading.Event()
    worker = Worker(session_factory, lease_seconds=60, poll_seconds=0.01, worker_id="worker-loop")

    thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    try:
        assert _wait_until(lambda: _count(migrated_engine, "SUCCEEDED") == len(ids)), (
            "worker loop did not drain the queue"
        )
    finally:
        stop.set()
        thread.join(timeout=10)

    assert not thread.is_alive(), "worker loop did not stop after the stop event was set"
    for job_id in ids:
        assert read_job(migrated_engine, job_id).status == "SUCCEEDED"
    events = [event["event"] for event in logged_events(log_stream)]
    assert events[0] == "worker.started"
    assert events[-1] == "worker.stopped"


def test_stop_signal_finishes_the_job_in_hand(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop request during a handler must not abandon the claimed job."""
    job_id = seed_job(migrated_engine, job_type="slow", payload={"slow": True})
    stop = threading.Event()
    handler_entered = threading.Event()

    def _slow(payload: dict) -> dict:
        handler_entered.set()
        # Give the loop a chance to observe the stop while we are mid-job.
        time.sleep(0.05)
        return {"done": True}

    monkeypatch.setitem(worker_module.handlers.HANDLERS, "slow", _slow)

    worker = Worker(session_factory, lease_seconds=60, poll_seconds=0.01, worker_id="worker-stop")
    thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    try:
        assert handler_entered.wait(timeout=10)
        stop.set()
    finally:
        thread.join(timeout=10)

    assert not thread.is_alive()
    row = read_job(migrated_engine, job_id)
    assert row.status == "SUCCEEDED", "a claimed job was abandoned when the worker was stopped"
    assert row.result == {"done": True}
    assert row.lease_owner is None


def _count(engine: Engine, status: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM jobs WHERE status = :status"), {"status": status}
        ).scalar_one()


def test_stop_events_cover_sigterm_and_sigint() -> None:
    import signal

    assert set(STOP_EVENTS) == {signal.SIGTERM, signal.SIGINT}


# ---------------------------------------------------------------------------
# Logging contract
# ---------------------------------------------------------------------------


def test_claim_logs_one_json_object_with_required_keys_and_no_payload(
    session_factory: sessionmaker[Session], migrated_engine: Engine, log_stream: io.StringIO
) -> None:
    secret = "client-secret-value-abc123"
    seed_job(migrated_engine, job_type="echo", payload={"token": secret})

    run_once(session_factory, lease_seconds=60, worker_id="worker-log")

    output = log_stream.getvalue()
    assert secret not in output, "payload text must never reach the logs"

    claim_events = [event for event in logged_events(log_stream) if event["event"] == "job.claimed"]
    assert len(claim_events) == 1
    claimed = claim_events[0]
    for key in ("event", "job_id", "job_type", "attempt", "worker_id", "lease_expires_at"):
        assert key in claimed, f"job.claimed is missing {key}"
    assert claimed["worker_id"] == "worker-log"
    assert claimed["attempt"] == 1


def test_result_values_are_not_logged(
    session_factory: sessionmaker[Session], migrated_engine: Engine, log_stream: io.StringIO
) -> None:
    marker = "result-marker-xyz789"
    seed_job(migrated_engine, job_type="echo", payload={"v": marker})

    run_once(session_factory, worker_id="worker-result")
    assert marker not in log_stream.getvalue()


def test_every_event_carries_worker_id(
    session_factory: sessionmaker[Session], migrated_engine: Engine, log_stream: io.StringIO
) -> None:
    seed_job(migrated_engine)
    run_once(session_factory, worker_id="worker-keys")
    events = logged_events(log_stream)
    assert events
    for event in events:
        assert event.get("worker_id") == "worker-keys", event


# ---------------------------------------------------------------------------
# Worker identity
# ---------------------------------------------------------------------------


def test_worker_id_is_host_pid_and_suffix() -> None:
    worker_id = make_worker_id()
    host, pid, suffix = worker_id.rsplit("-", 2)
    assert host
    assert pid.isdigit()
    assert len(suffix) == 8
