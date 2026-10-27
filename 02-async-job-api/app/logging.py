"""Structured JSON-lines logging (one object per line on stdout).

CloudWatch and ``jq`` can filter on ``event``; job events carry ``job_id`` and
``attempt``. Payloads and results are never logged — they can hold client data.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

LOGGER_NAME = "async_job_api"

#: Keys whose values must never reach a log line.
REDACTED_KEYS = frozenset({"payload", "result", "traceback"})


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
