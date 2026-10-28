"""Tests for the lease heartbeat and the reconciler's live-lease guard (PR-2-06).

Two gaps are closed here and neither is closed by the other.

**The heartbeat** keeps a long handler's lease alive, so a handler that outlives
``LEASE_SECONDS`` is no longer reclaimed mid-run and executed twice. It is
owner-scoped, so a worker whose lease was taken over cannot revive it: the renewal
answers zero rows, the worker logs ``job.lease_lost`` and stops renewing, and the
handler is allowed to finish — its completion is refused by the same
``lease_owner`` guard that protects every other write.

**The live-lease guard** closes the effect-duplication window the heartbeat alone
leaves open. The reconciler used to select ``INTENT`` rows by age alone, so a
handler that heartbeated past ``EFFECT_RECONCILE_GRACE_SECONDS`` had its in-flight
effect reconciled underneath it and applied twice. The age filter is now only a
candidate list; the decision is taken under the effect row's lock, where the job's
lease is re-read, and a live lease means skip. That check has to be at apply time:
a heartbeat landing between an unlocked read and the apply revives the lease, so a
read-time check would already have decided to apply.

The concurrency tests drive real threads and real connections. Where a wait could
be shortened to a sleep it is an ``Event`` instead, so the test observes the point
it cares about rather than hoping a duration is long enough.
"""

import io
import json
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app import handlers, repository
from app.logging import configure_logging
from app.worker import (
    HEARTBEAT_THREAD_NAME_PREFIX,
    Worker,
    _LeaseHeartbeat,
    renewal_interval_seconds,
    run_once,
)
from tests.db_helpers import (
    clear_jobs,
    dispose,
    new_session_factory,
    read_job,
    run_concurrently,
    seed_job,
)

LEASE_SECONDS = 1
#: Grace used by the reconciler tests. One second is the smallest value the
#: configuration accepts, so a handler has to outlive a real second to be past it.
GRACE_SECONDS = 1


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
def second_factory(database_url: str) -> Iterator[sessionmaker[Session]]:
    """A second worker's factory — separate connections from the first."""
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
    records = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    return [record for record in records if record.get("event")]


def events_named(stream: io.StringIO, name: str) -> list[dict[str, Any]]:
    return [event for event in logged_events(stream) if event["event"] == name]


def live_heartbeat_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(HEARTBEAT_THREAD_NAME_PREFIX)
    ]


def count_effects(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM job_effects")).scalar_one()


def read_effect(engine: Engine, job_id: uuid.UUID, effect_key: str = "notify") -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT status FROM job_effects WHERE job_id = :job_id AND effect_key = :key"),
            {"job_id": job_id, "key": effect_key},
        ).one_or_none()


def seed_effect(engine: Engine, *, job_id: uuid.UUID, effect_key: str = "notify", age: int) -> None:
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO job_effects (job_id, effect_key, status, created_at) "
                "VALUES (:job_id, :effect_key, 'INTENT', now() - make_interval(secs => :age))"
            ),
            {"job_id": job_id, "effect_key": effect_key, "age": age},
        )
        conn.commit()


def mark_job(
    engine: Engine,
    job_id: uuid.UUID,
    *,
    status: str,
    owner: str | None,
    lease_seconds_from_now: float | None,
    finished: bool = False,
) -> None:
    """Set the lease fields directly, so a state is reached without sleeping."""
    with engine.connect() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET status = :status, lease_owner = :owner, "
                " lease_expires_at = CASE WHEN CAST(:lease AS double precision) IS NULL "
                "   THEN NULL "
                "   ELSE now() + make_interval(secs => CAST(:lease AS double precision)) END, "
                " finished_at = CASE WHEN :finished THEN now() ELSE NULL END "
                "WHERE id = :id"
            ),
            {
                "status": status,
                "owner": owner,
                "lease": lease_seconds_from_now,
                "finished": finished,
                "id": job_id,
            },
        )
        conn.commit()


class EffectRecorder:
    """The injected external call; every application is appended."""

    def __init__(self) -> None:
        self.applied: list[tuple[uuid.UUID, str]] = []

    def __call__(self, job_id: uuid.UUID, effect_key: str, detail: dict[str, Any] | None) -> None:
        self.applied.append((job_id, effect_key))

    @property
    def count(self) -> int:
        return len(self.applied)


# ---------------------------------------------------------------------------
# 1. A long handler is not reclaimed
# ---------------------------------------------------------------------------


