"""Run lifecycle: the 10 named steps, ledger transitions and cost accounting.

The pipeline in this package is fixed at ten steps so that "step 7 of 10" has
a precise meaning: step 7 is the moment the untrusted, agent-authored test is
executed inside the guest. That is where the designed failure occurs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings, get_settings
from .evidence import FailureTrace, utc_now

STEPS: list[tuple[int, str]] = [
    (1, "validate_input"),
    (2, "create_sandbox"),
    (3, "wait_ready"),
    (4, "clone_repository"),
    (5, "inspect_repository"),
    (6, "write_regression_test"),
    (7, "execute_untrusted_test"),
    (8, "classify_result"),
    (9, "persist_evidence"),
    (10, "report_and_cleanup"),
]
STEP_NAMES = dict(STEPS)
RISKY_STEP = 7
TOTAL_STEPS = len(STEPS)

# Published Celesto rates, USD per running hour. Used only to *estimate* cost;
# the README says plainly that these are not invoices.
HOURLY_RATE_USD = {"micro": 0.015, "standard": 0.030, "large": 0.080}

# Ledger states mandated by the challenge contract.
LEDGER_STATES = {"created", "ready", "running", "succeeded", "failed", "deleted"}


@dataclass
class TaskSpec:
    problem_id: str
    repo_url: str
    commit: str
    fixed_commit: str
    issue_text: str
    test_command_template: str = "python3 -m pytest -q {test}"
    expected_signature: str = ""
    # Some fixtures live in a subdirectory of the repository (SafeRepo's own
    # fixtures do). The gold check overlays the sibling fix_dir onto the
    # worktree, turning the buggy checkout into the fixed one.
    subdir: str = ""
    fix_dir: str = ""

    @classmethod
    def from_detail(cls, problem_id: str, detail: dict[str, Any]) -> "TaskSpec":
        return cls(
            problem_id=problem_id,
            repo_url=detail["repo_url"],
            commit=detail.get("commit", "HEAD"),
            fixed_commit=detail.get("fixed_commit", ""),
            issue_text=detail.get("issue_text", ""),
            test_command_template=detail.get(
                "test_command_template", "python3 -m pytest -q {test}"
            ),
            expected_signature=detail.get("expected_signature", ""),
            subdir=detail.get("subdir") or "",
            fix_dir=detail.get("fix_dir") or "",
        )


@dataclass
class RunOutcome:
    run_id: str
    sandbox_name: str
    success: bool
    status: str
    reason: str = ""
    wall_clock_s: float = 0.0
    boot_to_ready_s: float = 0.0
    cost_usd: float = 0.0
    steps_completed: int = 0
    failed_step: int | None = None
    policy_applied: str = ""
    test_path: str = ""
    evidence_path: str = ""
    llm: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    def as_run_row(self) -> dict[str, Any]:
        return {
            "sandbox_name": self.sandbox_name,
            "fresh": True,
            "success": self.success,
            "wall_clock_s": round(self.wall_clock_s, 3),
            "boot_to_ready_s": round(self.boot_to_ready_s, 3),
            "cost_usd": round(self.cost_usd, 6),
            "status": self.status,
            "failed_step": self.failed_step,
            "policy": self.policy_applied,
            "run_id": self.run_id,
        }


class LedgerClient:
    """Thin HTTP client for the trusted ledger.

    Used by host-side scripts and by the api's own background task. Updating
    the ledger is best-effort: a ledger hiccup must not destroy a run's
    evidence, but it is reported in the outcome.
    """

    def __init__(self, base_url: str | None = None, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self.base_url = (base_url or settings.api_base_url).rstrip("/")
        self.settings = settings
        self.errors: list[str] = []

    def update(
        self,
        *,
        run_id: str,
        sandbox_name: str,
        problem_id: str,
        state: str,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        assert state in LEDGER_STATES, state
        payload = {
            "run_id": run_id,
            "sandbox_name": sandbox_name,
            "problem_id": problem_id,
            "state": state,
            "execution_target": "celesto_sandbox",
            "detail": detail or {},
        }
        try:
            response = httpx.post(
                f"{self.base_url}/api/runs", json=payload, timeout=10.0
            )
            if response.status_code != 201:
                self.errors.append(
                    f"{state}: HTTP {response.status_code} {response.text[:200]}"
                )
                return False
            return True
        except httpx.HTTPError as exc:
            self.errors.append(f"{state}: {exc}")
            return False

    def fetch(self, run_id: str) -> dict[str, Any] | None:
        try:
            response = httpx.get(f"{self.base_url}/api/runs/{run_id}", timeout=10.0)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            return None
        return None


class RunTrace:
    """Step-level events for one run, written to reports/failure_trace.jsonl."""

    def __init__(
        self,
        run_id: str,
        settings: Settings | None = None,
        trace: FailureTrace | None = None,
    ) -> None:
        self.run_id = run_id
        self.current_step = 1
        self.completed_steps: set[int] = set()
        self.trace = trace or FailureTrace(settings=settings)

    def started(self, step: int, sandbox_name: str = "", **fields: Any) -> None:
        self.current_step = step
        self.trace.event(
            "step_started",
            run_id=self.run_id,
            step=step,
            step_name=STEP_NAMES.get(step, "?"),
            total_steps=TOTAL_STEPS,
            sandbox_name=sandbox_name,
            **fields,
        )

    def finished(self, step: int, sandbox_name: str = "", **fields: Any) -> None:
        self.completed_steps.add(step)
        self.trace.event(
            "step_finished",
            run_id=self.run_id,
            step=step,
            step_name=STEP_NAMES.get(step, "?"),
            total_steps=TOTAL_STEPS,
            sandbox_name=sandbox_name,
            **fields,
        )

    def failed(
        self, step: int, error: str, sandbox_name: str = "", **fields: Any
    ) -> None:
        self.trace.event(
            "step_failed",
            run_id=self.run_id,
            step=step,
            step_name=STEP_NAMES.get(step, "?"),
            total_steps=TOTAL_STEPS,
            sandbox_name=sandbox_name,
            error=error,
            **fields,
        )

    def policy_applied(
        self,
        policy: str,
        old_sandbox: str,
        new_sandbox: str,
        reason: str,
        **fields: Any,
    ) -> None:
        self.trace.event(
            "policy_applied",
            run_id=self.run_id,
            policy=policy,
            failed_step=RISKY_STEP,
            total_steps=TOTAL_STEPS,
            old_sandbox_name=old_sandbox,
            new_sandbox_name=new_sandbox,
            reason=reason,
            applied_at=utc_now(),
            **fields,
        )

    def cleanup(self, sandbox_name: str, state: str, **fields: Any) -> None:
        self.trace.event(
            "cleanup",
            run_id=self.run_id,
            sandbox_name=sandbox_name,
            final_state=state,
            **fields,
        )
