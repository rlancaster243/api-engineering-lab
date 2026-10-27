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


def test_handler_failure_retries_then_terminal_fails(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising handler requeues while attempts remain, and fails once exhausted.

    max_attempts=1 so the single failed attempt is the last one: the job must land
    FAILED rather than be requeued.
    """
    failing_id = seed_job(migrated_engine, job_type="boom", payload={}, max_attempts=1)
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
    job_id = seed_job(migrated_engine, job_type="no.such.handler", payload={"a": 1}, max_attempts=1)

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
    job_id = seed_job(migrated_engine, job_type="chatty", payload={}, max_attempts=1)

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


def test_non_database_error_during_claim_does_not_kill_the_worker(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any exception from a cycle is survivable, not just ``SQLAlchemyError``.

    A ``TypeError`` escaping the claim used to propagate out of ``run_forever`` and
    kill the process, so the queue stopped draining.
    """
    job_id = seed_job(migrated_engine)

    def _broken_claim(session: Session, worker_id: str, lease_seconds: int) -> Any:
        raise TypeError("claim blew up in a way SQLAlchemy knows nothing about")

    monkeypatch.setattr(worker_module.repository, "claim_next_job", _broken_claim)

    assert run_once(session_factory, worker_id="worker-typeerror") is False

    errors = [event for event in logged_events(log_stream) if event["level"] == "error"]
    assert errors, "a surviving cycle failure must still be logged"
    assert errors[-1]["event"] == "worker.cycle_failed"
    assert errors[-1]["error_class"] == "TypeError"
    assert "exception" not in errors[-1], "a cycle failure must not log a traceback"

    monkeypatch.undo()
    assert run_once(session_factory, worker_id="worker-recovered") is True
    assert read_job(migrated_engine, job_id).status == "SUCCEEDED"


def test_claim_failure_message_never_reaches_the_logs(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret in a claim exception must not reach the logs via a traceback.

    A claim failure can surface a row value — a decode error quoting a payload
    field, for example — so the cycle-failure path logs the class alone and never
    a formatted traceback.
    """
    secret = "row-decode-key-9f3c1a7b"

    def _broken_claim(session: Session, worker_id: str, lease_seconds: int) -> Any:
        raise RuntimeError(f"decode failed for row with key {secret}")

    monkeypatch.setattr(worker_module.repository, "claim_next_job", _broken_claim)

    assert run_once(session_factory, worker_id="worker-claim-leak") is False

    output = log_stream.getvalue()
    assert secret not in output, "a claim exception message reached the logs"
    assert "decode failed for row" not in output, "the message reached the logs"

    records = logged_events(log_stream)
    assert records, "the surviving failure must still be logged"
    assert records[-1]["event"] == "worker.cycle_failed"
    assert records[-1]["error_class"] == "RuntimeError"
    assert "exception" not in records[-1], "a cycle failure must not log a traceback"


def test_run_forever_survives_a_non_database_claim_error(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop itself keeps polling after a non-SQLAlchemy claim failure."""
    job_id = seed_job(migrated_engine)
    stop = threading.Event()
    failures = 0
    real_claim = worker_module.repository.claim_next_job

    def _flaky_claim(session: Session, worker_id: str, lease_seconds: int) -> Any:
        nonlocal failures
        if failures == 0:
            failures += 1
            raise TypeError("first poll raises")
        return real_claim(session, worker_id, lease_seconds)

    monkeypatch.setattr(worker_module.repository, "claim_next_job", _flaky_claim)

    worker = Worker(session_factory, lease_seconds=60, poll_seconds=0.01, worker_id="worker-flaky")
    thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    try:
        assert _wait_until(lambda: _count(migrated_engine, "SUCCEEDED") == 1), (
            "the loop died on a non-SQLAlchemy claim error"
        )
    finally:
        stop.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert read_job(migrated_engine, job_id).status == "SUCCEEDED"


# ---------------------------------------------------------------------------
# Handler result contract
# ---------------------------------------------------------------------------


def test_non_dict_handler_result_fails_job_and_worker_survives(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler returning a list must fail the job, not strand it RUNNING.

    Under PR-2-03 semantics "fail" means the failure is recorded, so a non-object
    result consumes the attempt budget: the first failure requeues, and the job
    terminates FAILED once the budget is gone. Either way the row never sits
    RUNNING with a live lease, and the next job still runs.
    """
    bad_id = seed_job(migrated_engine, job_type="returns.list", payload={})
    following_id = seed_job(migrated_engine, payload={"ok": True})

    def _returns_list(payload: dict) -> Any:
        return [1, 2, 3]

    monkeypatch.setitem(worker_module.handlers.HANDLERS, "returns.list", _returns_list)

    assert run_once(session_factory, worker_id="worker-badresult") is True

    requeued = read_job(migrated_engine, bad_id)
    assert requeued.status == "PENDING", "a non-object result must be recorded, not ignored"
    assert requeued.last_error == "invalid handler result: expected dict, got list"
    assert requeued.result is None
    assert requeued.finished_at is None
    assert requeued.lease_owner is None

    # The healthy job is served while the bad one is backed off.
    assert run_once(session_factory, worker_id="worker-badresult") is True
    assert read_job(migrated_engine, following_id).status == "SUCCEEDED"

    # Clear the backoff and spend the remaining attempts: the job must terminate.
    for _ in range(2):
        with migrated_engine.connect() as conn:
            conn.execute(
                text("UPDATE jobs SET next_attempt_at = now() WHERE id = :id"), {"id": bad_id}
            )
            conn.commit()
        assert run_once(session_factory, worker_id="worker-badresult") is True

    exhausted = read_job(migrated_engine, bad_id)
    assert exhausted.status == "FAILED"
    assert exhausted.finished_at is not None
    assert exhausted.lease_owner is None


def test_non_serialisable_result_fails_job_instead_of_stranding_it(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dict holding a set cannot reach jsonb; the job must land FAILED, not RUNNING."""
    job_id = seed_job(migrated_engine, job_type="returns.set", payload={})

    def _returns_set(payload: dict) -> dict:
        return {"values": {1, 2, 3}}

    monkeypatch.setitem(worker_module.handlers.HANDLERS, "returns.set", _returns_set)

    assert run_once(session_factory, worker_id="worker-unserialisable") is True

    row = read_job(migrated_engine, job_id)
    assert row.status == "FAILED", "an unstorable result left the job RUNNING"
    assert row.result is None
    assert row.lease_owner is None
    assert row.finished_at is not None
    assert row.last_error is not None
    assert row.last_error.startswith("completion failed: TypeError")

    failed_events = [
        event for event in logged_events(log_stream) if event.get("event") == "job.failed"
    ]
    assert len(failed_events) == 1
    assert failed_events[0]["error_class"] == "TypeError"


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


def test_stop_between_claim_and_handler_does_not_abandon_the_job(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The riskiest window is between claiming and running: a stop landing there must
    not abandon a claimed job.

    Driven through ``run_forever`` because that is where the stop event is consulted.
    The stop is requested from inside ``_claim`` — after the row is already
    ``RUNNING`` and owned by this worker — so an implementation that checks the stop
    flag before running the handler would leave the job stranded.
    """
    job_id = seed_job(migrated_engine, payload={"must": "complete"})
    stop = threading.Event()
    worker = Worker(session_factory, lease_seconds=60, poll_seconds=0.01, worker_id="worker-mid")

    real_claim = worker._claim

    def claim_then_request_stop() -> Any:
        job = real_claim()
        stop.set()
        return job

    monkeypatch.setattr(worker, "_claim", claim_then_request_stop)

    thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()

    row = read_job(migrated_engine, job_id)
    assert row.status == "SUCCEEDED", (
        "a job claimed before the stop request was abandoned instead of finished"
    )
    assert row.result == {"echoed": {"must": "complete"}}
    assert row.lease_owner is None


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


def test_handler_failure_message_never_reaches_the_logs(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret quoted in an exception message stays out of stdout, but not out of the row.

    The message is handler-authored text: it is the operator's debugging signal in
    ``jobs.last_error`` and must never be written to a log line. Checked on both
    failure events: ``job.retrying`` while the budget lasts and ``job.failed`` at
    the end, since PR-2-03 sends a handler failure through the retry path.
    """
    secret = "sk-live-9f3c1a7b2e"
    job_id = seed_job(
        migrated_engine, job_type="leaky", payload={"api_key": secret}, max_attempts=1
    )

    def _leaky(payload: dict) -> dict:
        raise RuntimeError(f"rejected key {payload['api_key']}")

    monkeypatch.setitem(worker_module.handlers.HANDLERS, "leaky", _leaky)

    assert run_once(session_factory, worker_id="worker-leaky") is True

    output = log_stream.getvalue()
    assert secret not in output, "an exception message reached the logs"
    assert "rejected key" not in output, "handler message text reached the logs"

    row = read_job(migrated_engine, job_id)
    assert row.status == "FAILED"
    assert row.last_error == f"RuntimeError: rejected key {secret}"

    failed_events = [
        event for event in logged_events(log_stream) if event.get("event") == "job.failed"
    ]
    assert len(failed_events) == 1
    assert failed_events[0]["error_class"] == "RuntimeError"
    assert failed_events[0]["last_error_chars"] == len(row.last_error)
    assert "last_error" not in failed_events[0]


def test_handler_failure_retry_event_carries_class_and_length_only(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The requeue event must obey the same rule as the terminal one.

    PR-2-03 made a handler failure requeue while budget remains, which moved the
    failure event from ``job.failed`` to ``job.retrying``. The security contract
    does not move with it: ``job.retrying`` reports class and message length, and
    the message stays on the row.
    """
    secret = "sk-live-retry-77c2"
    job_id = seed_job(migrated_engine, job_type="leaky-retry", payload={"api_key": secret})

    def _leaky(payload: dict) -> dict:
        raise RuntimeError(f"rejected key {payload['api_key']}")

    monkeypatch.setitem(worker_module.handlers.HANDLERS, "leaky-retry", _leaky)

    assert run_once(session_factory, worker_id="worker-leaky-retry") is True

    output = log_stream.getvalue()
    assert secret not in output, "an exception message reached the logs on the retry path"
    assert "rejected key" not in output, "handler message text reached the logs"

    row = read_job(migrated_engine, job_id)
    assert row.status == "PENDING"
    assert row.last_error == f"RuntimeError: rejected key {secret}"

    retrying = [
        event for event in logged_events(log_stream) if event.get("event") == "job.retrying"
    ]
    assert len(retrying) == 1
    assert retrying[0]["error_class"] == "RuntimeError"
    assert retrying[0]["last_error_chars"] == len(row.last_error)
    assert "last_error" not in retrying[0]
    assert retrying[0]["backoff_seconds"] > 0


def test_non_serialisable_result_does_not_leak_payload_or_result_to_logs(
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failed completion must not log SQLAlchemy's parameter dump of the result."""
    secret = "result-secret-4411"
    seed_job(migrated_engine, job_type="returns.opaque", payload={"k": secret})

    def _returns_opaque(payload: dict) -> dict:
        return {"secret": secret, "values": {1, 2}}

    monkeypatch.setitem(worker_module.handlers.HANDLERS, "returns.opaque", _returns_opaque)

    assert run_once(session_factory, worker_id="worker-opaque") is True
    assert secret not in log_stream.getvalue()


# ---------------------------------------------------------------------------
# Worker identity
# ---------------------------------------------------------------------------


def test_worker_id_is_host_pid_and_suffix() -> None:
    worker_id = make_worker_id()
    host, pid, suffix = worker_id.rsplit("-", 2)
    assert host
    assert pid.isdigit()
    assert len(suffix) == 8
