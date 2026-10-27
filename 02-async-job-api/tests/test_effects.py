"""Tests for the effect outbox and the reconciler (PR-2-04).

Syllabus falsification case 4 — "external side effect succeeds but success
persistence fails: retry must be safe through external idempotency or explicit
reconciliation" — is the gate of this PR. ``test_effect_is_applied_once_when_completion_fails``
is that gate: it drives a real job through a failed completion, a reclaim and a
re-run, and asserts the effect was applied exactly once.

"Applied exactly once" is observed through an injected ``apply_effect`` callable,
not through a log line or a database count alone: the callable is the same seam the
handler and the reconciler both use, so every application — normal or reconciled —
lands in one list. The outbox row count is asserted alongside it as the durable
evidence, because the counter lives only for the test.
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app import handlers, repository
from app.logging import configure_logging
from app.worker import run_once
from tests.db_helpers import (
    clear_jobs,
    dispose,
    new_session_factory,
    read_job,
    run_concurrently,
    seed_job,
)

SECRET = "effect-detail-secret-7c1f"


class EffectRecorder:
    """The injected external call. Every application is appended, in order."""

    def __init__(self) -> None:
        self.applied: list[tuple[uuid.UUID, str]] = []

    def __call__(self, job_id: uuid.UUID, effect_key: str, detail: dict[str, Any] | None) -> None:
        self.applied.append((job_id, effect_key))

    @property
    def count(self) -> int:
        return len(self.applied)


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
def recorder() -> EffectRecorder:
    return EffectRecorder()


def logged_events(stream: io.StringIO) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    return [record for record in records if record.get("event")]


def claim_effect_once(
    database_url: str,
    *,
    job_id: uuid.UUID,
    effect_key: str = "notify",
    detail: dict[str, Any] | None = None,
) -> bool:
    """Claim on a factory whose every checkout is a fresh physical connection."""
    factory = new_session_factory(database_url)
    try:
        with factory() as session:
            claimed = repository.claim_effect(
                session, job_id=job_id, effect_key=effect_key, detail=detail
            )
            session.commit()
            return claimed
    finally:
        dispose(factory)


def seed_effect(
    engine: Engine,
    *,
    job_id: uuid.UUID,
    effect_key: str = "notify",
    status: str = "INTENT",
    detail: dict[str, Any] | None = None,
    age_seconds: int = 0,
) -> None:
    """Insert an outbox row directly, backdated by ``age_seconds``."""
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO job_effects "
                "(job_id, effect_key, status, detail, created_at, applied_at) "
                "VALUES (:job_id, :effect_key, :status, CAST(:detail AS jsonb), "
                " now() - make_interval(secs => :age), "
                " CASE WHEN :status = 'APPLIED' THEN now() ELSE NULL END)"
            ),
            {
                "job_id": job_id,
                "effect_key": effect_key,
                "status": status,
                "detail": None if detail is None else json.dumps(detail),
                "age": age_seconds,
            },
        )
        conn.commit()


def read_effect(engine: Engine, job_id: uuid.UUID, effect_key: str = "notify") -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT status, applied_at, detail FROM job_effects "
                "WHERE job_id = :job_id AND effect_key = :effect_key"
            ),
            {"job_id": job_id, "effect_key": effect_key},
        ).one_or_none()


def count_effects(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM job_effects")).scalar_one()


def seed_unclaimable_job(engine: Engine, *, job_type: str = "echo") -> uuid.UUID:
    """A job no worker can claim, so a cycle only exercises the reconciler.

    Attempts are exhausted (``attempt == max_attempts``), which is what the claim's
    ``attempt < max_attempts`` guard refuses. The row is deliberately left
    ``PENDING`` rather than ``RUNNING``: a ``RUNNING`` row would exercise the sweep,
    and these tests are about the reconcile step that runs before the claim.
    """
    return seed_job(engine, job_type=job_type, attempt=3, max_attempts=3)


# ---------------------------------------------------------------------------
# Outbox claim semantics
# ---------------------------------------------------------------------------


def test_claim_effect_returns_true_once_then_false_from_a_fresh_connection(
    migrated_engine: Engine, database_url: str
) -> None:
    """The second claim must lose, even on a different physical connection."""
    job_id = seed_job(migrated_engine, job_type="notify")

    assert claim_effect_once(database_url, job_id=job_id) is True, "the first claim must win"
    assert claim_effect_once(database_url, job_id=job_id) is False, (
        "a second claim for the same (job_id, effect_key) must not win"
    )
    assert count_effects(migrated_engine) == 1, "the losing claim must not insert a row"
    assert read_effect(migrated_engine, job_id).status == "INTENT"


def test_claim_effect_is_per_effect_key_not_per_job(
    migrated_engine: Engine, database_url: str
) -> None:
    """Two distinct effects on one job are independent claims."""
    job_id = seed_job(migrated_engine, job_type="notify")

    assert claim_effect_once(database_url, job_id=job_id, effect_key="charge") is True
    assert claim_effect_once(database_url, job_id=job_id, effect_key="email") is True
    assert count_effects(migrated_engine) == 2


def test_crash_between_claim_and_apply_leaves_the_effect_intent(
    migrated_engine: Engine, database_url: str
) -> None:
    """The crash window the reconciler exists for: claimed, never confirmed."""
    job_id = seed_job(migrated_engine, job_type="notify")

    assert claim_effect_once(database_url, job_id=job_id, detail={"v": 1}) is True
    # No mark_effect_applied: this is the state a SIGKILL leaves behind.

    row = read_effect(migrated_engine, job_id)
    assert row.status == "INTENT"
    assert row.applied_at is None
    assert row.detail == {"v": 1}


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


def test_reconciler_applies_a_stranded_intent(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    recorder: EffectRecorder,
) -> None:
    job_id = seed_unclaimable_job(migrated_engine)
    seed_effect(migrated_engine, job_id=job_id, age_seconds=600, detail={"v": 1})

    # False means no job was claimed; the cycle still reconciled.
    assert (
        run_once(
            session_factory,
            worker_id="worker-reconciler",
            effect_reconcile_grace_seconds=30,
            apply_effect=recorder,
        )
        is False
    )

    assert recorder.applied == [(job_id, "notify")], "the stranded effect was not applied"
    row = read_effect(migrated_engine, job_id)
    assert row.status == "APPLIED"
    assert row.applied_at is not None

    with session_factory() as session:
        assert repository.pending_effects(session, grace_seconds=30) == [], (
            "the pending list must empty"
        )


def test_reconciler_leaves_a_young_intent_alone(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    recorder: EffectRecorder,
) -> None:
    """A live handler's in-flight claim must never be stolen by the reconciler."""
    job_id = seed_unclaimable_job(migrated_engine)
    seed_effect(migrated_engine, job_id=job_id, age_seconds=1)

    assert (
        run_once(
            session_factory,
            worker_id="worker-reconciler",
            effect_reconcile_grace_seconds=30,
            apply_effect=recorder,
        )
        is False
    )

    assert recorder.count == 0, "a young INTENT was applied inside the grace window"
    assert read_effect(migrated_engine, job_id).status == "INTENT"


