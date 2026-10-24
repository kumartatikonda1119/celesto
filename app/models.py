"""Data models for the SafeRepo run ledger.

The actual database layer uses raw SQL (see database.py). This module
documents the schema and provides typed references for use in type hints.
The graded surface is three endpoints with plain dicts, so an ORM mapping
is deliberately avoided to keep the audit surface small.
"""

from __future__ import annotations

# The runs table schema is defined in seeds/01_runs.sql and in database.py (DDL).
# Fields:
#   run_id           TEXT PRIMARY KEY
#   sandbox_name     TEXT NOT NULL
#   problem_id       TEXT NOT NULL
#   state            TEXT NOT NULL  (created | ready | running | succeeded | failed | deleted)
#   execution_target TEXT NOT NULL  (always "celesto_sandbox")
#   detail           JSONB NOT NULL DEFAULT '{}'
#   created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
#   updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
