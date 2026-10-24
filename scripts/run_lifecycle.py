"""Verify cleanup on normal completion, raised failure, and simulated interrupt.

All three probes create real disposable Celesto guests; interruption is a
controlled KeyboardInterrupt in the trusted runner, not a hard process kill.
Only measured outcomes are written to lifecycle.json.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.services.celesto_service import Sandbox, make_name
from app.services.probes import record_probe, relative_evidence, write_json


def cleanup_probe(settings, scenario):
    sandbox = Sandbox(make_name(), settings)
    ready = False
    expected_event_seen = False
    error = ""
    try:
        sandbox.create()
        sandbox.wait_ready()
        ready = True
        if scenario == "failure":
            raise RuntimeError("controlled lifecycle failure after readiness")
        if scenario == "interrupt":
            raise KeyboardInterrupt("controlled lifecycle interrupt after readiness")
        expected_event_seen = True
    except KeyboardInterrupt:
        expected_event_seen = scenario == "interrupt"
    except Exception as exc:
        expected_event_seen = ready and scenario == "failure"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        sandbox.delete()
    passed = ready and expected_event_seen and sandbox.deleted
    path = record_probe(settings, f"cleanup-{scenario}", sandbox, scenario=scenario,
                        ready=ready, passed=passed, simulated=scenario != "success", error=error)
    return sandbox.name, passed, path


def main():
    settings = get_settings()
    if not settings.has_celesto:
        print("CELESTO_API_KEY must be configured; no cleanup claims generated", file=sys.stderr)
        return 2
    payload = {"sandbox_names": [], "evidence_paths": [],
               "interrupt_scope": "Controlled KeyboardInterrupt, not SIGKILL or power loss. Use the owned-ID sweep after hard termination."}
    for scenario in ("success", "failure", "interrupt"):
        name, passed, path = cleanup_probe(settings, scenario)
        payload[f"cleanup_on_{scenario}"] = passed
        payload["sandbox_names"].append(name)
        payload["evidence_paths"].append(relative_evidence(path, settings))
        print(f"cleanup_on_{scenario}: {passed}; evidence={path}")
    write_json(settings.report_dir / "lifecycle.json", payload, settings)
    return 0 if all(payload[f"cleanup_on_{s}"] for s in ("success", "failure", "interrupt")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
