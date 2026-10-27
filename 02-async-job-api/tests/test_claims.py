"""Tests for the claim/lease protocol (repository level).

Falsification case 2 — "two workers claim the same job: no simultaneous
successful claim" — is the gate of PR-2-02. Every concurrency test here uses real
concurrent database connections: each claimer opens its own engine with
``NullPool`` so it gets its own physical connection, its own transaction, and its
own thread. Sequential calls on one session cannot falsify locking, so they are
never used to prove the claim protocol.

The two-claimer test repeats the race many times because a claim bug can pass a
single race by luck.
"""

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import get_type_hints

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app import repository
from app.models import Job
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

#: Long enough for a genuinely blocked claimer to be detected as stuck.
BLOCK_TIMEOUT_SECONDS = 5.0


@pytest.fixture(autouse=True)
def clean_database(migrated_engine: Engine) -> Iterator[None]:
    clear_jobs(migrated_engine)
    yield
    clear_jobs(migrated_engine)


@pytest.fixture
def database_url(migrated_engine: Engine) -> str:
    return migrated_engine.url.render_as_string(hide_password=False)


# ---------------------------------------------------------------------------
# Falsification case 2 — the gate of this PR
# ---------------------------------------------------------------------------


def test_two_concurrent_claimers_exactly_one_wins(
    database_url: str, migrated_engine: Engine
) -> None:
    """Two independent connections race for one job; exactly one must win.

    Repeated, because a single lucky race proves nothing about the locking.
    """
    trials = 20
    for trial in range(trials):
        job_id = seed_job(migrated_engine, payload={"trial": trial})
        recorder = ClaimRecorder()

        errors = run_concurrently(
            recorder.call(database_url, "worker-a"),
            recorder.call(database_url, "worker-b"),
        )
        assert errors == [None, None], f"trial {trial}: a claimer raised {errors}"

        winners = [worker for worker, claimed in recorder.claimed.items() if claimed == job_id]
        losers = [worker for worker, claimed in recorder.claimed.items() if claimed is None]
        assert len(winners) == 1, (
            f"trial {trial}: expected exactly one winner, got {recorder.claimed} — "
            "two simultaneous successful claims falsify the protocol"
        )
        assert len(losers) == 1, (
            f"trial {trial}: expected exactly one loser, got {recorder.claimed}"
        )

        row = read_job(migrated_engine, job_id)
        assert row.attempt == 1, f"trial {trial}: attempt must be 1, got {row.attempt}"
        assert row.status == "RUNNING"
        assert row.lease_owner == winners[0]


def test_five_jobs_five_claimers_each_job_claimed_once(
    database_url: str, migrated_engine: Engine
) -> None:
    """Five jobs, five concurrent claimers: every job goes to exactly one claimer."""
    seeded = {seed_job(migrated_engine, payload={"n": n}) for n in range(5)}
    recorder = ClaimRecorder()

    errors = run_concurrently(
        *[recorder.call(database_url, f"worker-{index}") for index in range(5)]
    )
    assert errors == [None] * 5, f"a claimer raised {errors}"

    claimed_ids = [claimed for claimed in recorder.claimed.values() if claimed is not None]
    assert len(claimed_ids) == 5, f"expected 5 claims from 5 workers, got {recorder.claimed}"
    assert len(set(claimed_ids)) == 5, f"a job was claimed twice: {claimed_ids}"
    assert set(claimed_ids) == seeded


def test_claim_skips_row_locked_by_another_transaction(
    database_url: str, migrated_engine: Engine
) -> None:
    """A claimer must skip a row another transaction holds, not block on it.

    Worker A claims the oldest job and keeps its transaction open. Worker B, starting
    afterwards, must take the next job rather than waiting for A to commit — that is
    SKIP LOCKED; plain FOR UPDATE would block here.

    Both workers are built the same way — own factory, own connection, disposed by
    the test body once both threads are done — so neither can quietly borrow the
    other's connection and make the race unfalsifiable.
    """
    first = seed_job(migrated_engine, created_at=datetime.now(UTC) - timedelta(minutes=2))
    second = seed_job(migrated_engine, created_at=datetime.now(UTC) - timedelta(minutes=1))

    factory_a = new_session_factory(database_url)
    factory_b = new_session_factory(database_url)
    assert factory_a.kw["bind"] is not factory_b.kw["bind"], (
        "the two claimers must not share an engine"
    )

    claimed_by_a: list[uuid.UUID | None] = []
    claimed_by_b: list[uuid.UUID | None] = []
    a_has_claimed = threading.Event()
    release_a = threading.Event()

    def worker_a() -> None:
        try:
            with factory_a() as session:
                claim = repository.claim_next_job(session, "worker-a", 60)
                claimed_by_a.append(None if claim is None else claim[0].id)
                a_has_claimed.set()
                release_a.wait(timeout=BLOCK_TIMEOUT_SECONDS)
                session.commit()
        finally:
            a_has_claimed.set()

    def worker_b() -> None:
        assert a_has_claimed.wait(timeout=BLOCK_TIMEOUT_SECONDS)
        with factory_b() as session:
            claim = repository.claim_next_job(session, "worker-b", 60)
            claimed_by_b.append(None if claim is None else claim[0].id)
            session.commit()

    thread_a = threading.Thread(target=worker_a, daemon=True)
    thread_b = threading.Thread(target=worker_b, daemon=True)
    thread_a.start()
    thread_b.start()

    try:
        thread_b.join(timeout=BLOCK_TIMEOUT_SECONDS)
        assert not thread_b.is_alive(), (
            "worker-b blocked on the row worker-a holds instead of skipping it — "
            "the claim is not using SKIP LOCKED"
        )
    finally:
        release_a.set()
        thread_a.join(timeout=BLOCK_TIMEOUT_SECONDS)
        thread_b.join(timeout=BLOCK_TIMEOUT_SECONDS)
        dispose(factory_a)
        dispose(factory_b)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert claimed_by_a == [first]
    assert claimed_by_b == [second]


