"""Job handler registry.

A handler takes the job payload and returns the result dict stored on the job.
Only ``echo`` ships in PR-2-02; it exists so the claim and lease tests have real
work to interrupt.
"""

from collections.abc import Callable
from dataclasses import dataclass

Handler = Callable[[dict], dict]

MAX_ERROR_CHARS = 2000
UNKNOWN_TYPE_MESSAGE = "no handler registered for type '{job_type}'"
INVALID_RESULT_MESSAGE = "invalid handler result: expected dict, got {result_type}"


def _echo(payload: dict) -> dict:
    return {"echoed": payload}


HANDLERS: dict[str, Handler] = {"echo": _echo}


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
