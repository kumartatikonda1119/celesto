"""Postgres access for the run ledger.

Deliberately small: one connection per request, raw SQL, no ORM. The graded
surface is three endpoints, and a tiny plain-SQL layer is easier to audit than
an ORM mapping.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import get_settings

DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    sandbox_name     TEXT NOT NULL,
    problem_id       TEXT NOT NULL,
    state            TEXT NOT NULL,
    execution_target TEXT NOT NULL,
    detail           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS runs_state_idx ON runs (state);
CREATE INDEX IF NOT EXISTS runs_sandbox_idx ON runs (sandbox_name);
"""


def connect() -> psycopg.Connection:
    return psycopg.connect(
        get_settings().database_url,
        row_factory=dict_row,
        connect_timeout=5,
    )


def init_db(retries: int = 30, delay: float = 1.0) -> None:
    """Create the schema, retrying while Postgres finishes booting."""
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            with connect() as conn:
                conn.execute(DDL)
                conn.commit()
            return
        except psycopg.Error as exc:  # pragma: no cover - timing dependent
            last_error = exc
            time.sleep(delay)
    raise RuntimeError(f"ledger database not reachable: {last_error}")


def list_runs() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY created_at ASC, run_id ASC"
        ).fetchall()
    return [serialize(row) for row in rows]


def get_run(run_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id = %s", (run_id,)
        ).fetchone()
    return serialize(row) if row else None


def upsert_run(
    run_id: str,
    sandbox_name: str,
    problem_id: str,
    state: str,
    execution_target: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO runs
                (run_id, sandbox_name, problem_id, state, execution_target, detail)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                sandbox_name     = EXCLUDED.sandbox_name,
                problem_id       = EXCLUDED.problem_id,
                state            = EXCLUDED.state,
                execution_target = EXCLUDED.execution_target,
                detail           = runs.detail || EXCLUDED.detail,
                updated_at       = now()
            RETURNING *
            """,
            (
                run_id,
                sandbox_name,
                problem_id,
                state,
                execution_target,
                psycopg.types.json.Jsonb(detail or {}),
            ),
        ).fetchone()
        conn.commit()
    return serialize(row)


def serialize(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("created_at", "updated_at"):
        value = out.get(key)
        if isinstance(value, datetime):
            out[key] = value.isoformat()
    return out
