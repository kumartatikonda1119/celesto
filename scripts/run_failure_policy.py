"""Run a genuine cloud task with one controlled step-7 fault and fresh replay.

The test is still authored by the LLM. No fake successful trace or reliability
row is produced. This experiment is separate from the 20-run evaluation.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.services.celesto_service import make_name
from app.services.pipeline import execute_run
from app.services.probes import write_json
from scripts.run_eval import _fixture_problems


def main():
    config = json.loads((ROOT / "fixtures/fixtures.json").read_text(encoding="utf-8"))
    problems = _fixture_problems(config["repo_url"], config["commit"])
    settings = replace(get_settings(), fault="step7")
    if problems or not settings.has_celesto or not settings.has_llm:
        print("Pinned fixtures and CELESTO_API_KEY/LLM_API_KEY/LLM_MODEL are required", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 2
    fixture = config["fixtures"][0]
    record = {
        "run_id": f"failure-probe-{uuid.uuid4().hex[:12]}", "sandbox_name": make_name(),
        "problem_id": fixture["id"], "detail": {
            "repo_url": config["repo_url"], "commit": config["commit"],
            "subdir": fixture["subdir"], "fix_dir": fixture["fix_dir"],
            "issue_text": (ROOT / fixture["issue"]).read_text(encoding="utf-8"),
            "expected_signature": fixture["expected_signature"],
            "test_command_template": config["test_command"],
        },
    }
    outcome = execute_run(record, settings)
    applied = outcome.policy_applied == "fresh"
    write_json(settings.report_dir / "failure_policy.json", {
        "failed_step": 7, "total_steps": 10, "policy": "fresh",
        "trace_path": "reports/failure_trace.jsonl", "run_id": outcome.run_id,
        "policy_observed": applied, "recovery_succeeded": outcome.success,
        "fault_injection": "SAFEREPO_FAULT=step7; one controlled pre-execution fault",
        "rationale": "A lost execution result at the untrusted-test boundary cannot be treated as successful. On this Windows/cloud path, local VM snapshots are unavailable; fresh recovery deletes the old guest before allocating a new uniquely identified guest, checks out the pinned repository again, and replays the saved untrusted test without another LLM call. This adds boot cost but avoids trusting partially mutated guest state. A failed old-guest deletion aborts recovery rather than leaking capacity or claiming cleanup.",
    }, settings)
    print(f"policy_applied={applied}, recovery_succeeded={outcome.success}, evidence={outcome.evidence_path}")
    return 0 if applied and outcome.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
