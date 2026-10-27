"""Job handler registry.

A handler takes the job payload and returns the result dict stored on the job.
Only ``echo`` ships in PR-2-02; it exists so the claim and lease tests have real
work to interrupt.
"""

from collections.abc import Callable

Handler = Callable[[dict], dict]

MAX_ERROR_CHARS = 2000
UNKNOWN_TYPE_MESSAGE = "no handler registered for type '{job_type}'"


def _echo(payload: dict) -> dict:
    return {"echoed": payload}


HANDLERS: dict[str, Handler] = {"echo": _echo}


def resolve(job_type: str) -> Handler | None:
    """Return the handler registered for ``job_type``, or ``None`` when unknown."""
    return HANDLERS.get(job_type)


def describe_failure(error: BaseException) -> str:
    """Render a handler failure as ``"<ExceptionClass>: <message>"``, capped for the row.

    Tracebacks go to logs, never into ``last_error``.
    """
    return f"{type(error).__name__}: {error}"[:MAX_ERROR_CHARS]


def unknown_type_error(job_type: str) -> str:
    """The ``last_error`` for a job whose type has no registered handler."""
    return UNKNOWN_TYPE_MESSAGE.format(job_type=job_type)[:MAX_ERROR_CHARS]
