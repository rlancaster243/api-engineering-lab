"""Readiness probe: PostgreSQL must answer a round trip (PR-08, Gate E).

Unlike ``GET /health``, this endpoint performs a real ``SELECT 1`` so orchestrators can
distinguish "process up" from "can serve persistence-backed routes".
"""

from sqlalchemy import text
from sqlalchemy.orm import Session


def database_is_ready(session: Session) -> None:
    session.execute(text("SELECT 1"))
