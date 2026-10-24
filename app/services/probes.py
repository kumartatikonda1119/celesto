"""Trusted probe output helpers. These never execute a command on the host."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import Settings
from .evidence import redact, utc_now


def write_json(path: Path, payload: dict[str, Any], settings: Settings) -> None:
    """Write redacted JSON atomically so a reader never sees half a report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(redact(json.dumps(payload, indent=2), settings), encoding="utf-8")
    temporary.replace(path)


def sandbox_row(sandbox) -> dict[str, Any]:
    return {
        "sandbox_name": sandbox.name,
        "cloud_id": sandbox.cloud_id,
        "create_options": sandbox.create_options,
        "deleted": sandbox.deleted,
        "delete_error": sandbox.delete_error,
    }


def record_probe(settings: Settings, probe: str, sandbox, **fields) -> Path:
    path = settings.report_dir / "evidence" / f"{probe}-{sandbox.name}.json"
    write_json(path, {"probe": probe, "observed_at": utc_now(), **sandbox_row(sandbox), **fields}, settings)
    return path


def relative_evidence(path: Path, settings: Settings) -> str:
    return path.relative_to(settings.report_dir.parent).as_posix()
