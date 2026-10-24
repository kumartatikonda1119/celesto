"""Attempt five simultaneous sandboxes and record what actually happens.

The trial plan is expected to reject most of these creates. That is a result,
not a failure of the harness: the script records each attempt's real outcome
and writes breakage notes describing exactly what was observed. No retries, no
hidden workarounds, no purchased capacity.

    python scripts/run_concurrency.py
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.services.celesto_service import Sandbox, make_name  # noqa: E402
from app.services.evidence import ensure_dirs, utc_now, redact  # noqa: E402
from app.services.probes import record_probe, write_json  # noqa: E402


def _job(index: int, settings, launch: Barrier, hold: Barrier) -> dict:
    launch.wait(timeout=30)
    started_at = utc_now()
    started = time.monotonic()
    name = make_name("saferepo")
    sandbox = Sandbox(name, settings)
    row = {
        "job": index,
        "sandbox_name": name,
        "started_at": started_at,
        "finished_at": "",
        "ok": False,
        "boot_s": None,
        "exit_code": None,
        "stdout": "",
        "error": "",
    }
    try:
        sandbox.create()
        row["boot_s"] = round(sandbox.wait_ready(), 3)
        result = sandbox.run(f"echo job-{index}-ok", timeout_s=30)
        row["exit_code"] = result.exit_code
        row["stdout"] = result.stdout.strip()[:200]
        row["ok"] = result.ok and f"job-{index}-ok" in result.stdout
        if not row["ok"]:
            row["error"] = (result.stderr or result.stdout)[:300]
    except Exception as exc:  # noqa: BLE001 - the whole point is to record it
        row["error"] = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        # Keep successful allocations alive until every create/exec attempt
        # completes; fast cleanup must not turn this into sequential capacity.
        try:
            hold.wait(timeout=600)
        except Exception as exc:
            row["error"] += f" | hold barrier: {exc}"
        sandbox.delete()
        row["cloud_id"] = sandbox.cloud_id
        row["cleanup_ok"] = sandbox.deleted
        row["delete_error"] = sandbox.delete_error
        row["duration_s"] = round(time.monotonic() - started, 3)
        row["finished_at"] = utc_now()
        record_probe(settings, "concurrency-job", sandbox, job=index, result=row)
    return row


def _breakage_notes(jobs: list[dict], requested: int) -> str:
    ok = [job for job in jobs if job["ok"]]
    failed = [job for job in jobs if not job["ok"]]
    names = [job["sandbox_name"] for job in jobs]
    unique = len(set(names)) == len(names)
    parts = [
        f"Requested {requested} concurrent sandboxes; {len(ok)} created and executed a "
        f"command, {len(failed)} failed."
    ]
    if unique:
        parts.append(
            f"All {len(names)} names were unique (observed zero name collisions)."
        )
    else:
        parts.append("Duplicate names were generated, which is a bug in make_name().")
    if ok:
        boots = sorted(float(job["boot_s"]) for job in ok)
        parts.append(
            f"Successful attempts reached ready in {boots[0]:.1f}s..{boots[-1]:.1f}s."
        )
    for job in failed:
        parts.append(f"{job['sandbox_name']}: {job['error'] or 'unknown error'}")
    parts.append("Allocations were held until all create/exec attempts completed before cleanup.")
    cleanup_failed = [job for job in jobs if not job.get("cleanup_ok")]
    parts.append(f"Cleanup failed for {len(cleanup_failed)} recorded allocations; use the owned-ID sweep for any leftovers.")
    if failed:
        parts.append(
            "No retry loop or paid upgrade was used; the failure is reported as observed."
        )
    return " ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Concurrency probe")
    parser.add_argument("--jobs", type=int, default=5)
    parser.add_argument("--out", default=str(ROOT / "reports/concurrency.json"))
    parser.add_argument("--log", default=str(ROOT / "reports/evidence/concurrency.log"))
    args = parser.parse_args()
    if not 1 <= args.jobs <= 5:
        parser.error("--jobs must be 1..5; no paid-capacity stress test is performed")

    settings = get_settings()
    if not settings.has_celesto:
        print("CELESTO_API_KEY is not set", file=sys.stderr)
        return 2
    ensure_dirs(settings)

    print(f"launching {args.jobs} concurrent create/exec/delete jobs ...", flush=True)
    launch, hold = Barrier(args.jobs), Barrier(args.jobs)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        jobs = list(pool.map(lambda i: _job(i, settings, launch, hold), range(1, args.jobs + 1)))

    payload = {
        "requested": args.jobs,
        "observed_at": utc_now(),
        "jobs": jobs,
        "breakage_notes": _breakage_notes(jobs, args.jobs),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(out_path, payload, settings)

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [payload["breakage_notes"], ""]
    for job in jobs:
        lines.append(
            f"{job['sandbox_name']}  ok={job['ok']}  boot={job['boot_s']}  "
            f"duration={job['duration_s']}s  {job['started_at']} -> {job['finished_at']}"
        )
        if job["error"]:
            lines.append(f"    error: {job['error']}")
    log_path.write_text(redact("\n".join(lines) + "\n", settings), encoding="utf-8")

    print(payload["breakage_notes"])
    print(f"\nwritten: {out_path}")
    return 0 if all(job["ok"] and job["cleanup_ok"] for job in jobs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
