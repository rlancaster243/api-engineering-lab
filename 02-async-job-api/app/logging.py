"""Structured JSON-lines logging (one object per line on stdout).

CloudWatch and ``jq`` can filter on ``event``; job events carry ``job_id``,
``attempt`` and, for failures, ``error_class`` and ``last_error_chars``.

What the worker never logs:

* the job payload or the handler's result;
* any handler-derived message text — an exception's message or a stored
  ``last_error``. Handler messages are written by job code and can quote client
  data (an API key a handler rejected, for example). The message is kept in
  ``jobs.last_error`` for the operator and the class alone is logged here.

The worker also logs no traceback at all, for a handler failure or for a failure
in its own cycle: a formatted traceback repeats the exception message and the
source lines around it, and a claim failure can carry a row value out of the
driver. Every failure on this path is recorded as its class name.

This module only formats what it is given; a caller that passes ``exc_info`` or
puts a value in the message defeats these rules. Two callers deliberately avoid
both: the worker logs ``error_class`` only, and ``app.errors`` logs
``stack_only()`` — stack frames and SQLSTATE, with the exception message omitted,
because psycopg's ``DETAIL: Failing row contains (...)`` quotes the submitted row.
"""

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

LOGGER_NAME = "async_job_api"

#: Keys whose values must never reach a log line, as a backstop if a caller ever
#: passes a whole job dict. Callers should not pass these at all.
REDACTED_KEYS = frozenset({"payload", "result"})


class JsonLinesFormatter(logging.Formatter):
    """Render each record as one JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
        }
        event = getattr(record, "event", None)
        if isinstance(event, str):
            obj["event"] = event
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                if key not in REDACTED_KEYS:
                    obj[key] = value
        obj["message"] = record.getMessage()
        if record.exc_info:
            obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str, ensure_ascii=False)


def stack_only(exc: BaseException) -> str:
    """Format an exception's stack frames without its message.

    Frames name the call sites, which is what makes a traceback useful; the message
    and the exception's own line are omitted because they can quote a payload value
    or, for a driver error, the failing row.
    """
    return "".join(traceback.format_tb(exc.__traceback__))


def configure_logging(stream: Any = None) -> logging.Logger:
    """Install the JSON formatter on the worker logger and return it."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonLinesFormatter())
    logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit one structured event. ``fields`` are merged into the JSON object."""
    level = fields.pop("_level", logging.INFO)
    logger.log(level, event, extra={"event": event, "fields": fields})
