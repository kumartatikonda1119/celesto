"""Validate the submission against the challenge's machine-read contract.

Run:  python scripts/validate_reports.py
Exit code 0 means every checked rule passes.

This script deliberately re-implements the arithmetic instead of trusting the
numbers written by the harness: summary values must be reproducible from the
run rows.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SANDBOX_RE = re.compile(r"^sbx-[a-z0-9]{8,}$")
STATES = {"created", "ready", "running", "succeeded", "failed", "deleted"}
ALLOWED_CAPS = {
    "persistence",
    "snapshots",
    "celestofs",
    "browser",
    "published_ports",
    "network_policy",
    "safety_hooks",
    "host_mounts",
    "coding_agent_preset",
    "agentor_durable",
}
README_PHRASES = [
    "Problem statement",
    "How I used Celesto",
    "Eval report",
    "Containment test",
    "State and failure design",
    "Setup",
]
REQUIRED_FILES = [
    "docker-compose.yml",
    "Dockerfile",
    ".env.example",
    "README.md",
    "seeds/01_runs.sql",
    "reports/problem.json",
    "reports/capabilities.json",
    "reports/lifecycle.json",
    "reports/reliability.json",
    "reports/postmortem.md",
    "reports/containment.json",
    "reports/failure_policy.json",
    "reports/failure_trace.jsonl",
    "reports/concurrency.json",
    "logs/commands.jsonl",
    "tests/test_reports.py",
]


def _load_json(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            errors.append(f"{path.relative_to(ROOT)} must contain a JSON object")
            return None
        return data
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read {path.relative_to(ROOT)} as JSON: {exc}")
    return None


def _jsonl(path: Path, errors: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"cannot read {path.relative_to(ROOT)}: {exc}")
        return rows
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
            else:
                errors.append(f"{path.name}:{number} must contain a JSON object")
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name}:{number} is not valid JSON: {exc}")
    return rows


def median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def p90(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(1, min(math.ceil(0.90 * len(ordered)), len(ordered)))
    return ordered[rank - 1]


def _nonnegative_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _evidence_exists(value: Any) -> bool:
    """Evidence must be a file inside the submission, not a host path."""
    if not isinstance(value, str) or not value:
        return False
    path = (ROOT / value).resolve()
    return path.is_relative_to(ROOT.resolve()) and path.is_file()


def check_files(errors: list[str]) -> None:
    for rel in REQUIRED_FILES:
        if not (ROOT / rel).exists():
            errors.append(f"missing required file: {rel}")
    # A local gitignored .env is expected during development. Only a tracked
    # secret file violates the submission rule; never read its contents.
    if (ROOT / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "--", ".env"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            errors.append("cannot verify whether .env is tracked by Git")
        elif result.stdout.strip():
            errors.append(".env must not be tracked by Git")


def check_problem(errors: list[str]) -> None:
    data = _load_json(ROOT / "reports/problem.json", errors)
    if data is None:
        return
    for key, minimum in (("who", 40), ("agent_work", 40), ("proof_output", 20)):
        value = data.get(key)
        if not isinstance(value, str) or len(value) < minimum:
            errors.append(f"problem.json: {key} must be a string of >= {minimum} characters")
    if data.get("idea_source") not in ("idea_bank", "original"):
        errors.append("problem.json: idea_source must be 'idea_bank' or 'original'")


def check_capabilities(errors: list[str]) -> None:
    data = _load_json(ROOT / "reports/capabilities.json", errors)
    if data is None:
        return
    caps = data.get("capabilities")
    if not isinstance(caps, list) or len(caps) < 2:
        errors.append("capabilities.json: needs at least two capability objects")
        return
    seen = set()
    for cap in caps:
        if not isinstance(cap, dict):
            errors.append("capabilities.json: every entry must be an object")
            continue
        cap_id = cap.get("id")
        if not isinstance(cap_id, str) or cap_id not in ALLOWED_CAPS:
            errors.append(f"capabilities.json: id {cap_id!r} is not in the allowed set")
            continue
        if cap_id in seen:
            errors.append(f"capabilities.json: duplicate id {cap_id!r}")
        seen.add(cap_id)
        if len(str(cap.get("why", ""))) < 80:
            errors.append(f"capabilities.json: {cap_id} needs a 'why' of >= 80 characters")
        evidence = cap.get("evidence_path")
        if not _evidence_exists(evidence):
            errors.append(f"capabilities.json: {cap_id} evidence_path does not exist: {evidence!r}")


def _command_names() -> set[str]:
    rows = _jsonl(ROOT / "logs/commands.jsonl", [])
    return {
        row["sandbox_name"] for row in rows
        if isinstance(row.get("sandbox_name"), str)
    }


def check_lifecycle(errors: list[str]) -> None:
    data = _load_json(ROOT / "reports/lifecycle.json", errors)
    if data is None:
        return
    for flag in ("cleanup_on_success", "cleanup_on_failure", "cleanup_on_interrupt"):
        if data.get(flag) is not True:
            errors.append(f"lifecycle.json: {flag} must be true")
    names = data.get("sandbox_names") or []
    if not isinstance(names, list) or not names:
        errors.append("lifecycle.json: sandbox_names must be a non-empty array")
        return
    if not all(isinstance(name, str) for name in names):
        errors.append("lifecycle.json: sandbox names must be strings")
        return
    if len(set(names)) != len(names):
        errors.append("lifecycle.json: sandbox_names contains duplicates")
    known = _command_names()
    reliability = ROOT / "reports/reliability.json"
    if reliability.exists():
        report = _load_json(reliability, errors)
        runs = report.get("runs", []) if report is not None else []
        if isinstance(runs, list):
            for run in runs:
                if isinstance(run, dict) and isinstance(run.get("sandbox_name"), str):
                    known.add(run["sandbox_name"])
    for name in names:
        if not isinstance(name, str) or not SANDBOX_RE.match(name):
            errors.append(f"lifecycle.json: invalid sandbox name {name!r}")
        elif name not in known:
            errors.append(f"lifecycle.json: {name} is not in commands.jsonl or reliability.json")


def check_commands(errors: list[str]) -> None:
    rows = _jsonl(ROOT / "logs/commands.jsonl", errors)
    if not rows:
        errors.append("commands.jsonl: needs at least one JSON line")
        return
    for index, row in enumerate(rows, 1):
        for key in ("ts", "sandbox_name", "command", "decision", "execution_target"):
            if key not in row:
                errors.append(f"commands.jsonl line {index}: missing {key}")
        if not SANDBOX_RE.match(str(row.get("sandbox_name", ""))):
            errors.append(f"commands.jsonl line {index}: invalid sandbox_name")
        if not str(row.get("command", "")).strip():
            errors.append(f"commands.jsonl line {index}: empty command")
        if row.get("decision") not in {"allow", "block"}:
            errors.append(f"commands.jsonl line {index}: decision must be allow or block")
        if row.get("execution_target") != "celesto_sandbox":
            errors.append(
                f"commands.jsonl line {index}: execution_target must be 'celesto_sandbox'"
            )


def check_reliability(errors: list[str]) -> None:
    data = _load_json(ROOT / "reports/reliability.json", errors)
    if data is None:
        return
    runs = data.get("runs")
    if not isinstance(runs, list):
        errors.append("reliability.json: runs must be an array")
        return
    if len(runs) < 20:
        errors.append(f"reliability.json: needs >= 20 runs (found {len(runs)})")
    if type(data.get("run_count")) is not int or data["run_count"] != len(runs):
        errors.append("reliability.json: run_count must equal len(runs)")

    valid = True
    names = []
    for index, run in enumerate(runs, 1):
        if not isinstance(run, dict):
            errors.append(f"reliability.json run {index}: must be an object")
            valid = False
            continue
        if run.get("fresh") is not True:
            errors.append(f"reliability.json run {index}: fresh must be true")
        if not isinstance(run.get("success"), bool):
            errors.append(f"reliability.json run {index}: success must be boolean")
        for key in ("wall_clock_s", "boot_to_ready_s", "cost_usd"):
            if not _nonnegative_number(run.get(key)):
                errors.append(f"reliability.json run {index}: {key} must be a finite number >= 0")
                valid = False
        name = run.get("sandbox_name")
        if not isinstance(name, str) or not SANDBOX_RE.fullmatch(name):
            errors.append(f"reliability.json run {index}: invalid sandbox_name")
        else:
            names.append(name)
    if len(set(names)) != len(names):
        errors.append("reliability.json: sandbox_name values must be unique")

    if valid and runs:
        wall = [float(r["wall_clock_s"]) for r in runs]
        boot = [float(r["boot_to_ready_s"]) for r in runs]
        if wall != sorted(wall):
            errors.append("reliability.json: runs must be sorted by wall_clock_s ascending")
        checks = {
            "success_rate": (sum(r.get("success") is True for r in runs) / len(runs), 0.001),
            "median_wall_clock_s": (median(wall), 0.001),
            "p90_wall_clock_s": (p90(wall), 0.001),
            "median_boot_to_ready_s": (median(boot), 0.001),
            "total_cost_usd": (sum(float(r["cost_usd"]) for r in runs), 0.01),
        }
        for key, (expected, tolerance) in checks.items():
            actual = data.get(key)
            if not _nonnegative_number(actual):
                errors.append(f"reliability.json: {key} is missing or not finite numeric")
            elif abs(float(actual) - expected) > tolerance:
                errors.append(
                    f"reliability.json: {key}={actual} does not match recomputed {expected:.6f}"
                )

    if data.get("postmortem_path") != "reports/postmortem.md":
        errors.append("reliability.json: postmortem_path must be reports/postmortem.md")
    postmortem = ROOT / "reports/postmortem.md"
    if not postmortem.is_file() or len(postmortem.read_text(encoding="utf-8")) < 200:
        errors.append("reports/postmortem.md must exist and contain >= 200 characters")


def check_containment(errors: list[str]) -> None:
    data = _load_json(ROOT / "reports/containment.json", errors)
    if data is None:
        return
    if data.get("attack") not in (
        "prompt_injection_exfil",
        "destructive_command",
        "suspicious_package",
    ):
        errors.append("containment.json: attack must be one of the three ids")
    if data.get("blocked") is not True:
        errors.append("containment.json: blocked must be true")
    if data.get("host_untouched") is not True:
        errors.append("containment.json: host_untouched must be true")
    paths = data.get("evidence_paths") or []
    if not isinstance(paths, list) or not paths:
        errors.append("containment.json: evidence_paths must be a non-empty array")
        return
    for path in paths:
        if not _evidence_exists(path):
            errors.append(f"containment.json: evidence path does not exist: {path}")


def check_failure_policy(errors: list[str]) -> None:
    data = _load_json(ROOT / "reports/failure_policy.json", errors)
    if data is None:
        return
    if data.get("failed_step") != 7:
        errors.append("failure_policy.json: failed_step must be 7")
    if data.get("total_steps") != 10:
        errors.append("failure_policy.json: total_steps must be 10")
    policy = data.get("policy")
    if policy not in ("resume", "rollback", "fresh"):
        errors.append("failure_policy.json: policy must be resume, rollback or fresh")
    if len(str(data.get("rationale", ""))) < 200:
        errors.append("failure_policy.json: rationale must be >= 200 characters")
    trace_path = data.get("trace_path")
    if trace_path != "reports/failure_trace.jsonl":
        errors.append("failure_policy.json: trace_path must be reports/failure_trace.jsonl")
    trace = _jsonl(ROOT / "reports/failure_trace.jsonl", errors)
    matches = [
        row
        for row in trace
        if row.get("event") == "policy_applied" and row.get("policy") == policy
    ]
    if not matches:
        errors.append(
            "failure_trace.jsonl: no line with event=policy_applied and the configured policy"
        )


def check_concurrency(errors: list[str]) -> None:
    data = _load_json(ROOT / "reports/concurrency.json", errors)
    if data is None:
        return
    jobs = data.get("jobs") or []
    if not isinstance(jobs, list) or len(jobs) < 5:
        errors.append("concurrency.json: needs at least five jobs")
        return
    names = []
    for index, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            errors.append(f"concurrency.json job {index}: must be an object")
            continue
        name = job.get("sandbox_name")
        if not isinstance(name, str) or not SANDBOX_RE.fullmatch(name):
            errors.append(f"concurrency.json job {index}: invalid sandbox_name")
        else:
            names.append(name)
        started_at = job.get("started_at")
        if not isinstance(started_at, str) or not started_at.strip():
            errors.append(f"concurrency.json job {index}: started_at must not be empty")
    if len(set(names)) != len(names):
        errors.append("concurrency.json: sandbox names must be unique")
    if len(str(data.get("breakage_notes", ""))) < 40:
        errors.append("concurrency.json: breakage_notes must be >= 40 characters")


def check_readme(errors: list[str]) -> None:
    path = ROOT / "README.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    for phrase in README_PHRASES:
        if phrase not in text:
            errors.append(f"README.md: missing required phrase {phrase!r}")


def check_compose(errors: list[str]) -> None:
    path = ROOT / "docker-compose.yml"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    for service in ("api:", "ledger:"):
        if service not in text:
            errors.append(f"docker-compose.yml: missing service {service[:-1]}")
    if "./seeds" not in text:
        errors.append("docker-compose.yml: ledger must mount ./seeds")
    if "pg_isready" not in text:
        errors.append("docker-compose.yml: ledger healthcheck must use pg_isready")
    if "http://localhost:8080/health" not in text:
        errors.append("docker-compose.yml: api healthcheck must request /health")
    for key in ("interval:", "timeout:", "retries:", "start_period:"):
        if text.count(key) < 2:
            errors.append(f"docker-compose.yml: both healthchecks need {key[:-1]}")


def check_env_example(errors: list[str]) -> None:
    path = ROOT / ".env.example"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    for key in (
        "CELESTO_PROVIDER",
        "CELESTO_API_KEY",
        "LLM_API_KEY",
        "LLM_MODEL",
        "DATABASE_URL",
        "API_PORT",
    ):
        if not re.search(rf"^{key}=", text, re.MULTILINE):
            errors.append(f".env.example: missing {key}")
    if "replace-me" not in text:
        errors.append(".env.example: expected placeholder values")


def check_seed(errors: list[str]) -> None:
    path = ROOT / "seeds/01_runs.sql"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if "sbx-seeddemo1" not in text:
        errors.append("seeds/01_runs.sql: must seed sbx-seeddemo1")
    if "ON CONFLICT" not in text:
        errors.append("seeds/01_runs.sql: seed must be idempotent (ON CONFLICT)")


def check_demo(errors: list[str]) -> None:
    if not (ROOT / "demo/walkthrough.mp4").exists():
        errors.append("demo/walkthrough.mp4 is missing")


def validate_all() -> list[str]:
    errors: list[str] = []
    check_files(errors)
    check_problem(errors)
    check_capabilities(errors)
    check_lifecycle(errors)
    check_commands(errors)
    check_reliability(errors)
    check_containment(errors)
    check_failure_policy(errors)
    check_concurrency(errors)
    check_readme(errors)
    check_compose(errors)
    check_env_example(errors)
    check_seed(errors)
    check_demo(errors)
    return errors


def main() -> int:
    errors = validate_all()
    if errors:
        print(f"FAILED with {len(errors)} problem(s):")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("All submission checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
