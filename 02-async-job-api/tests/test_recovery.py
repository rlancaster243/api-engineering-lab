"""Tests for retry and lease-expiry recovery (PR-2-03).

Falsification case 3 — "worker crashes mid-job: expired claim is recovered safely" —
is the gate of this PR. The crash test spawns a real worker subprocess and SIGKILLs
it; a mocked handler cannot prove the process actually died holding a lease.
"""

import io
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app import repository
from app import worker as worker_module
from app.config import retry_delay_seconds
from app.logging import configure_logging
from app.worker import run_once
from tests.db_helpers import (
    ClaimRecorder,
    claim_once,
    clear_jobs,
    dispose,
    new_session_factory,
    read_job,
    run_concurrently,
    seed_job,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def expire_lease(engine: Engine, job_id: uuid.UUID, *, minutes: int = 5) -> None:
    """Backdate a lease instead of sleeping through LEASE_SECONDS."""
    with engine.connect() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET lease_expires_at = now() - make_interval(mins => :m) "
                "WHERE id = :id"
            ),
            {"id": job_id, "m": minutes},
        )
        conn.commit()


def make_running(
    engine: Engine,
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    owner: str = "crashed-worker",
    created_at: datetime | None = None,
    expired: bool = True,
) -> uuid.UUID:
    """Seed a job already claimed by a worker, lease expired by default."""
    job_id = seed_job(engine, attempt=attempt, max_attempts=max_attempts, created_at=created_at)
    lease = "now() - interval '5 minutes'" if expired else "now() + interval '60 seconds'"
    with engine.connect() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET status='RUNNING', lease_owner=:owner, "
                f"lease_expires_at = {lease}, claimed_at = now() WHERE id = :id"
            ),
            {"id": job_id, "owner": owner},
        )
        conn.commit()
    return job_id


def mark_running_owned(
    engine: Engine, job_id: uuid.UUID, *, owner: str, expired: bool = False
) -> None:
    lease = "now() - interval '5 minutes'" if expired else "now() + interval '60 seconds'"
    with engine.connect() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET status='RUNNING', lease_owner=:owner, "
                f"lease_expires_at = {lease} WHERE id = :id"
            ),
            {"id": job_id, "owner": owner},
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Falsification case 3 — the gate of this PR
# ---------------------------------------------------------------------------