def test_long_handler_is_not_reclaimed_and_completes_once(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    second_factory: sessionmaker[Session],
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler that outlives ``LEASE_SECONDS`` keeps its job.

    Kills two mutations. "Never start the renewal thread": the lease then expires
    mid-handler, the second worker's reclaim probe takes the row, the handler runs a
    second time and the first worker's completion is refused. "Renew at the full
    lease instead of a third": the first renewal lands at exactly the lease, which is
    too late — the reclaim probe at 2.5s still finds an expired row.

    The handler blocks on an ``Event`` rather than sleeping, so the second worker is
    guaranteed to probe while the handler is still running, and the mid-handler
    snapshot is taken while the handler is provably alive. The one real wait is the
    deliberate one past the lease: the probe has to land *after* the claim's lease
    would have expired, which is one second of wall clock by definition.
    """
    job_id = seed_job(migrated_engine, job_type="slow", payload={"x": 1}, max_attempts=3)
    entered = threading.Event()
    release = threading.Event()
    executions: list[str] = []
    renewal_times: list[float] = []
    real_renew = repository.renew_lease

    def _spy_renew(
        session: Session, *, job_id: uuid.UUID, worker_id: str, lease_seconds: int
    ) -> bool:
        renewed = real_renew(
            session, job_id=job_id, worker_id=worker_id, lease_seconds=lease_seconds
        )
        renewal_times.append(time.monotonic())
        return renewed

    monkeypatch.setattr(repository, "renew_lease", _spy_renew)

    def _slow(_payload: dict, _context: handlers.HandlerContext) -> dict:
        executions.append("run")
        if entered.is_set():
            # Only reachable if the job was reclaimed and re-run.
            return {"second_execution": True}
        entered.set()
        assert release.wait(timeout=30), "the test never released the handler"
        return {"done": True}

    monkeypatch.setitem(handlers.HANDLERS, "slow", _slow)

    logger = configure_logging(log_stream)
    worker_a = Worker(
        session_factory,
        lease_seconds=LEASE_SECONDS,
        poll_seconds=0.01,
        worker_id="worker-a",
        logger=logger,
    )
    runner = threading.Thread(target=worker_a.run_once, daemon=True)
    runner.start()
    try:
        assert entered.wait(timeout=10), "the handler never started"
        claimed_at = read_job(migrated_engine, job_id)
        assert claimed_at.lease_expires_at is not None

        # Well past the original claim's lease, and past several renewal ticks.
        time.sleep(2.5)

        mid_flight = read_job(migrated_engine, job_id)
        assert mid_flight.status == "RUNNING"
        assert mid_flight.lease_owner == "worker-a"
        assert mid_flight.attempt == 1, "the job was reclaimed while its handler was alive"
        assert mid_flight.lease_expires_at > claimed_at.lease_expires_at, (
            "the lease was never pushed forward, so the job only survived by luck"
        )

        # The cadence, not just the fact of renewing. A third of the lease gives ~7
        # renewals in 2.5s; renewing once per lease gives 2, and the lease is expired
        # for most of that interval. Asserting the gap is what pins the ratio.
        assert len(renewal_times) >= 3, (
            f"only {len(renewal_times)} renewal(s) in 2.5s at a {LEASE_SECONDS}s lease; "
            "the interval is not a third of the lease"
        )
        gaps = [
            later - earlier
            for earlier, later in zip(renewal_times, renewal_times[1:], strict=False)
        ]
        assert max(gaps) < LEASE_SECONDS / 2, (
            f"renewals were {max(gaps):.2f}s apart on a {LEASE_SECONDS}s lease; "
            "the interval is not a third of the lease"
        )

        worker_b = Worker(
            second_factory,
            lease_seconds=LEASE_SECONDS,
            poll_seconds=0.01,
            worker_id="worker-b",
            logger=logger,
        )
        assert worker_b.run_once() is False, "a live job was claimable by another worker"
    finally:
        release.set()
        runner.join(timeout=30)
        logger.handlers.clear()

    assert not runner.is_alive()

    row = read_job(migrated_engine, job_id)
    assert row.status == "SUCCEEDED", "the long handler's job did not complete"
    assert row.attempt == 1, "the job was reclaimed instead of completing"
    assert row.result == {"done": True}
    assert row.lease_owner is None
    assert executions == ["run"], "the handler ran more than once"

    names = [event["event"] for event in logged_events(log_stream)]
    assert "job.lease_lost" not in names
    assert "job.recovered" not in names


# ---------------------------------------------------------------------------
# 2. Renewal is owner-scoped
# ---------------------------------------------------------------------------


def test_renewal_by_a_non_owner_affects_no_rows_and_leaves_the_lease_alone(
    migrated_engine: Engine, database_url: str
) -> None:
    """Kills the mutation that drops ``lease_owner = :worker_id`` from the renewal.

    Without that predicate a takeover victim could push the new owner's lease
    forward, and — worse — a worker that lost the job could keep it looking alive
    while a third worker's reclaim probe watched it. The row must be untouched.
    """
    job_id = seed_job(migrated_engine, job_type="echo")
    mark_job(
        migrated_engine,
        job_id,
        status="RUNNING",
        owner="worker-owner",
        lease_seconds_from_now=60,
    )
    before = read_job(migrated_engine, job_id)

    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            assert (
                repository.renew_lease(
                    session, job_id=job_id, worker_id="worker-thief", lease_seconds=600
                )
                is False
            ), "a non-owner renewed the lease"
            session.commit()

        after_thief = read_job(migrated_engine, job_id)
        assert after_thief.lease_owner == "worker-owner"
        assert after_thief.lease_expires_at == before.lease_expires_at, (
            "a non-owner's renewal moved the lease"
        )
        assert after_thief.attempt == before.attempt
        assert after_thief.status == "RUNNING"

        # The negative control: the owner does move it, so the assertion above is
        # about ownership and not about the statement being a no-op.
        with factory() as session:
            assert (
                repository.renew_lease(
                    session, job_id=job_id, worker_id="worker-owner", lease_seconds=600
                )
                is True
            ), "the owner could not renew its own lease"
            session.commit()
    finally:
        dispose(factory)

    after = read_job(migrated_engine, job_id)
    assert after.lease_owner == "worker-owner"
    assert after.lease_expires_at is not None
    assert after.lease_expires_at > before.lease_expires_at, (
        "the owner's own renewal did not extend the lease"
    )
    assert after.attempt == before.attempt, "a renewal must not consume an attempt"
    assert after.status == "RUNNING"


# ---------------------------------------------------------------------------
# 3. A lost lease mid-handler is detected, logged once, and the write is refused
# ---------------------------------------------------------------------------


def test_lease_lost_mid_handler_is_logged_and_the_completion_is_refused(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The thief takes the job mid-handler; the stale worker must not write to it.

    The handler waits for the *renewal itself* to report the loss rather than
    sleeping for a fixed time, so the detection point is observed, not hoped for.
    Two ``job.lease_lost`` events are expected: the heartbeat's, when its renewal
    matched zero rows, and ``_finish``'s, when the completion was refused. One event
    means the heartbeat never noticed; three would mean it logged the loss twice
    instead of stopping.
    """
    job_id = seed_job(migrated_engine, job_type="steal", payload={}, max_attempts=3)
    renewals: list[bool] = []
    lost = threading.Event()
    real_renew = repository.renew_lease

    def _spy(session: Session, *, job_id: uuid.UUID, worker_id: str, lease_seconds: int) -> bool:
        renewed = real_renew(
            session, job_id=job_id, worker_id=worker_id, lease_seconds=lease_seconds
        )
        renewals.append(renewed)
        if not renewed:
            lost.set()
        return renewed

    monkeypatch.setattr(repository, "renew_lease", _spy)

    def _steal(_payload: dict, _context: handlers.HandlerContext) -> dict:
        with migrated_engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE jobs SET lease_owner = 'worker-thief', "
                    " lease_expires_at = now() + interval '5 minutes' WHERE id = :id"
                ),
                {"id": job_id},
            )
            conn.commit()
        assert lost.wait(timeout=15), "the heartbeat never observed the lost lease"
        return {"ignored": True}

    monkeypatch.setitem(handlers.HANDLERS, "steal", _steal)

    logger = configure_logging(log_stream)
    worker = Worker(
        session_factory,
        lease_seconds=LEASE_SECONDS,
        poll_seconds=0.01,
        worker_id="worker-victim",
        logger=logger,
    )
    try:
        assert worker.run_once() is True
    finally:
        logger.handlers.clear()

    assert renewals, "the heartbeat never renewed"
    assert all(renewed is False for renewed in renewals), (
        f"a renewal succeeded after the ownership change: {renewals}"
    )

    lease_lost = events_named(log_stream, "job.lease_lost")
    assert len(lease_lost) == 2, (
        f"expected the heartbeat's loss and the refused completion, got {lease_lost}"
    )
    assert all(event["worker_id"] == "worker-victim" for event in lease_lost)
    assert all(event["job_id"] == str(job_id) for event in lease_lost)
    assert all(event["attempt"] == 1 for event in lease_lost)
    assert all(event["level"] == "warning" for event in lease_lost)

    row = read_job(migrated_engine, job_id)
    assert row.status == "RUNNING", "the stale worker's completion changed the row"
    assert row.lease_owner == "worker-thief"
    assert row.result is None
    assert row.finished_at is None


