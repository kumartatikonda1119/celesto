"""Request/response shapes and the ledger's validation rules.

Validation is intentionally manual rather than delegated to FastAPI's 422
handler: the contract for POST /api/runs is HTTP 400 for a bad execution
target, an unknown state or a malformed sandbox name.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict

SANDBOX_PATTERN = re.compile(r"^sbx-[a-z0-9]{8,}$")
STATES = {"created", "ready", "running", "succeeded", "failed", "deleted"}
EXECUTION_TARGET = "celesto_sandbox"


class RunIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: str
    sandbox_name: str
    problem_id: str
    state: str
    execution_target: str
    detail: dict[str, Any] | None = None


class RunOut(BaseModel):
    run_id: str
    sandbox_name: str
    problem_id: str
    state: str
    execution_target: str
    created_at: str | None = None
    updated_at: str | None = None


def validate_run(payload: RunIn) -> str | None:
    """Return an error message, or None when the payload is acceptable."""
    if not payload.run_id.strip():
        return "run_id must not be empty"
    if not payload.problem_id.strip():
        return "problem_id must not be empty"
    if not SANDBOX_PATTERN.match(payload.sandbox_name):
        return (
            "sandbox_name must match ^sbx-[a-z0-9]{8,}$ "
            "(lowercase letters and digits only)"
        )
    if payload.state not in STATES:
        return f"state must be one of {sorted(STATES)}"
    if payload.execution_target != EXECUTION_TARGET:
        return f"execution_target must be exactly {EXECUTION_TARGET!r}"
    return None