def test_crashed_worker_job_is_reclaimed_by_another_worker(
    migrated_engine: Engine,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGKILL a real worker mid-job, then prove a second worker recovers the job."""
    ready_file = tmp_path / "ready"
    job_id = seed_job(
        migrated_engine,
        job_type="block",
        payload={"ready_file": str(ready_file)},
        max_attempts=3,
    )

    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "LEASE_SECONDS": "1",
        "WORKER_POLL_SECONDS": "0.05",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.blocking_worker"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not ready_file.exists():
            if process.poll() is not None:
                out, err = process.communicate()
                pytest.fail(f"worker exited before starting the job: {out!r} {err!r}")
            time.sleep(0.02)
        assert ready_file.exists(), "worker never entered the handler"

        mid_flight = read_job(migrated_engine, job_id)
        assert mid_flight.status == "RUNNING"
        dead_worker_id = mid_flight.lease_owner
        assert dead_worker_id, "a RUNNING job must name its lease owner"
    finally:
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=30)

    # SIGKILL leaves the row RUNNING: this is the gap PR-2-02 shipped with.
    after_crash = read_job(migrated_engine, job_id)
    assert after_crash.status == "RUNNING"
    assert after_crash.lease_owner == dead_worker_id
    assert after_crash.finished_at is None

    expire_lease(migrated_engine, job_id)

    # The rescuer runs in this process, so give it a non-blocking handler for the
    # same job type: the crash proved the lease is held by a dead process, and this
    # proves a healthy worker can take the job all the way to completion.
    monkeypatch.setitem(
        worker_module.handlers.HANDLERS, "block", lambda payload: {"recovered": True}
    )

    factory = new_session_factory(database_url)
    try:
        assert run_once(factory, lease_seconds=60, worker_id="worker-rescuer") is True
    finally:
        dispose(factory)

    recovered = read_job(migrated_engine, job_id)
    assert recovered.status == "SUCCEEDED", "the crashed job was not recovered"
    assert recovered.attempt == 2, "recovery must charge a second attempt"
    assert recovered.result == {"recovered": True}
    assert recovered.finished_at is not None
    assert recovered.lease_owner is None


def test_recovery_emits_job_recovered_event(migrated_engine: Engine, database_url: str) -> None:
    job_id = make_running(migrated_engine, owner="dead-worker")
    stream = io.StringIO()
    logger = configure_logging(stream)
    try:
        factory = new_session_factory(database_url)
        try:
            assert (
                run_once(factory, lease_seconds=60, worker_id="worker-rescuer", logger=logger)
                is True
            )
        finally:
            dispose(factory)
    finally:
        logger.handlers.clear()

    events = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if line.strip() and json.loads(line).get("event")
    ]
    recovered = [e for e in events if e["event"] == "job.recovered"]
    assert len(recovered) == 1
    assert recovered[0]["job_id"] == str(job_id)
    assert recovered[0]["attempt"] == 2
    assert read_job(migrated_engine, job_id).status == "SUCCEEDED"


def test_crash_on_last_attempt_is_failed_not_left_running(
    migrated_engine: Engine, database_url: str
) -> None:
    """The trap: expired + no attempts left must terminate, not sit RUNNING forever.

    Without the sweep the claim's `attempt < max_attempts` guard refuses this row and
    nothing else moves it: it would stay RUNNING with an expired lease forever.
    """
    job_id = make_running(migrated_engine, attempt=1, max_attempts=1)

    factory = new_session_factory(database_url)
    try:
        # Nothing is claimable; the sweep is what terminates the row.
        assert run_once(factory, lease_seconds=60, worker_id="worker-sweeper") is False
    finally:
        dispose(factory)

    row = read_job(migrated_engine, job_id)
    assert row.status == "FAILED", "an exhausted expired job must not stay RUNNING"
    assert row.finished_at is not None
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    assert "no attempts remaining" in row.last_error
    assert row.last_error.startswith("lease expired after 1 attempt")


def test_exhausted_expired_job_is_swept_and_never_claimed(
    migrated_engine: Engine, database_url: str
) -> None:
    job_id = make_running(migrated_engine, attempt=3, max_attempts=3)

    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            swept = repository.sweep_expired(session)
            session.commit()
            assert repository.claim_next_job(session, "w", 60) is None
            session.commit()
    finally:
        dispose(factory)

    assert swept == [(job_id, 3)]
    assert read_job(migrated_engine, job_id).status == "FAILED"


def test_live_lease_is_never_stolen_or_swept(migrated_engine: Engine, database_url: str) -> None:
    """A healthy in-flight job must survive both the sweep and the claim."""
    job_id = make_running(migrated_engine, attempt=1, max_attempts=3, expired=False)

    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            assert repository.sweep_expired(session) == []
            session.commit()
            assert repository.claim_next_job(session, "worker-thief", 60) is None
            session.commit()
    finally:
        dispose(factory)

    row = read_job(migrated_engine, job_id)
    assert row.status == "RUNNING"
    assert row.lease_owner == "crashed-worker"
    assert row.attempt == 1


def test_reclaim_probe_is_exclusive_under_concurrency(
    migrated_engine: Engine, database_url: str
) -> None:
    """Two independent connections racing one expired row: exactly one winner."""
    trials = 20
    for trial in range(trials):
        job_id = make_running(migrated_engine, attempt=1, max_attempts=3)
        recorder = ClaimRecorder()

        errors = run_concurrently(
            recorder.call(database_url, "worker-a"),
            recorder.call(database_url, "worker-b"),
        )
        assert errors == [None, None], f"trial {trial}: a claimer raised {errors}"

        winners = [w for w, claimed in recorder.claimed.items() if claimed == job_id]
        assert len(winners) == 1, (
            f"trial {trial}: expected exactly one winner, got {recorder.claimed}"
        )
        assert recorder.kinds[winners[0]] == "reclaimed"
        row = read_job(migrated_engine, job_id)
        assert row.attempt == 2, f"trial {trial}: attempt must go 1 -> 2"
        clear_jobs(migrated_engine)


def test_reclaim_is_served_before_a_busy_pending_queue(
    migrated_engine: Engine, database_url: str
) -> None:
    """A crashed job must not wait behind PENDING work."""
    expired = make_running(migrated_engine, attempt=1, max_attempts=3, created_at=datetime.now(UTC))
    # Older PENDING jobs, so FIFO alone would pick one of these first.
    for _ in range(3):
        seed_job(migrated_engine, created_at=datetime.now(UTC) - timedelta(minutes=10))

    claimed, kind = claim_once(database_url, "worker-reclaimer")
    assert claimed == expired, "the reclaim probe must run before the pending probe"
    assert kind == "reclaimed"


# ---------------------------------------------------------------------------
# Retry and requeue
# ---------------------------------------------------------------------------


def test_requeue_sets_backoff_and_is_not_immediately_claimable(
    migrated_engine: Engine, database_url: str
) -> None:
    job_id = seed_job(migrated_engine, attempt=1, max_attempts=3)
    mark_running_owned(migrated_engine, job_id, owner="worker-a")

    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            outcome = repository.fail_attempt(
                session,
                job_id=job_id,
                worker_id="worker-a",
                last_error="RuntimeError: boom",
                backoff_seconds=30,
            )
            session.commit()
            assert outcome == ("PENDING", 1)
    finally:
        dispose(factory)

    row = read_job(migrated_engine, job_id)
    assert row.status == "PENDING"
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    assert row.finished_at is None
    assert row.last_error == "RuntimeError: boom"
    assert row.next_attempt_at > datetime.now(UTC), "the retry must be deferred"

    claimed, _ = claim_once(database_url, "worker-b")
    assert claimed is None, "a backed-off job was claimed immediately"


def test_requeued_job_is_claimable_once_due(migrated_engine: Engine, database_url: str) -> None:
    job_id = seed_job(migrated_engine, attempt=1, max_attempts=3)
    with migrated_engine.connect() as conn:
        conn.execute(
            text("UPDATE jobs SET next_attempt_at = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )
        conn.commit()
    claimed, kind = claim_once(database_url, "worker-due")
    assert claimed == job_id
    assert kind == "pending"
    assert read_job(migrated_engine, job_id).attempt == 2


def test_failure_at_last_attempt_is_terminal(migrated_engine: Engine, database_url: str) -> None:
    job_id = seed_job(migrated_engine, attempt=3, max_attempts=3)
    mark_running_owned(migrated_engine, job_id, owner="worker-a")

    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            outcome = repository.fail_attempt(
                session,
                job_id=job_id,
                worker_id="worker-a",
                last_error="RuntimeError: boom",
                backoff_seconds=5,
            )
            session.commit()
            assert outcome == ("FAILED", 3)
    finally:
        dispose(factory)

    row = read_job(migrated_engine, job_id)
    assert row.status == "FAILED"
    assert row.finished_at is not None
    assert row.lease_owner is None

    assert claim_once(database_url, "worker-b") == (None, None)


def test_fail_attempt_from_a_worker_that_lost_the_lease_changes_nothing(
    migrated_engine: Engine, database_url: str
) -> None:
    job_id = seed_job(migrated_engine, attempt=1, max_attempts=3)
    mark_running_owned(migrated_engine, job_id, owner="worker-a")

    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            outcome = repository.fail_attempt(
                session,
                job_id=job_id,
                worker_id="worker-thief",
                last_error="RuntimeError: boom",
                backoff_seconds=5,
            )
            session.commit()
    finally:
        dispose(factory)

    assert outcome is None, "a non-owner must not be able to fail or requeue the job"
    row = read_job(migrated_engine, job_id)
    assert row.status == "RUNNING"
    assert row.lease_owner == "worker-a"
    assert row.last_error is None


def test_backoff_prevents_a_poison_job_from_starving_the_queue(
    migrated_engine: Engine, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing job must not burn all attempts in a tight loop and block others."""
    poison = seed_job(migrated_engine, job_type="boom", max_attempts=3)
    healthy = seed_job(migrated_engine, job_type="echo", payload={"ok": True})

    def _boom(payload: dict) -> dict:
        raise RuntimeError("always fails")

    monkeypatch.setitem(worker_module.handlers.HANDLERS, "boom", _boom)

    factory = new_session_factory(database_url)
    try:
        assert run_once(factory, worker_id="w", retry_backoff_seconds=30) is True
        assert read_job(migrated_engine, poison).status == "PENDING", (
            "first failure should requeue, not exhaust"
        )

        # The healthy job must be next, not a second poison attempt.
        assert run_once(factory, worker_id="w", retry_backoff_seconds=30) is True
        assert read_job(migrated_engine, healthy).status == "SUCCEEDED"
        assert read_job(migrated_engine, poison).attempt == 1, (
            "the poison job was retried before the healthy job was served"
        )
    finally:
        dispose(factory)


def test_success_is_terminal_despite_remaining_attempts(
    migrated_engine: Engine, database_url: str
) -> None:
    job_id = seed_job(migrated_engine, attempt=1, max_attempts=5, payload={"x": 1})
    factory = new_session_factory(database_url)
    try:
        assert run_once(factory, worker_id="w") is True
    finally:
        dispose(factory)

    row = read_job(migrated_engine, job_id)
    assert row.status == "SUCCEEDED"
    assert row.attempt == 2
    assert row.finished_at is not None


def test_worker_logs_retrying_then_succeeded(
    migrated_engine: Engine, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = io.StringIO()
    logger = configure_logging(stream)
    calls = {"n": 0}

    def _flaky(payload: dict) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first attempt fails")
        return {"ok": True}

    monkeypatch.setitem(worker_module.handlers.HANDLERS, "flaky", _flaky)
    job_id = seed_job(migrated_engine, job_type="flaky", max_attempts=3)

    factory = new_session_factory(database_url)
    try:
        assert run_once(factory, worker_id="w", logger=logger, retry_backoff_seconds=60) is True
        # Clear the backoff so the retry is claimable now.
        with migrated_engine.connect() as conn:
            conn.execute(
                text("UPDATE jobs SET next_attempt_at = now() WHERE id = :id"), {"id": job_id}
            )
            conn.commit()
        assert run_once(factory, worker_id="w", logger=logger) is True
    finally:
        dispose(factory)
        logger.handlers.clear()

    events = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if line.strip() and json.loads(line).get("event")
    ]
    names = [e["event"] for e in events]
    assert "job.retrying" in names
    assert "job.succeeded" in names
    assert read_job(migrated_engine, job_id).status == "SUCCEEDED"


# ---------------------------------------------------------------------------
# Backoff arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, 5), (2, 10), (3, 20), (4, 40), (8, 300), (20, 300)],
)
def test_backoff_is_exponential_and_capped(attempt: int, expected: int) -> None:
    assert retry_delay_seconds(attempt, base=5, ceiling=300) == expected


