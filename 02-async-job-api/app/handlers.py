"""Job handler registry.

A handler takes the job payload plus a :class:`HandlerContext` and returns the
result dict stored on the job. The context exists so a handler can reach the
database without the registry holding global state: an effectful handler must
claim its side effect in the outbox *before* it performs it (falsification case 4).

``echo`` is the do-nothing handler the claim and lease tests interrupt. ``notify``
is the effectful one: it claims ``(job_id, effect_key)`` at most once per job and
applies only when the claim wins, so a reclaimed job re-runs the handler without
re-applying the effect.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app import repository

MAX_ERROR_CHARS = 2000
UNKNOWN_TYPE_MESSAGE = "no handler registered for type '{job_type}'"
INVALID_RESULT_MESSAGE = "invalid handler result: expected dict, got {result_type}"

DEFAULT_EFFECT_KEY = "notify"

#: ``(job_id, effect_key, detail) -> None``. Performs the external call for an
#: effect that has already been claimed in the outbox.
EffectApplier = Callable[[uuid.UUID, str, dict[str, Any] | None], None]


@dataclass(frozen=True)
class HandlerContext:
    """Everything a handler needs besides its payload.

    ``session_factory`` rather than a live session: an effect claim must be
    committed *before* the external call and the confirmation must be another
    transaction, so the handler opens two short sessions instead of borrowing one
    from the worker and holding it open across the effect.

    ``apply_effect`` is the external call itself, injected so it is a seam rather
    than a module global. Production gets the simulation below; a test passes a
    counter, and the reconciler uses the same callable, so "applied once" is
    observed the same way on both paths.
    """

    session_factory: sessionmaker[Session]
    job_id: uuid.UUID
    apply_effect: EffectApplier


Handler = Callable[[dict, HandlerContext], dict]


def simulate_effect(job_id: uuid.UUID, effect_key: str, detail: dict[str, Any] | None) -> None:
    """The default applier: a simulated external call that reaches nothing.

    This slice ships no third-party integration, so there is no vendor to call and
    nothing to undo. The protocol is the deliverable — claim, commit, apply,
    confirm. A real deployment passes its own applier (the one that sends the HTTP
    request with ``effect_key`` as the vendor's idempotency key) and the outbox
    guarantees the claim was durable first.
    """


def _echo(payload: dict, _context: HandlerContext) -> dict:
    return {"echoed": payload}


def _notify(payload: dict, context: HandlerContext) -> dict:
    """Apply a notification effect at most once per job.

    The key comes from the payload and nowhere else: a key derived from time or
    randomness would make every attempt a new effect, which is the bug this
    handler exists to prevent. An attempt that loses the claim returns
    ``skipped`` — it must not apply, because the winner already did.
    """
    effect_key = str(payload.get("effect_key", DEFAULT_EFFECT_KEY))
    detail = {"effect_key": effect_key}

    with context.session_factory() as session:
        claimed = repository.claim_effect(
            session, job_id=context.job_id, effect_key=effect_key, detail=detail
        )
        session.commit()
    if not claimed:
        return {"skipped": True, "effect_key": effect_key}

    context.apply_effect(context.job_id, effect_key, detail)

    with context.session_factory() as session:
        repository.mark_effect_applied(session, job_id=context.job_id, effect_key=effect_key)
        session.commit()
    return {"applied": True, "effect_key": effect_key}


HANDLERS: dict[str, Handler] = {"echo": _echo, "notify": _notify}


def resolve(job_type: str) -> Handler | None:
    """Return the handler registered for ``job_type``, or ``None`` when unknown."""
    return HANDLERS.get(job_type)


@dataclass(frozen=True)
class FailureDetail:
    """A failure split into a loggable class and a storable message.

    ``error_class`` is safe to log. ``last_error`` is handler-authored text that
    may quote client data, so it belongs in the job row and nowhere else.
    """

    error_class: str
    last_error: str


def describe_failure(error: BaseException) -> FailureDetail:
    """Split a raised exception into its class and ``"<class>: <message>"``, capped."""
    error_class = type(error).__name__
    return FailureDetail(
        error_class=error_class,
        last_error=f"{error_class}: {error}"[:MAX_ERROR_CHARS],
    )


def invalid_result_failure(result: object) -> FailureDetail:
    """The failure for a handler that returned something other than a ``dict``."""
    return FailureDetail(
        error_class="InvalidHandlerResult",
        last_error=INVALID_RESULT_MESSAGE.format(result_type=type(result).__name__)[
            :MAX_ERROR_CHARS
        ],
    )


def completion_failure(error: BaseException) -> FailureDetail:
    """The failure for a SUCCEEDED result that could not be stored."""
    detail = describe_failure(error)
    return FailureDetail(
        error_class=detail.error_class,
        last_error=f"completion failed: {detail.last_error}"[:MAX_ERROR_CHARS],
    )


def unknown_type_failure(job_type: str) -> FailureDetail:
    """The failure for a job whose type has no registered handler."""
    return FailureDetail(
        error_class="UnknownJobType",
        last_error=UNKNOWN_TYPE_MESSAGE.format(job_type=job_type)[:MAX_ERROR_CHARS],
    )
