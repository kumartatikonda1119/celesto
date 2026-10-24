"""Exercise destructive-command containment against our own disposable guest.

The attack is deliberately submitted through Sandbox.run, never a host shell.
The target is a guest-only marker directory, not a host or third-party path.
A blocked decision, surviving guest marker, and unchanged trusted-file hash
are all required. This is a policy-layer test, not proof that arbitrary Python
code cannot bypass a regex policy.

    docker compose run --rm runner scripts/run_containment.py
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.services.celesto_service import Sandbox, make_name
from app.services.probes import record_probe, relative_evidence, write_json

GUEST_TARGET = "/work/saferepo-containment-target"
ATTACK_COMMAND = f"rm -rf {GUEST_TARGET}"


def main():
    settings = get_settings()
    if not settings.has_celesto:
        print("CELESTO_API_KEY must be configured; no containment result was generated", file=sys.stderr)
        return 2
    # Trusted operator read, not an agent tool. The agent never receives a
    # host path or a mount containing host source or credentials.
    sentinel = ROOT / "Dockerfile"
    before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    sandbox = Sandbox(make_name(), settings)
    result = {"blocked": False, "marker_survived": False, "error": ""}
    try:
        sandbox.create()
        sandbox.wait_ready()
        sandbox.write_file(f"{GUEST_TARGET}/marker.txt", "owned-guest-marker")
        attempted = sandbox.run(ATTACK_COMMAND)
        result.update(blocked=attempted.blocked, decision=attempted.decision,
                      exit_code=attempted.exit_code, reason=attempted.stderr)
        result["marker_survived"] = sandbox.read_file(f"{GUEST_TARGET}/marker.txt") == "owned-guest-marker"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        sandbox.delete()
        after = hashlib.sha256(sentinel.read_bytes()).hexdigest()
        result.update(host_sha256_before=before, host_sha256_after=after,
                      host_untouched=before == after, command=ATTACK_COMMAND)
        evidence = record_probe(settings, "containment", sandbox, **result)
    passed = result["blocked"] and result["marker_survived"] and result["host_untouched"] and sandbox.deleted
    report = {
        "attack": "destructive_command", "blocked": result["blocked"],
        "host_untouched": result["host_untouched"], "passed": passed,
        "sandbox_name": sandbox.name, "cleanup_ok": sandbox.deleted,
        "evidence_paths": [relative_evidence(evidence, settings)],
        "scope": "Control-plane policy blocks the guest-only recursive delete before SDK execution; no claim of universal shell/code confinement.",
    }
    write_json(settings.report_dir / "containment.json", report, settings)
    print(f"Containment passed={passed}; evidence={evidence}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
