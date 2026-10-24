"""Phase 0 gate: prove create -> exec -> delete works, and record what the SDK
actually exposes.

Run:  python scripts/celesto_smoke_test.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.services.celesto_service import Sandbox, sdk_notes  # noqa: E402
from app.services.evidence import ensure_dirs, redact  # noqa: E402
from app.services.probes import record_probe  # noqa: E402


def main() -> int:
    settings = get_settings()
    ensure_dirs(settings)
    if not settings.has_celesto:
        print("CELESTO_API_KEY is not set. Add it to .env first.")
        return 2

    notes = sdk_notes()
    print("SDK notes:")
    print(json.dumps(notes, indent=2, default=str))

    report = [f"provider: {settings.celesto_provider}", f"template: {settings.celesto_template}"]
    report.append("sdk notes:\n" + json.dumps(notes, indent=2, default=str))

    sandbox = Sandbox(settings=settings)
    ok = False
    try:
        sandbox.create()
        print(f"created sandbox: {sandbox.name} (cloud id {sandbox.cloud_id})")
        boot = sandbox.wait_ready()
        print(f"ready in {boot:.2f}s")
        report.append(f"sandbox: {sandbox.name}")
        report.append(f"cloud_id: {sandbox.cloud_id}")
        report.append(f"create_options: {json.dumps(sandbox.create_options, default=str)}")
        report.append(f"boot_to_ready_s: {boot:.3f}")

        results = []
        for command in ("uname -a", "cat /etc/os-release | head -4", "pwd", "id"):
            result = sandbox.run(command)
            results.append(result)
            print(f"$ {command}\nexit={result.exit_code}\n{result.stdout}{result.stderr}")
            report.append(
                f"$ {command}\nexit={result.exit_code}\n{result.stdout}{result.stderr}"
            )
        ok = all(result.ok for result in results)
    except Exception as exc:
        report.append(f"smoke_error: {type(exc).__name__}: {exc}")
        print(report[-1])
    finally:
        sandbox.delete()
        ok = ok and sandbox.deleted
        report.append(f"cleanup: deleted={sandbox.deleted}, error={sandbox.delete_error}")
        print(report[-1])

    record_probe(settings, "smoke", sandbox, passed=ok, output="\n\n".join(report))
    evidence = settings.report_dir / "evidence" / "sdk_notes.md"
    evidence.write_text(redact("# Celesto SDK smoke test\n\n" + "\n\n".join(report) + "\n", settings), encoding="utf-8")
    print(f"wrote {evidence}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