def test_claim_once_returns_a_job_so_ids_are_never_compared_to_jobs(
    database_url: str, migrated_engine: Engine
) -> None:
    """``claim_once`` must hand back ``(Job, kind) | None``, not a bare id.

    The concurrency gate compares recorded claim results against seeded UUIDs. If the
    helper returned anything other than the repository's own ``Job``, those
    comparisons could silently never match and the gate would pass without testing
    anything.
    """
    job_id = seed_job(migrated_engine)
    claim = claim_once(database_url, "worker-typed")

    assert claim is not None
    claimed, kind = claim
    assert isinstance(claimed, Job), f"expected a Job, got {type(claimed).__name__}"
    assert isinstance(claimed.id, uuid.UUID)
    assert claimed.id == job_id
    assert kind == "pending"
    assert claim_once(database_url, "worker-typed") is None


def test_claim_helpers_are_annotated_with_the_repository_return_type() -> None:
    """The helper annotations must match ``claim_next_job`` so a checker agrees.

    mypy is not a project dependency, so the annotation is asserted directly rather
    than left to a type checker nobody runs.
    """
    expected = get_type_hints(repository.claim_next_job)["return"]
    assert get_type_hints(claim_once)["return"] == expected
    assert get_type_hints(ClaimRecorder.claim)["return"] is type(None)
    assert get_type_hints(ClaimRecorder.__init__)["return"] is type(None)


# ---------------------------------------------------------------------------
# Ordering, exhaustion, and claim side effects
# ---------------------------------------------------------------------------


def test_claim_returns_oldest_first(database_url: str, migrated_engine: Engine) -> None:
    now = datetime.now(UTC)
    oldest = seed_job(migrated_engine, created_at=now - timedelta(minutes=3))
    middle = seed_job(migrated_engine, created_at=now - timedelta(minutes=2))
    newest = seed_job(migrated_engine, created_at=now - timedelta(minutes=1))

    factory = new_session_factory(database_url)
    claimed: list[uuid.UUID] = []
    try:
        for _ in range(3):
            with factory() as session:
                claim = repository.claim_next_job(session, "worker-order", 60)
                assert claim is not None
                claimed.append(claim[0].id)
                session.commit()
    finally:
        dispose(factory)

    assert claimed == [oldest, middle, newest]


def test_exhausted_job_is_never_claimed(database_url: str, migrated_engine: Engine) -> None:
    """attempt == max_attempts means exhausted: the claim must leave it alone."""
    job_id = seed_job(migrated_engine, attempt=3, max_attempts=3)

    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            assert repository.claim_next_job(session, "worker-exhausted", 60) is None
            session.commit()
    finally:
        dispose(factory)

    row = read_job(migrated_engine, job_id)
    assert row.status == "PENDING"
    assert row.attempt == 3
    assert row.lease_owner is None


def test_claim_sets_lease_and_increments_attempt(
    database_url: str, migrated_engine: Engine
) -> None:
    job_id = seed_job(migrated_engine, payload={"x": 1})
    before = read_job(migrated_engine, job_id)

    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            claim = repository.claim_next_job(session, "worker-lease", 90)
            session.commit()
            assert claim is not None and claim[0].id == job_id
    finally:
        dispose(factory)

    row = read_job(migrated_engine, job_id)
    assert row.status == "RUNNING"
    assert row.lease_owner == "worker-lease"
    assert row.attempt == before.attempt + 1 == 1
    assert row.claimed_at is not None
    assert row.finished_at is None
    assert row.lease_expires_at is not None
    lease_seconds = (row.lease_expires_at - row.claimed_at).total_seconds()
    assert 85 <= lease_seconds <= 95, f"lease should be ~90s, got {lease_seconds}"


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


def _claim_as(database_url: str, worker_id: str, job_id: uuid.UUID) -> None:
    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            claim = repository.claim_next_job(session, worker_id, 60)
            assert claim is not None and claim[0].id == job_id
            session.commit()
    finally:
        dispose(factory)


