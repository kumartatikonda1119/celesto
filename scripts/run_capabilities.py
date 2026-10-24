"""Verify cloud persistence and create-time network isolation with real guests.

Run inside the Linux runner on Windows:
    docker compose run --rm runner scripts/run_capabilities.py

Only successful, cleaned-up experiments are added to capabilities.json. Failed
attempts remain in evidence. This does not claim a cloud domain allow-list:
network_policy is the separately tested open/off create-time policy.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.services.celesto_service import Sandbox, make_name
from app.services.probes import record_probe, relative_evidence, write_json


def persistence(settings):
    sandbox = Sandbox(make_name(), settings, external_volume=True)
    marker = uuid.uuid4().hex
    results = {"passed": False, "marker_survived": False, "error": ""}
    try:
        sandbox.create()
        sandbox.wait_ready()
        sandbox.write_file("/home/ohm/saferepo-persistence.txt", marker)
        if not sandbox.stop() or not sandbox.start():
            raise RuntimeError("stop/resume not available")
        sandbox.wait_ready()
        observed = sandbox.read_file("/home/ohm/saferepo-persistence.txt").strip()
        results["marker_survived"] = observed == marker
        results["passed"] = results["marker_survived"]
    except Exception as exc:
        results["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        sandbox.delete()
        results["passed"] = results["passed"] and sandbox.deleted
        path = record_probe(settings, "persistence", sandbox, **results)
    return results["passed"], path


def network_policy(settings):
    # The same known-good HTTPS request is tried in separate open/off guests.
    # A missing curl binary or unreachable control is NOT proof of isolation.
    observations = []
    evidence = []
    for mode in ("open", "off"):
        configured = replace(settings, network_mode=mode)
        sandbox = Sandbox(make_name(), configured)
        result = {"mode": mode, "tool_available": False, "exit_code": None, "error": ""}
        try:
            sandbox.create()
            sandbox.wait_ready()
            tool = sandbox.run("command -v curl", timeout_s=15)
            result["tool_available"] = tool.ok and bool(tool.stdout.strip())
            if not result["tool_available"]:
                raise RuntimeError("curl missing; cannot verify network policy")
            probe = sandbox.run("curl -I -sS --max-time 8 https://github.com", timeout_s=15)
            result.update(exit_code=probe.exit_code, stdout=probe.stdout, stderr=probe.stderr)
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            sandbox.delete()
            result["deleted"] = sandbox.deleted
            result["policy_applied"] = sandbox.create_options.get("network_policy") == mode
            evidence.append(relative_evidence(record_probe(settings, "network-policy", sandbox, result=result), settings))
            observations.append(result)
    opened, closed = observations
    passed = (
        all(r["tool_available"] and r["deleted"] and r["policy_applied"] for r in observations)
        and opened["exit_code"] == 0
        and isinstance(closed["exit_code"], int) and closed["exit_code"] != 0
    )
    path = settings.report_dir / "evidence" / f"network-policy-summary-{uuid.uuid4().hex[:12]}.json"
    write_json(path, {"passed": passed, "evidence_paths": evidence, "observations": observations}, settings)
    return passed, path


def main():
    parser = argparse.ArgumentParser(description="Verify actual Celesto cloud capabilities")
    parser.add_argument("--only", choices=("persistence", "network_policy"))
    args = parser.parse_args()
    settings = get_settings()
    if not settings.has_celesto:
        print("CELESTO_API_KEY must be configured; no capability claim was generated", file=sys.stderr)
        return 2
    report = settings.report_dir / "capabilities.json"
    existing = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {"capabilities": []}
    descriptions = {
        "persistence": "A synthetic marker on an explicitly enabled external volume is written in the guest, then checked after stop/resume. The preserved files avoid silently losing task state while the guest is stopped; this experiment is separate from all fresh eval runs.",
        "network_policy": "Cloud network policy is fixed at creation. Separate open/off guests execute the same HTTPS probe with curl verified present; only a successful open control and failed off request count as evidence. This is not a per-domain cloud allow-list.",
    }
    failed = False
    for name, experiment in (("persistence", persistence), ("network_policy", network_policy)):
        if args.only and args.only != name:
            continue
        passed, evidence = experiment(settings)
        print(f"{name}: passed={passed}, evidence={evidence}")
        if not passed:
            failed = True
            continue
        existing["capabilities"] = [c for c in existing["capabilities"] if c.get("id") != name]
        existing["capabilities"].append({"id": name, "why": descriptions[name],
            "evidence_path": relative_evidence(evidence, settings)})
        write_json(report, existing, settings)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
