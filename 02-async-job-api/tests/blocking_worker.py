"""Test-only worker entry point used by ``tests/test_recovery.py``.

Registers a handler that writes a readiness marker file and then blocks forever, so
the crash test can spawn a **real** worker process, wait until it is genuinely
mid-job, and ``SIGKILL`` it.

Run as ``python -m tests.blocking_worker``. App code stays free of test-only
handlers.
"""

import sys
import time
from pathlib import Path

from app import handlers
from app.handlers import HandlerContext
from app.worker import main


def _block(payload: dict, _context: HandlerContext) -> dict:
    ready_file = payload["ready_file"]
    Path(ready_file).write_text("started", encoding="utf-8")
    while True:  # killed by the test; never returns
        time.sleep(0.05)


handlers.HANDLERS["block"] = _block


if __name__ == "__main__":
    sys.exit(main())
