"""Run the reliability evaluation: every fixture, several independent reps.

Each rep is a *fresh* sandbox with a unique name; nothing is reused. Per-run
rows are appended to ``reports/evidence/runs.jsonl`` as they finish, so an
interrupted eval keeps its completed work (failed runs are never dropped).
``reports/reliability.json`` is rewritten after every run, which means the
summary is always consistent with the rows on disk.

Examples:
    python scripts/run_eval.py --dry-run
    python scripts/run_eval.py --reps 4
    python scripts/run_eval.py --resume          # continue an interrupted eval
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.services.celesto_service import make_name  # noqa: E402
from app.services.evaluator import append_jsonl, load_jsonl, write_reliability  # noqa: E402
from app.services.pipeline import execute_run  # noqa: E402
from app.services.evidence import redact  # noqa: E402


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _slots(fixtures: list[dict], reps: int) -> list[tuple[dict, int]]:
    """Round-robin by rep so an interrupted eval covers every fixture."""
    return [(fixture, rep) for rep in range(1, reps + 1) for fixture in fixtures]


def _fixture_problems(repo_url: str, commit: str) -> list[str]:
    """Validate the effective inputs, after applying CLI overrides."""
    problems = []
    if not isinstance(repo_url, str) or not repo_url.startswith("https://"):
        problems.append("repo_url must be a public HTTPS repository URL")
    elif "REPLACE-" in repo_url.upper():
        problems.append("repo_url still contains a placeholder account name")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        problems.append("commit must be a pinned, full 40-character Git SHA")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the reliability eval")
    parser.add_argument("--fixtures", default=str(ROOT / "fixtures/fixtures.json"))
    parser.add_argument("--reps", type=int, default=4)
    parser.add_argument("--runs", type=int, default=20, help="stop after this many rows")
    parser.add_argument("--repo-url", default="", help="override fixtures.json")
    parser.add_argument("--commit", default="", help="override fixtures.json")
    parser.add_argument("--only", default="", help="fixture id substring filter")
    parser.add_argument("--resume", action="store_true", help="skip completed slots")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "reports/reliability.json"))
    parser.add_argument("--rows", default=str(ROOT / "reports/evidence/runs.jsonl"))
    args = parser.parse_args()
    if args.reps < 1 or args.runs < 1:
        parser.error("--reps and --runs must be positive")

    fixtures_path = Path(args.fixtures)
    config = _read_json(fixtures_path)
    fixtures = config.get("fixtures", [])
    if args.only:
        fixtures = [f for f in fixtures if args.only in f["id"]]
    if not fixtures:
        print("no fixtures selected", file=sys.stderr)
        return 2

    repo_url = args.repo_url or config.get("repo_url", "")
    commit = args.commit or config.get("commit", "")
    test_command = config.get("test_command", "python3 -m pytest -q {test}")

    plan = _slots(fixtures, args.reps)[: args.runs]
    if args.dry_run:
        print(f"{len(plan)} slots (repo {repo_url} @ {commit}):")
        for fixture, rep in plan:
            print(f"  {fixture['id']}  rep {rep}")
        return 0

    problems = _fixture_problems(repo_url, commit)
    if problems:
        print("fixtures are not ready to run:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("Publish the fixture repo, then update fixtures/fixtures.json.", file=sys.stderr)
        return 2

    rows_path = Path(args.rows)
    out_path = Path(args.out)
    if not args.resume and (rows_path.exists() or out_path.exists()):
        parser.error("existing evidence found; use --resume or new --rows/--out paths")
    rows = load_jsonl(rows_path) if args.resume else []
    for row in rows:
        if row.get("repo_url") != repo_url or row.get("commit") != commit:
            parser.error("cannot resume evidence from a different or unknown repo/commit")
    done = {str(row.get("slot") or row.get("run_id")) for row in rows}

    settings = get_settings()
    if not settings.has_celesto or not settings.has_llm:
        print("CELESTO_API_KEY and LLM_API_KEY/LLM_MODEL must be configured", file=sys.stderr)
        return 2
    started_all = time.monotonic()
    exit_code = 1 if any(row.get("success") is not True for row in rows) else 0

    try:
        for index, (fixture, rep) in enumerate(plan, 1):
            slot = f"{fixture['id']}-r{rep}"
            if slot in done:
                print(f"[{index}/{len(plan)}] {slot}: already recorded, skipping")
                continue
            if len(rows) >= args.runs:
                break

            issue_path = ROOT / fixture["issue"]
            record = {
                "run_id": f"run-{slot}-{uuid.uuid4().hex[:12]}",
                "sandbox_name": make_name("saferepo"),
                "problem_id": fixture["id"],
                "detail": {
                    "repo_url": repo_url,
                    "commit": commit,
                    "fixed_commit": fixture.get("fixed_commit", ""),
                    "subdir": fixture["subdir"],
                    "fix_dir": fixture["fix_dir"],
                    "issue_text": issue_path.read_text(encoding="utf-8"),
                    "expected_signature": fixture.get("expected_signature", ""),
                    "test_command_template": fixture.get("test_command", test_command),
                },
            }

            print(f"[{index}/{len(plan)}] {slot}: creating {record['sandbox_name']} ...", flush=True)
            outcome = execute_run(record, settings=settings)
            row = outcome.as_run_row()
            row.update(
                {
                    "slot": slot,
                    "fixture_id": fixture["id"],
                    "repo_url": repo_url,
                    "commit": commit,
                    "rep": rep,
                    "status_detail": outcome.status,
                    "reason": redact(outcome.reason[:500], settings),
                    "evidence_path": outcome.evidence_path,
                }
            )
            append_jsonl(rows_path, row)
            rows.append(row)

            summary = write_reliability(rows, out_path)
            print(
                f"    success={outcome.success} status={outcome.status} "
                f"wall={row['wall_clock_s']}s boot={row['boot_to_ready_s']}s "
                f"cost=${row['cost_usd']:.6f}",
                flush=True,
            )
            print(
                f"    progress: {summary['run_count']}/{args.runs} runs, "
                f"success_rate={summary['success_rate']:.2f}, "
                f"elapsed={time.monotonic() - started_all:.0f}s",
                flush=True,
            )
            if outcome.status == "interrupted":
                exit_code = 130
                break
            if not outcome.success:
                exit_code = 1
    except KeyboardInterrupt:
        print("\ninterrupted; completed runs are already on disk", file=sys.stderr)
        exit_code = 130

    if rows:
        summary = write_reliability(rows, out_path)
        print("\nsummary")
        print(json.dumps({k: v for k, v in summary.items() if k != "runs"}, indent=2))
    else:
        print("no runs recorded", file=sys.stderr)
        exit_code = exit_code or 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