# ---------------------------------------------------------------------------
# 4. The renewal thread's lifetime
# ---------------------------------------------------------------------------


def test_renewal_interval_is_a_third_of_the_lease() -> None:
    """The pure statement of the cadence, for the smallest lease and the default.

    This is what fails if the interval is changed to the full lease: the ratio is
    the safety margin, and expressing it as ``LEASE_SECONDS`` leaves no room for a
    slow renewal. It is checked here rather than through timing because the ratio is
    the contract and a wall-clock assertion would only sample it.

    The smallest lease the configuration accepts is one second, so the interval is
    always positive — there is no clamp, and none is needed.
    """
    assert renewal_interval_seconds(60) == pytest.approx(20.0)
    assert renewal_interval_seconds(3) == pytest.approx(1.0)
    assert renewal_interval_seconds(1) == pytest.approx(1 / 3)
    assert renewal_interval_seconds(1) < LEASE_SECONDS


def test_no_renewal_thread_survives_a_run_and_none_fires_after_it(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished job must not leave a renewer behind, nor a renewal in flight.

    The join is made observable rather than sampled: the handler waits until a renewal
    is parked inside ``renew_lease`` and *then* returns, so the parked renewal is
    still uncommitted at the moment the handler finishes. A worker that only signals
    its heartbeat instead of joining it returns with that renewal still in flight, and
    the renewal then commits after the job is finished. The worker must instead be
    blocked until the renewal completes.

    Three observations, because "no thread is alive right now" alone is weak — an
    event-driven loop stops within one tick whether or not anything joined it: the
    worker is blocked while the renewal is parked, no renewal commits after
    ``run_once`` returns, and no heartbeat thread is alive two ticks later. The last
    one covers a thread that was never signalled at all.
    """
    job_id = seed_job(migrated_engine, job_type="busy", payload={})
    renewal_in_flight = threading.Event()
    release_renewal = threading.Event()
    renewal_commits: list[float] = []
    real_renew = repository.renew_lease

    def _parked_renew(
        session: Session, *, job_id: uuid.UUID, worker_id: str, lease_seconds: int
    ) -> bool:
        renewal_in_flight.set()
        assert release_renewal.wait(timeout=30), "the test never released the renewal"
        renewed = real_renew(
            session, job_id=job_id, worker_id=worker_id, lease_seconds=lease_seconds
        )
        renewal_commits.append(time.monotonic())
        return renewed

    monkeypatch.setattr(repository, "renew_lease", _parked_renew)

    def _busy(_payload: dict, _context: handlers.HandlerContext) -> dict:
        # Return while a renewal is parked, so the join is what the next assertion
        # is really about.
        assert renewal_in_flight.wait(timeout=15), "the heartbeat never renewed"
        return {"done": True}

    monkeypatch.setitem(handlers.HANDLERS, "busy", _busy)

    logger = configure_logging(log_stream)
    worker = Worker(
        session_factory,
        lease_seconds=LEASE_SECONDS,
        poll_seconds=0.01,
        worker_id="worker-lifetime",
        logger=logger,
    )
    runner = threading.Thread(target=worker.run_once, daemon=True)
    runner.start()
    try:
        assert renewal_in_flight.wait(timeout=15), "the heartbeat never renewed"
        # Long enough for a worker that did not join to have finished the completion
        # write and returned.
        time.sleep(0.4)
        assert runner.is_alive(), (
            "the worker returned while a renewal was still uncommitted; stop() did not join"
        )
        assert not renewal_commits, "the renewal committed before the worker returned"
    finally:
        release_renewal.set()
        runner.join(timeout=30)
        logger.handlers.clear()

    assert not runner.is_alive()
    returned_at = time.monotonic()
    assert renewal_commits, "the parked renewal never committed"
    assert max(renewal_commits) <= returned_at

    row = read_job(migrated_engine, job_id)
    assert row.status == "SUCCEEDED"
    assert row.lease_expires_at is None, "a finished row was left looking in-flight"
    before_wait = (row.lease_expires_at, row.lease_owner, row.finished_at, row.updated_at)

    assert len(live_heartbeat_threads()) == 0, (
        f"a renewal thread outlived the handler: {live_heartbeat_threads()}"
    )

    # A leaked renewal would tick at ~lease/3 and push the finished row's lease
    # forward. Give it two opportunities, then compare the same fields.
    interval = renewal_interval_seconds(LEASE_SECONDS)
    time.sleep(2 * interval + 0.2)
    after = read_job(migrated_engine, job_id)
    after_wait = (after.lease_expires_at, after.lease_owner, after.finished_at, after.updated_at)
    assert after_wait == before_wait, "a leaked renewer changed a finished job's row"
    assert after.lease_expires_at is None, "a leaked renewer revived a finished job's lease"
    assert after.status == "SUCCEEDED"
    assert len(live_heartbeat_threads()) == 0, (
        f"a renewal thread appeared after the run: {live_heartbeat_threads()}"
    )


def test_renewal_thread_is_joined_even_when_the_handler_raises(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``finally`` must join on the raising path too.

    A renewal is parked inside ``renew_lease`` while the handler raises. The worker
    must still be blocked when it is observed a moment later: if it had already
    returned, the renewal would be free to commit after the failure write. The
    parked renewal is then released and the failure recording must land.
    """
    job_id = seed_job(migrated_engine, job_type="raises-while-renewing", payload={})
    renewal_in_flight = threading.Event()
    release_renewal = threading.Event()
    renewal_commits: list[float] = []
    real_renew = repository.renew_lease

    def _blocking_renew(
        session: Session, *, job_id: uuid.UUID, worker_id: str, lease_seconds: int
    ) -> bool:
        renewal_in_flight.set()
        assert release_renewal.wait(timeout=30), "the test never released the renewal"
        renewed = real_renew(
            session, job_id=job_id, worker_id=worker_id, lease_seconds=lease_seconds
        )
        renewal_commits.append(time.monotonic())
        return renewed

    monkeypatch.setattr(repository, "renew_lease", _blocking_renew)

    def _raises(_payload: dict, _context: handlers.HandlerContext) -> dict:
        assert renewal_in_flight.wait(timeout=15), "the heartbeat never renewed"
        raise RuntimeError("handler exploded")

    monkeypatch.setitem(handlers.HANDLERS, "raises-while-renewing", _raises)

    logger = configure_logging(log_stream)
    worker = Worker(
        session_factory,
        lease_seconds=LEASE_SECONDS,
        poll_seconds=0.01,
        worker_id="worker-raising",
        logger=logger,
    )
    runner = threading.Thread(target=worker.run_once, daemon=True)
    runner.start()
    try:
        assert renewal_in_flight.wait(timeout=15), "the heartbeat never renewed"
        # Long enough for an unjoined worker to have run the failure write and
        # returned; a joined one is parked on the renewal.
        time.sleep(0.4)
        assert runner.is_alive(), (
            "the worker returned while a renewal was still in flight; stop() did not join"
        )
    finally:
        release_renewal.set()
        runner.join(timeout=30)
        logger.handlers.clear()

    assert not runner.is_alive()
    returned_at = time.monotonic()
    assert renewal_commits, "the parked renewal never completed"
    assert max(renewal_commits) <= returned_at
    assert len(live_heartbeat_threads()) == 0

    row = read_job(migrated_engine, job_id)
    assert row.status == "PENDING", "a handler failure must still requeue"
    assert row.last_error == "RuntimeError: handler exploded"
    assert row.lease_owner is None
    assert events_named(log_stream, "job.retrying"), "the requeue was not logged"


# ---------------------------------------------------------------------------
# A renewal that raises costs one renewal, not the heartbeat or the worker
# ---------------------------------------------------------------------------


def test_a_failing_renewal_is_logged_by_class_and_the_heartbeat_continues(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database error in one renewal must not kill the thread or the worker.

    The first renewal raises a message carrying a secret; the next one must still
    happen, and the handler must still complete. Logged by class alone, never by
    message or traceback, per ``app.logging``'s contract.

    Note the failure is injected at the *statement* level rather than by breaking the
    connection, so the heartbeat's own session/commit path still runs: the test is
    about what the worker does with the exception, not about reproducing a dead
    database.
    """
    secret = "lease-renew-secret-4d21"
    job_id = seed_job(migrated_engine, job_type="survives-renewal-error", payload={})
    attempts: list[int] = []
    real_renew = repository.renew_lease

    def _flaky_renew(
        session: Session, *, job_id: uuid.UUID, worker_id: str, lease_seconds: int
    ) -> bool:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError(f"connection dropped while renewing {secret}")
        return real_renew(session, job_id=job_id, worker_id=worker_id, lease_seconds=lease_seconds)

    monkeypatch.setattr(repository, "renew_lease", _flaky_renew)

    def _slow(_payload: dict, _context: handlers.HandlerContext) -> dict:
        # Three renewal ticks at lease/3, so a heartbeat that died on the first
        # error is observable.
        time.sleep(1.2)
        return {"done": True}

    monkeypatch.setitem(handlers.HANDLERS, "survives-renewal-error", _slow)

    logger = configure_logging(log_stream)
    worker = Worker(
        session_factory,
        lease_seconds=LEASE_SECONDS,
        poll_seconds=0.01,
        worker_id="worker-flaky-renewal",
        logger=logger,
    )
    try:
        assert worker.run_once() is True
    finally:
        logger.handlers.clear()

    output = log_stream.getvalue()
    assert secret not in output, "a renewal exception message reached the logs"
    assert "connection dropped while renewing" not in output

    records = [json.loads(line) for line in output.splitlines() if line.strip()]
    errors = [record for record in records if record["level"] == "error"]
    assert errors, "a failing renewal must be logged"
    assert any("error_class=RuntimeError" in record["message"] for record in errors)
    assert all("exception" not in record for record in errors), (
        "a failing renewal must not log a traceback"
    )

    assert len(attempts) >= 3, (
        f"the heartbeat stopped after a renewal raised: {len(attempts)} attempt(s) in 1.2s"
    )
    row = read_job(migrated_engine, job_id)
    assert row.status == "SUCCEEDED", "a failing renewal cost the job"
    assert row.result == {"done": True}
    assert not events_named(log_stream, "job.lease_lost")


# ---------------------------------------------------------------------------
# 5. The reconciler guard: a heartbeating handler past the grace window
# ---------------------------------------------------------------------------


def test_heartbeating_handler_past_the_grace_window_applies_exactly_once(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    second_factory: sessionmaker[Session],
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this PR exists for, driven end to end.

    The handler claims its effect, outlives ``EFFECT_RECONCILE_GRACE_SECONDS`` while
    its lease stays alive, and a second worker's cycle runs the reconciler against
    that row mid-handler. The reconciler must see the row as a candidate and refuse
    to lock it, because the lease is live.

    Kills the mutation that removes the live-lease predicate from
    ``LOCK_PENDING_EFFECT_SQL``: the reconciler then applies the effect underneath
    the handler and the final count is two.
    """
    recorder = EffectRecorder()
    job_id = seed_job(migrated_engine, job_type="slow-notify", payload={})
    probe_results: list[bool] = []
    candidates_seen: list[list[tuple[uuid.UUID, str, dict | None]]] = []
    real_pending = repository.pending_effects

    def _spy_pending(
        session: Session, *, grace_seconds: int, limit: int = 100
    ) -> list[tuple[uuid.UUID, str, dict | None]]:
        rows = real_pending(session, grace_seconds=grace_seconds, limit=limit)
        candidates_seen.append(rows)
        return rows

    monkeypatch.setattr(repository, "pending_effects", _spy_pending)

    def _probe() -> None:
        """A second worker's whole cycle, from inside the running handler."""
        worker_b = Worker(
            second_factory,
            lease_seconds=LEASE_SECONDS,
            poll_seconds=0.01,
            worker_id="worker-b",
            logger=configure_logging(log_stream),
            effect_reconcile_grace_seconds=GRACE_SECONDS,
            apply_effect=recorder,
        )
        probe_results.append(worker_b.run_once())

    def _slow_notify(_payload: dict, context: handlers.HandlerContext) -> dict:
        key = "notify"
        detail = {"k": 1}
        with context.session_factory() as session:
            claimed = repository.claim_effect(
                session, job_id=context.job_id, effect_key=key, detail=detail
            )
            session.commit()
        if not claimed:
            return {"skipped": True}

        # Outlive the grace window while the heartbeat keeps the lease alive.
        time.sleep(GRACE_SECONDS + 0.4)
        _probe()

        context.apply_effect(context.job_id, key, detail)
        with context.session_factory() as session:
            repository.mark_effect_applied(session, job_id=context.job_id, effect_key=key)
            session.commit()
        return {"applied": True}

    monkeypatch.setitem(handlers.HANDLERS, "slow-notify", _slow_notify)

    logger = configure_logging(log_stream)
    worker_a = Worker(
        session_factory,
        lease_seconds=LEASE_SECONDS,
        poll_seconds=0.01,
        worker_id="worker-a",
        logger=logger,
        effect_reconcile_grace_seconds=GRACE_SECONDS,
        apply_effect=recorder,
    )
    try:
        assert worker_a.run_once() is True
    finally:
        logger.handlers.clear()

    assert probe_results == [False], "the reconciler cycle claimed another worker's job"
    seen = [row for call in candidates_seen for row in call]
    assert (job_id, "notify", {"k": 1}) in seen, (
        "the reconciler never saw the row as a candidate, so the guard was not exercised"
    )
    assert recorder.count == 1, (
        f"the effect was applied {recorder.count} time(s): the reconciler took a live job's row"
    )
    assert read_effect(migrated_engine, job_id).status == "APPLIED"
    assert count_effects(migrated_engine) == 1, "a second outbox row was inserted"
    assert not events_named(log_stream, "effect.reconciled"), (
        "the reconciler reported applying a row it should have skipped"
    )

    row = read_job(migrated_engine, job_id)
    assert row.status == "SUCCEEDED"
    assert row.attempt == 1


# ---------------------------------------------------------------------------
# 6. The guard still reconciles a job nobody is working on
# ---------------------------------------------------------------------------


def test_guard_reconciles_a_dead_handler(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    log_stream: io.StringIO,
) -> None:
    """A run's dead handler must still have its effect applied, once.

    The positive control for the guard: the job is ``RUNNING`` with an exhausted
    attempt budget and an expired lease, so the sweep terminally fails it before the
    reconcile step, and by then no lease is left to protect it.
    """
    recorder = EffectRecorder()
    job_id = seed_job(migrated_engine, job_type="echo", payload={}, attempt=3, max_attempts=3)
    mark_job(
        migrated_engine,
        job_id,
        status="RUNNING",
        owner="dead-worker",
        lease_seconds_from_now=-300,
    )
    seed_effect(migrated_engine, job_id=job_id, age=600)

    logger = configure_logging(log_stream)
    try:
        assert (
            run_once(
                session_factory,
                worker_id="worker-reconciler",
                logger=logger,
                effect_reconcile_grace_seconds=GRACE_SECONDS,
                apply_effect=recorder,
            )
            is False
        )
    finally:
        logger.handlers.clear()

    assert recorder.applied == [(job_id, "notify")], "a dead handler's effect was not applied"
    assert read_effect(migrated_engine, job_id).status == "APPLIED"
    assert count_effects(migrated_engine) == 1

    row = read_job(migrated_engine, job_id)
    assert row.status == "FAILED", "the exhausted expired job should have been swept"
    assert row.lease_owner is None

    reconciled = events_named(log_stream, "effect.reconciled")
    assert len(reconciled) == 1
    assert reconciled[0]["age_seconds"] >= 600


@pytest.mark.parametrize(
    ("case", "status", "owner", "lease", "finished", "takes_it"),
    [
        ("running live lease", "RUNNING", "worker-live", 60.0, False, False),
        ("running expired lease", "RUNNING", "worker-dead", -300.0, False, True),
        ("succeeded", "SUCCEEDED", None, None, True, True),
        ("failed", "FAILED", None, None, True, True),
    ],
)
def test_lock_pending_effect_decision_matrix(
    migrated_engine: Engine,
    database_url: str,
    case: str,
    status: str,
    owner: str | None,
    lease: float | None,
    finished: bool,
    takes_it: bool,
) -> None:
    """The three verified behaviours, stated once each, at the guard itself.

    A live lease is the only case that is skipped, and it is skipped whatever the
    effect's age: the age filter lives in the unlocked read and is not repeated in
    the locked statement.
    """
    job_id = seed_job(migrated_engine, job_type="echo")
    mark_job(
        migrated_engine,
        job_id,
        status=status,
        owner=owner,
        lease_seconds_from_now=lease,
        finished=finished,
    )
    seed_effect(migrated_engine, job_id=job_id, age=600)

    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            locked = repository.lock_pending_effect(session, job_id=job_id, effect_key="notify")
            session.commit()
    finally:
        dispose(factory)

    if takes_it:
        assert locked is not None, f"{case}: the guard refused a row nobody owns"
        age_seconds, _detail = locked
        assert age_seconds >= 600
    else:
        assert locked is None, f"{case}: the guard took a live handler's row"
    assert read_effect(migrated_engine, job_id).status == "INTENT", (
        f"{case}: locking the row must not change its status"
    )


# ---------------------------------------------------------------------------
# 7. The lock is on the effect row, not the job row
# ---------------------------------------------------------------------------


def test_effect_lock_does_not_block_a_concurrent_heartbeat(
    migrated_engine: Engine, database_url: str
) -> None:
    """``FOR UPDATE OF je``, not bare ``FOR UPDATE`` — and the heartbeat's real statement.

    A bare ``FOR UPDATE`` on the join also locks the ``jobs`` row, which means the
    reconciler holds the heartbeat off for the whole apply-and-confirm. That inverts
    the guard: the heartbeat is what keeps a live handler's lease alive, so blocking
    it is how a live job becomes an expired one and gets reconciled.

    One session holds the effect lock; a second runs the heartbeat's own statement,
    ``repository.renew_lease``, in its own session and commits. Two things are
    therefore covered: the lock is not on the ``jobs`` row, and ``RENEW_LEASE_SQL``
    itself executes without blocking. A raw ``UPDATE`` here would only prove the
    first, leaving the statement the worker actually sends free to regress.

    ``lease_owner`` is the owner passed as ``worker_id``, and the result is asserted
    ``True``, so a renewal that matched zero rows cannot pass this for the wrong
    reason. The holder waits up to five seconds for the renewal, so a blocked
    heartbeat shows up as a slow one rather than as a hung test.
    """
    owner = "worker-dead"
    job_id = seed_job(migrated_engine, job_type="echo")
    mark_job(
        migrated_engine,
        job_id,
        status="RUNNING",
        owner=owner,
        lease_seconds_from_now=-300,
    )
    seed_effect(migrated_engine, job_id=job_id, age=600)

    locked = threading.Event()
    update_done = threading.Event()
    elapsed: list[float] = []
    renewed: list[bool] = []

    def _holder() -> None:
        factory = new_session_factory(database_url)
        try:
            with factory() as session:
                assert (
                    repository.lock_pending_effect(session, job_id=job_id, effect_key="notify")
                    is not None
                ), "the holder could not take the effect lock"
                locked.set()
                assert update_done.wait(timeout=5), "the heartbeat's renewal never completed"
                session.rollback()
        finally:
            dispose(factory)

    def _heartbeat() -> None:
        assert locked.wait(timeout=10), "the holder never took the lock"
        factory = new_session_factory(database_url)
        try:
            with factory() as session:
                started = time.monotonic()
                renewed.append(
                    repository.renew_lease(
                        session, job_id=job_id, worker_id=owner, lease_seconds=60
                    )
                )
                session.commit()
                elapsed.append(time.monotonic() - started)
                update_done.set()
        finally:
            dispose(factory)

    errors = run_concurrently(_holder, _heartbeat)
    assert errors == [None, None], f"a participant raised {errors}"
    assert renewed == [True], "the renewal matched no row, so the lock was never exercised"
    assert elapsed, "the heartbeat never ran"
    assert elapsed[0] < 2.0, (
        f"the heartbeat waited {elapsed[0]:.2f}s behind the effect lock; "
        "the lock covers the jobs row as well as the effect row"
    )

    row = read_job(migrated_engine, job_id)
    assert row.lease_expires_at is not None
    assert row.lease_owner == owner
    assert read_effect(migrated_engine, job_id).status == "INTENT"


# ---------------------------------------------------------------------------
# 8. A blocked renewal gives up, so ``stop``'s unbounded join cannot hang
# ---------------------------------------------------------------------------


def _call_with_deadline(call: Any, deadline: float) -> tuple[Any, dict[str, Any], float]:
    """Run ``call`` on a thread and give up waiting after ``deadline`` seconds.

    A blocked renewal is exactly what these tests must detect, so the wait on it has
    to be bounded here too — otherwise the mutation that removes the lock timeout
    turns the test into a hang rather than a failure. Returns the thread, whatever
    the call produced (``{"value": ...}`` or ``{"error": ...}``), and the elapsed time.
    """
    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            outcome["value"] = call()
        except BaseException as error:  # noqa: BLE001 - reported by the caller's assertions
            outcome["error"] = error

    thread = threading.Thread(target=_run, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(timeout=deadline)
    return thread, outcome, time.monotonic() - started


def test_a_blocked_renewal_gives_up_instead_of_waiting_on_the_job_row(
    migrated_engine: Engine, database_url: str
) -> None:
    """A reclaim holding the job row must not block the heartbeat indefinitely.

    ``_LeaseHeartbeat.stop`` joins without a timeout, and it has to: a renewal that
    committed after the completion would leave a finished row holding a live lease.
    That is only safe if the statement the join waits on is itself bounded, so
    ``renew_lease`` sets ``SET LOCAL lock_timeout`` before its update. Held up by a
    concurrent ``SELECT ... FOR UPDATE`` on the same row, it must fail with SQLSTATE
    ``55P03`` in roughly half a second rather than waiting for the holder to finish.

    The failure is asserted as an exception rather than a ``False`` return because a
    lock is not a lost lease: ``False`` would stop the heartbeat permanently.
    """
    job_id = seed_job(migrated_engine, job_type="echo")
    mark_job(
        migrated_engine,
        job_id,
        status="RUNNING",
        owner="worker-holder",
        lease_seconds_from_now=60,
    )
    before = read_job(migrated_engine, job_id)

    factory = new_session_factory(database_url)
    holder = new_session_factory(database_url)
    holder_session = holder()
    thread: threading.Thread | None = None

    def _renew() -> bool:
        with factory() as session:
            return repository.renew_lease(
                session, job_id=job_id, worker_id="worker-holder", lease_seconds=600
            )

    try:
        holder_session.execute(
            text("SELECT id FROM jobs WHERE id = :id FOR UPDATE"), {"id": job_id}
        )
        thread, outcome, elapsed = _call_with_deadline(_renew, deadline=5.0)
        assert not thread.is_alive(), (
            f"the renewal was still blocked after {elapsed:.2f}s on the job row; "
            "the statement has no lock timeout"
        )
    finally:
        holder_session.rollback()
        holder_session.close()
        if thread is not None:
            thread.join(timeout=10)
        dispose(factory)
        dispose(holder)

    error = outcome.get("error")
    assert isinstance(error, OperationalError), (
        f"the renewal failed for a reason other than the lock timeout: {outcome!r}"
    )
    assert getattr(error.orig, "sqlstate", None) == "55P03", (
        f"expected SQLSTATE 55P03 from the lock timeout, got {error!r}"
    )
    assert elapsed < 5.0, f"the renewal blocked for {elapsed:.2f}s; the lock timeout is not set"
    assert elapsed >= 0.25, (
        f"the renewal failed after {elapsed:.3f}s, before a 500ms timeout could expire; "
        "something other than the timeout raised"
    )

    after = read_job(migrated_engine, job_id)
    assert after.lease_expires_at == before.lease_expires_at, (
        "a renewal that timed out still moved the lease"
    )


def test_a_blocked_renewal_costs_one_tick_and_the_heartbeat_then_recovers(
    migrated_engine: Engine,
    database_url: str,
    log_stream: io.StringIO,
) -> None:
    """A timed-out renewal is one missed tick, not a lost lease or a dead heartbeat.

    The job row is held by a second session, exactly as a concurrent reclaim holds
    it. ``_renew_once`` must return ``True`` — the ``False`` that means "stop
    renewing" is reserved for a zero-row renewal — with the failure logged by class
    and nothing of the driver's message or traceback on the line. Once the holder
    lets go, the next renewal succeeds and moves the lease, which is why the
    unbounded join in ``stop`` cannot hang on a finished handler.
    """
    owner = "worker-blocked"
    job_id = seed_job(migrated_engine, job_type="echo")
    mark_job(
        migrated_engine,
        job_id,
        status="RUNNING",
        owner=owner,
        lease_seconds_from_now=60,
    )
    before = read_job(migrated_engine, job_id)

    logger = configure_logging(log_stream)
    factory = new_session_factory(database_url)
    holder = new_session_factory(database_url)
    worker = Worker(
        factory,
        lease_seconds=60,
        poll_seconds=0.01,
        worker_id=owner,
        logger=logger,
    )
    heartbeat = _LeaseHeartbeat(
        factory,
        job_id=job_id,
        attempt=1,
        worker_id=owner,
        lease_seconds=60,
        logger=logger,
        on_write_error=lambda action, error: worker._log_write_error(action, None, error),
    )
    holder_session = holder()
    thread: threading.Thread | None = None
    try:
        holder_session.execute(
            text("SELECT id FROM jobs WHERE id = :id FOR UPDATE"), {"id": job_id}
        )
        thread, outcome, elapsed = _call_with_deadline(heartbeat._renew_once, deadline=5.0)
        assert not thread.is_alive(), (
            f"the heartbeat's renewal was still blocked after {elapsed:.2f}s; "
            "stop() would join this thread forever on a finished handler"
        )
    finally:
        holder_session.rollback()
        holder_session.close()
        if thread is not None:
            thread.join(timeout=10)
        dispose(holder)

    assert outcome.get("error") is None, f"the blocked renewal escaped: {outcome!r}"
    assert outcome["value"] is True, (
        "a blocked renewal stopped the heartbeat instead of costing one tick"
    )

    try:
        recovered = heartbeat._renew_once()
    finally:
        dispose(factory)
        logger.handlers.clear()

    assert recovered is True, "the heartbeat did not recover once the lock was released"
    assert heartbeat.lease_lost is False

    records = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
    errors = [record for record in records if record["level"] == "error"]
    assert len(errors) == 1, f"expected exactly one logged renewal failure, got {errors}"
    assert "error_class=OperationalError" in errors[0]["message"]
    assert "lease renewal failed" in errors[0]["message"]
    assert "exception" not in errors[0], "a timed-out renewal must not log a traceback"
    assert "55P03" not in log_stream.getvalue(), "the driver's SQLSTATE reached the log"
    assert "lock timeout" not in log_stream.getvalue().lower()
    assert not events_named(log_stream, "job.lease_lost")

    after = read_job(migrated_engine, job_id)
    assert after.lease_owner == owner
    assert after.lease_expires_at is not None
    assert after.lease_expires_at > before.lease_expires_at, (
        "the renewal after the lock was released did not extend the lease"
    )
