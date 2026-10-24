"""Run one SafeRepo task end to end.

Example:
    python scripts/run_task.py \
      --repo-url https://github.com/<you>/saferepo \
      --commit <sha> --subdir fixtures/fx1-cart-rounding/project \
      --fix-dir fixtures/fx1-cart-rounding/fixed \
      --issue-file fixtures/fx1-cart-rounding/issue.md

The agent's commands never run on this machine: they go to a Celesto sandbox.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.services.celesto_service import make_name  # noqa: E402
from app.services.pipeline import execute_run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one bug-reproduction task")
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--fixed-commit", default="")
    parser.add_argument("--subdir", default="")
    parser.add_argument("--fix-dir", default="")
    parser.add_argument("--issue-file", default="")
    parser.add_argument("--issue", default="")
    parser.add_argument("--expected-signature", default="")
    parser.add_argument("--test-command", default="python3 -m pytest -q {test}")
    parser.add_argument("--problem-id", default="issue-failing-test")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()

    issue = args.issue
    if args.issue_file:
        issue = Path(args.issue_file).read_text(encoding="utf-8")
    if not issue.strip():
        parser.error("provide --issue-file or --issue")

    settings = get_settings()
    run_id = args.run_id or f"run-{int(time.time())}"
    record = {
        "run_id": run_id,
        "sandbox_name": make_name("saferepo"),
        "problem_id": args.problem_id,
        "detail": {
            "repo_url": args.repo_url,
            "commit": args.commit,
            "fixed_commit": args.fixed_commit,
            "subdir": args.subdir,
            "fix_dir": args.fix_dir,
            "issue_text": issue,
            "expected_signature": args.expected_signature,
            "test_command_template": args.test_command,
        },
    }

    outcome = execute_run(record, settings=settings)
    print(json.dumps(outcome.__dict__, indent=2, default=str))
    print(f"\nevidence: {outcome.evidence_path}")
    return 0 if outcome.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