def _complete(
    database_url: str,
    *,
    job_id: uuid.UUID,
    worker_id: str,
    status: str,
    result: dict | None = None,
    last_error: str | None = None,
) -> int:
    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            changed = repository.complete_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                status=status,
                result=result,
                last_error=last_error,
            )
            session.commit()
            return changed
    finally:
        dispose(factory)


def test_completion_is_owner_scoped(database_url: str, migrated_engine: Engine) -> None:
    """A worker that lost its lease must not be able to write the result."""
    job_id = seed_job(migrated_engine)
    _claim_as(database_url, "worker-a", job_id)

    changed = _complete(
        database_url,
        job_id=job_id,
        worker_id="worker-b",
        status="SUCCEEDED",
        result={"stolen": True},
    )
    assert changed == 0

    row = read_job(migrated_engine, job_id)
    assert row.status == "RUNNING"
    assert row.lease_owner == "worker-a"
    assert row.result is None
    assert row.finished_at is None


def test_completion_sets_terminal_state_and_clears_lease(
    database_url: str, migrated_engine: Engine
) -> None:
    job_id = seed_job(migrated_engine)
    _claim_as(database_url, "worker-a", job_id)

    changed = _complete(
        database_url,
        job_id=job_id,
        worker_id="worker-a",
        status="SUCCEEDED",
        result={"echoed": {}},
    )
    assert changed == 1

    row = read_job(migrated_engine, job_id)
    assert row.status == "SUCCEEDED"
    assert row.result == {"echoed": {}}
    assert row.last_error is None
    assert row.finished_at is not None
    assert row.lease_owner is None
    assert row.lease_expires_at is None


def test_completion_refuses_already_finished_job(
    database_url: str, migrated_engine: Engine
) -> None:
    """A second completion of a finished job changes nothing and returns 0.

    Here the lease has already been cleared by the first completion, so this
    exercises the lease clearing as much as the status guard. The status guard on
    its own is isolated in the test below.
    """
    job_id = seed_job(migrated_engine)
    _claim_as(database_url, "worker-a", job_id)
    assert (
        _complete(
            database_url,
            job_id=job_id,
            worker_id="worker-a",
            status="SUCCEEDED",
            result={"first": True},
        )
        == 1
    )

    changed = _complete(
        database_url,
        job_id=job_id,
        worker_id="worker-a",
        status="FAILED",
        last_error="late failure",
    )
    assert changed == 0

    row = read_job(migrated_engine, job_id)
    assert row.status == "SUCCEEDED"
    assert row.result == {"first": True}
    assert row.last_error is None


def test_completion_refuses_terminal_job_that_still_carries_its_lease(
    database_url: str, migrated_engine: Engine
) -> None:
    """The status = 'RUNNING' guard must reject on status alone.

    Seeded directly: a terminal row that still names its lease owner. Normal
    completion clears the lease, so this state does not arise from the happy path —
    which is exactly why the guard needs testing on its own. Without the status
    check, ``lease_owner = :worker_id`` alone would let this write through.
    """
    job_id = seed_job(migrated_engine)
    # One UPDATE so ck_jobs_lease_pair and ck_jobs_terminal_has_finished are both
    # satisfied at statement end: a terminal row that still names a live lease.
    with migrated_engine.connect() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET status = 'SUCCEEDED', "
                "lease_owner = 'worker-a', "
                "lease_expires_at = now() + interval '60 seconds', "
                "finished_at = now() "
                "WHERE id = :id"
            ),
            {"id": job_id},
        )
        conn.commit()

    changed = _complete(
        database_url,
        job_id=job_id,
        worker_id="worker-a",
        status="FAILED",
        last_error="must not overwrite a terminal job",
    )
    assert changed == 0

    row = read_job(migrated_engine, job_id)
    assert row.status == "SUCCEEDED", "a terminal job was overwritten"
    assert row.last_error is None


# ---------------------------------------------------------------------------
# Lease constraints — same direct-SQL pattern as test_ck_jobs_status_rejects_invalid_value
# ---------------------------------------------------------------------------


def test_ck_jobs_running_has_lease_rejects_violation(migrated_engine: Engine) -> None:
    with pytest.raises(IntegrityError):
        with migrated_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO jobs (id, type, payload, status) "
                    "VALUES (:id, 'lease.test', '{}'::jsonb, 'RUNNING')"
                ),
                {"id": str(uuid.uuid4())},
            )
            conn.commit()


def test_ck_jobs_lease_pair_rejects_half_lease(migrated_engine: Engine) -> None:
    with pytest.raises(IntegrityError):
        with migrated_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO jobs (id, type, payload, lease_owner) "
                    "VALUES (:id, 'lease.test', '{}'::jsonb, 'worker-a')"
                ),
                {"id": str(uuid.uuid4())},
            )
            conn.commit()


def test_ck_jobs_terminal_has_finished_rejects_violation(migrated_engine: Engine) -> None:
    with pytest.raises(IntegrityError):
        with migrated_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO jobs (id, type, payload, status) "
                    "VALUES (:id, 'lease.test', '{}'::jsonb, 'SUCCEEDED')"
                ),
                {"id": str(uuid.uuid4())},
            )
            conn.commit()