def test_reconciler_is_idempotent_across_two_runs(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    recorder: EffectRecorder,
) -> None:
    """A second cycle must find nothing and confirm nothing a second time."""
    job_id = seed_unclaimable_job(migrated_engine)
    seed_effect(migrated_engine, job_id=job_id, age_seconds=600)

    for _ in range(2):
        run_once(
            session_factory,
            worker_id="worker-reconciler",
            effect_reconcile_grace_seconds=30,
            apply_effect=recorder,
        )

    assert recorder.count == 1, "the second cycle applied the effect again"
    assert count_effects(migrated_engine) == 1

    # The mechanism behind that: a confirmation only reports a transition while the
    # row is still INTENT, so a reconciler that lost a race cannot double-apply on
    # the strength of a second "applied" answer.
    with session_factory() as session:
        assert (
            repository.mark_effect_applied(session, job_id=job_id, effect_key="notify") is False
        ), "confirming an already-APPLIED effect must not report a transition"
        session.commit()


def test_effect_lock_admits_one_reconciler_and_the_loser_sees_nothing(
    migrated_engine: Engine, database_url: str
) -> None:
    """Two workers reconcile every cycle, so they *will* race for the same row.

    The race is driven deterministically rather than hoped for: reconciler A takes
    the row lock and holds it open, B is shown to be blocked, then A commits and B
    is released. B must then find nothing — the row is ``APPLIED`` — so only one of
    them applies the effect. Without the ``FOR UPDATE`` in ``lock_pending_effect``
    both would hold a lockable row and both would apply.
    """
    job_id = seed_unclaimable_job(migrated_engine)
    seed_effect(migrated_engine, job_id=job_id, age_seconds=600)

    a_locked = threading.Event()
    b_attempted = threading.Event()
    a_committed = threading.Event()
    b_result: list[tuple[int, dict | None] | None] = []

    def _reconciler_a() -> None:
        factory = new_session_factory(database_url)
        try:
            with factory() as session:
                assert (
                    repository.lock_pending_effect(session, job_id=job_id, effect_key="notify")
                    is not None
                ), "the first reconciler must win the lock"
                a_locked.set()
                assert b_attempted.wait(timeout=10), "reconciler B never started"
                # Give B time to actually reach the lock and block on it.
                time.sleep(0.2)
                assert not b_result, "reconciler B read the row while A held the lock"
                repository.mark_effect_applied(session, job_id=job_id, effect_key="notify")
                session.commit()
                a_committed.set()
        finally:
            dispose(factory)

    def _reconciler_b() -> None:
        assert a_locked.wait(timeout=10), "reconciler A never took the lock"
        factory = new_session_factory(database_url)
        try:
            with factory() as session:
                b_attempted.set()
                b_result.append(
                    repository.lock_pending_effect(session, job_id=job_id, effect_key="notify")
                )
                session.commit()
        finally:
            dispose(factory)

    errors = run_concurrently(_reconciler_a, _reconciler_b)

    assert errors == [None, None], f"a reconciler raised {errors}"
    assert a_committed.is_set()
    assert b_result == [None], "the loser acquired a row the winner had confirmed"
    assert read_effect(migrated_engine, job_id).status == "APPLIED"


