"""Smoke the actual ledger HTTP API without launching an agent or cloud VM.

    python scripts/verify_api.py
    docker compose run --rm runner scripts/verify_api.py

A unique control-plane-only row is inserted then transitioned to deleted.
This is API evidence, not a sandbox run or a reliability-eval row.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.services.evidence import utc_now
from app.services.probes import write_json


def main():
    settings = get_settings()
    observations = []
    with httpx.Client(base_url=settings.api_base_url, timeout=15) as client:
        response = client.get("/health")
        observations.append({"check": "health", "status": response.status_code, "body": response.json()})
        assert response.status_code == 200 and response.json() == {"status": "ok"}
        response = client.get("/api/runs")
        assert response.status_code == 200
        assert any(row["sandbox_name"] == "sbx-seeddemo1" for row in response.json())
        observations.append({"check": "seed", "status": 200, "seed_present": True})
        run_id = f"api-check-{uuid.uuid4().hex[:12]}"
        payload = {"run_id": run_id, "sandbox_name": "sbx-apicheck123", "problem_id": "control-plane-smoke",
                   "state": "created", "execution_target": "celesto_sandbox",
                   "detail": {"scope": "HTTP API smoke only; no cloud allocation or agent execution"}}
        for state in ("created", "deleted"):
            payload["state"] = state
            response = client.post("/api/runs", json=payload)
            assert response.status_code == 201 and response.json()["state"] == state
            observations.append({"check": f"upsert_{state}", "status": response.status_code, "run_id": run_id})
        for key, invalid in (("state", "invalid"), ("sandbox_name", "bad-name"),
                             ("execution_target", "host")):
            response = client.post("/api/runs", json={**payload, key: invalid})
            assert response.status_code == 400
            observations.append({"check": f"reject_{key}", "status": response.status_code, "body": response.json()})
        missing = client.get(f"/api/runs/missing-{uuid.uuid4().hex}")
        assert missing.status_code == 404
        observations.append({"check": "missing_run", "status": 404})
        # With absent keys, scheduling is safe to exercise and must be 503.
        # With real keys, do not schedule the control-plane-only row.
        if not settings.has_celesto or not settings.has_llm:
            response = client.post(f"/api/runs/{run_id}/execute")
            assert response.status_code == 503
            observations.append({"check": "execute_missing_credentials", "status": 503})
    path = settings.report_dir / "evidence" / f"api-check-{uuid.uuid4().hex[:12]}.json"
    write_json(path, {"scope": "Real HTTP API smoke, not cloud execution", "observed_at": utc_now(),
                      "passed": True, "observations": observations}, settings)
    for observation in observations:
        print(f"{observation['check']}: HTTP {observation['status']}")
    print(f"API checks passed; evidence={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
