"""The run-ledger HTTP surface.

Graded contract:
  GET  /health       -> 200 {"status": "ok"}
  GET  /api/runs     -> 200 [run, ...]   (fresh db includes sbx-seeddemo1)
  POST /api/runs     -> 201 stored run   (400 for a bad target/state/name)
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException

from ..config import get_settings
from ..database import get_run, list_runs, upsert_run
from ..schemas import RunIn, validate_run

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    # Intentionally independent of Postgres and Celesto: the container is
    # "healthy" when the process is serving requests. Postgres readiness is
    # enforced separately by compose's depends_on condition.
    return {"status": "ok"}


@router.get("/api/runs")
def api_list_runs() -> list[dict]:
    return list_runs()


@router.get("/api/runs/{run_id}")
def api_get_run(run_id: str) -> dict:
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.post("/api/runs", status_code=201)
def api_create_run(payload: RunIn) -> dict:
    error = validate_run(payload)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return upsert_run(
        run_id=payload.run_id,
        sandbox_name=payload.sandbox_name,
        problem_id=payload.problem_id,
        state=payload.state,
        execution_target=payload.execution_target,
        detail=payload.detail,
    )


@router.post("/api/runs/{run_id}/execute", status_code=202)
def api_execute_run(run_id: str, background: BackgroundTasks) -> dict:
    """Kick off the agent for a queued run inside this process.

    Additive to the graded contract. The host-side scripts call the same
    pipeline function directly; this endpoint is what makes the API the
    control plane in the demo.
    """
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    settings = get_settings()
    if not settings.has_celesto:
        raise HTTPException(
            status_code=503,
            detail="CELESTO_API_KEY is not configured; the sandbox layer is unavailable",
        )
    if not settings.has_llm:
        raise HTTPException(
            status_code=503,
            detail="LLM_API_KEY/LLM_MODEL are not configured; the agent loop is unavailable",
        )

    from ..services.pipeline import execute_run_record

    background.add_task(execute_run_record, run)
    return {"status": "scheduled", "run_id": run_id}