def test_reconciler_failure_does_not_stop_the_claim(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    recorder: EffectRecorder,
) -> None:
    """A failing apply is logged by class and the cycle still claims work."""

    def _explode(job_id: uuid.UUID, effect_key: str, detail: dict | None) -> None:
        raise RuntimeError(f"vendor rejected {SECRET}")

    job_id = seed_unclaimable_job(migrated_engine)
    seed_effect(migrated_engine, job_id=job_id, age_seconds=600)
    claimable = seed_job(migrated_engine, payload={"ok": True})

    stream = io.StringIO()
    logger = configure_logging(stream)
    try:
        handled = run_once(
            session_factory,
            worker_id="w",
            logger=logger,
            effect_reconcile_grace_seconds=30,
            apply_effect=_explode,
        )
    finally:
        logger.handlers.clear()

    assert handled is True, "a reconcile failure must not stop the cycle's claim"
    assert read_job(migrated_engine, claimable).status == "SUCCEEDED"
    assert read_effect(migrated_engine, job_id).status == "INTENT", (
        "a failed apply must not be confirmed"
    )
    assert SECRET not in stream.getvalue()


def test_reconciler_logs_no_effect_detail(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    recorder: EffectRecorder,
) -> None:
    """``detail`` is handler-authored; the reconcile event must carry none of it."""
    job_id = seed_unclaimable_job(migrated_engine)
    seed_effect(
        migrated_engine,
        job_id=job_id,
        age_seconds=600,
        detail={"effect_key": "notify", "api_key": SECRET},
    )

    stream = io.StringIO()
    logger = configure_logging(stream)
    try:
        run_once(
            session_factory,
            worker_id="worker-reconciler",
            logger=logger,
            effect_reconcile_grace_seconds=30,
            apply_effect=recorder,
        )
    finally:
        logger.handlers.clear()

    output = stream.getvalue()
    assert SECRET not in output, "the effect detail reached the logs"
    assert "api_key" not in output, "a detail key reached the logs"

    reconciled = [e for e in logged_events(stream) if e["event"] == "effect.reconciled"]
    assert len(reconciled) == 1
    event = reconciled[0]
    assert event["worker_id"] == "worker-reconciler"
    assert event["job_id"] == str(job_id)
    assert event["effect_key"] == "notify"
    assert event["age_seconds"] >= 600
    assert "detail" not in event


# ---------------------------------------------------------------------------
# Falsification case 4 — the gate of this PR
# ---------------------------------------------------------------------------