def test_backoff_never_exceeds_ceiling() -> None:
    assert retry_delay_seconds(3, base=1, ceiling=3) == 3


# ---------------------------------------------------------------------------
# Sweep behaviour
# ---------------------------------------------------------------------------


def test_sweep_is_idempotent(migrated_engine: Engine, database_url: str) -> None:
    job_id = make_running(migrated_engine, attempt=2, max_attempts=2)
    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            assert len(repository.sweep_expired(session)) == 1
            session.commit()
        with factory() as session:
            assert repository.sweep_expired(session) == []
            session.commit()
    finally:
        dispose(factory)
    assert read_job(migrated_engine, job_id).status == "FAILED"


def test_sweep_leaves_budget_left_rows_alone(migrated_engine: Engine, database_url: str) -> None:
    """Expired but attempts remain: reclaimable, so the sweep must not fail it."""
    job_id = make_running(migrated_engine, attempt=1, max_attempts=3)
    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            assert repository.sweep_expired(session) == []
            session.commit()
    finally:
        dispose(factory)
    row = read_job(migrated_engine, job_id)
    assert row.status == "RUNNING", "a reclaimable job must not be failed by the sweep"


def test_sweep_failure_does_not_stop_the_worker(
    migrated_engine: Engine, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep DB error is logged and the claim still runs."""
    seed_job(migrated_engine, payload={})

    def _explode(session: Session) -> list[tuple[uuid.UUID, int]]:
        raise worker_module.SQLAlchemyError("sweep blew up")

    monkeypatch.setattr(repository, "sweep_expired", _explode)

    factory = new_session_factory(database_url)
    try:
        assert run_once(factory, worker_id="w") is True
    finally:
        dispose(factory)
