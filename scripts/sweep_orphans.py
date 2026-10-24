"""Sweep only cloud IDs recorded by SafeRepo in its own evidence files.

    docker compose run --rm runner scripts/sweep_orphans.py
    docker compose run --rm runner scripts/sweep_orphans.py --yes

Cloud assigns VM IDs; a logical sbx- audit name cannot be used to delete a VM.
No global account-wide name matching or local CLI fallback is used. List-only
is the default. --yes explicitly authorizes deletion of the displayed IDs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.services.celesto_service import delete_owned_computer, inspect_owned_computer


def owned_ids(evidence_dir: Path) -> dict[str, str]:
    owned = {}
    for path in evidence_dir.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        sandboxes = payload.get("sandboxes", [])
        if not isinstance(sandboxes, list) or not all(isinstance(row, dict) for row in sandboxes):
            raise ValueError(f"malformed sandbox records in {path.name}")
        rows = [payload] + sandboxes
        for row in rows:
            name = row.get("sandbox_name") or row.get("name", "")
            cloud_id = row.get("cloud_id")
            if isinstance(name, str) and name.startswith("sbx-saferepo") and isinstance(cloud_id, str) and cloud_id:
                owned[cloud_id] = name
    return owned


def main():
    parser = argparse.ArgumentParser(description="Inspect/delete SafeRepo-owned cloud IDs")
    parser.add_argument("--yes", action="store_true", help="delete the SafeRepo-owned IDs")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.has_celesto:
        print("CELESTO_API_KEY must be configured", file=sys.stderr)
        return 2
    try:
        owned = owned_ids(settings.report_dir / "evidence")
    except (OSError, ValueError) as exc:
        print(f"Cannot safely read owned IDs: {exc}", file=sys.stderr)
        return 2
    failed = False
    for cloud_id, name in owned.items():
        try:
            state = inspect_owned_computer(cloud_id, settings)
            print(f"{name}: cloud_id={cloud_id} status={state['status']}")
            if args.yes and state["status"] != "deleted":
                delete_owned_computer(cloud_id, settings)
                print(f"  deleted {cloud_id}")
        except Exception as exc:
            failed = True
            print(f"{name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if not args.yes:
        print("List-only; --yes deletes only the SafeRepo IDs recorded above.")
    if not owned:
        print("No recorded SafeRepo cloud IDs; this does not prove the account has no orphans.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