def test_effect_is_applied_once_when_completion_fails(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    recorder: EffectRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Side effect succeeds, success persistence fails, the retry must not re-apply.

    The first cycle runs the handler all the way through: it claims the effect,
    applies it, confirms it in the outbox — and then the job completion is forced to
    fail, which is exactly the crash window in falsification case 4. The job stays
    ``RUNNING`` on its lease, the next cycle reclaims it, and the handler runs again.
    The second run must lose the claim and report ``skipped``, so the external call
    stays at one application.

    ``handler_entries`` proves the second run really re-ran the handler. Without it
    the test could pass because the retry never happened at all, which would make the
    single application meaningless.
    """
    job_id = seed_job(migrated_engine, job_type="notify", payload={"effect_key": "charge-order-42"})
    handler_entries = 0
    real_notify = handlers.HANDLERS["notify"]

    def _counting_notify(payload: dict, context: handlers.HandlerContext) -> dict:
        nonlocal handler_entries
        handler_entries += 1
        return real_notify(payload, context)

    monkeypatch.setitem(handlers.HANDLERS, "notify", _counting_notify)

    def _completion_always_fails(*args: object, **kwargs: object) -> int:
        raise RuntimeError("database went away while storing the success")

    monkeypatch.setattr(repository, "complete_job", _completion_always_fails)

    assert (
        run_once(
            session_factory,
            worker_id="worker-a",
            effect_reconcile_grace_seconds=30,
            apply_effect=recorder,
        )
        is True
    )

    stuck = read_job(migrated_engine, job_id)
    assert stuck.status == "RUNNING", "the forced completion failure did not stick"
    assert stuck.result is None
    assert handler_entries == 1, "the handler did not run on the first cycle"
    assert recorder.count == 1, "the handler must have applied the effect once"
    row = read_effect(migrated_engine, job_id, "charge-order-42")
    assert row.status == "APPLIED", "the effect was confirmed before completion failed"

    # Recover: the next cycle reclaims the expired-lease row and re-runs the handler.
    # ``undo`` also drops the counting handler, so re-register it before the retry.
    monkeypatch.undo()
    monkeypatch.setitem(handlers.HANDLERS, "notify", _counting_notify)
    with migrated_engine.connect() as conn:
        conn.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '5 minutes' WHERE id = :id"),
            {"id": job_id},
        )
        conn.commit()

    assert (
        run_once(
            session_factory,
            worker_id="worker-b",
            effect_reconcile_grace_seconds=30,
            apply_effect=recorder,
        )
        is True
    )

    recovered = read_job(migrated_engine, job_id)
    assert recovered.status == "SUCCEEDED"
    assert recovered.attempt == 2, "the retry must charge a second attempt"
    assert recovered.result == {"skipped": True, "effect_key": "charge-order-42"}

    assert handler_entries == 2, "the reclaimed job must re-enter the handler"
    assert recorder.count == 1, "the external side effect was applied twice"
    assert count_effects(migrated_engine) == 1, "the retry inserted a second outbox row"
    assert read_effect(migrated_engine, job_id, "charge-order-42").status == "APPLIED"


def test_reclaimed_job_reconciles_an_effect_the_crash_left_intent(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    recorder: EffectRecorder,
) -> None:
    """The other half of case 4: the crash landed *before* the effect was applied.

    A stranded ``INTENT`` is not silently dropped and not applied twice — the
    reconciler applies it once and confirms it, so the job is complete and the
    external call happened exactly once even though no handler finished it.
    """
    job_id = seed_unclaimable_job(migrated_engine, job_type="notify")
    seed_effect(migrated_engine, job_id=job_id, effect_key="notify", age_seconds=600)

    run_once(
        session_factory,
        worker_id="worker-reconciler",
        effect_reconcile_grace_seconds=30,
        apply_effect=recorder,
    )
    run_once(
        session_factory,
        worker_id="worker-reconciler",
        effect_reconcile_grace_seconds=30,
        apply_effect=recorder,
    )

    assert recorder.count == 1
    assert read_effect(migrated_engine, job_id).status == "APPLIED"


# ---------------------------------------------------------------------------
# Schema constraints
# ---------------------------------------------------------------------------


def test_ck_effect_status_rejects_invalid_value(migrated_engine: Engine) -> None:
    """ck_effect_status must reject values outside the allowed set."""
    job_id = seed_job(migrated_engine, job_type="notify")
    with pytest.raises(IntegrityError):
        with migrated_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO job_effects (job_id, effect_key, status) "
                    "VALUES (:id, 'notify', 'INVALID_STATUS')"
                ),
                {"id": job_id},
            )
            conn.commit()


def test_ck_effect_applied_at_rejects_applied_without_timestamp(migrated_engine: Engine) -> None:
    """APPLIED with a NULL applied_at must be impossible to store."""
    job_id = seed_job(migrated_engine, job_type="notify")
    with pytest.raises(IntegrityError):
        with migrated_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO job_effects (job_id, effect_key, status, applied_at) "
                    "VALUES (:id, 'notify', 'APPLIED', NULL)"
                ),
                {"id": job_id},
            )
            conn.commit()


def test_ck_effect_applied_at_rejects_intent_with_timestamp(migrated_engine: Engine) -> None:
    """INTENT carrying an applied_at is equally inconsistent."""
    job_id = seed_job(migrated_engine, job_type="notify")
    with pytest.raises(IntegrityError):
        with migrated_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO job_effects (job_id, effect_key, status, applied_at) "
                    "VALUES (:id, 'notify', 'INTENT', now())"
                ),
                {"id": job_id},
            )
            conn.commit()


def test_effect_rows_cascade_with_their_job(migrated_engine: Engine) -> None:
    """Deleting the job must not leave an orphan effect behind."""
    job_id = seed_job(migrated_engine, job_type="notify")
    seed_effect(migrated_engine, job_id=job_id)

    with migrated_engine.connect() as conn:
        conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
        conn.commit()

    assert count_effects(migrated_engine) == 0
