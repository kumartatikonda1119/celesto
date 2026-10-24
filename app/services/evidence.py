"""Evidence plumbing: audit log, failure trace and secret redaction.

Everything the grader reads as "evidence" flows through this module:

  logs/commands.jsonl          one line per command attempt (allow AND block)
  reports/failure_trace.jsonl  step + lifecycle events, incl. policy_applied
  reports/evidence/*           human-readable run evidence

Redaction is value-based (we know the configured secrets) rather than
pattern-based, so git SHAs and other long hex strings survive intact.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings

EXECUTION_TARGET = "celesto_sandbox"
_LOCK = threading.Lock()
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}")
_SK = re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def redact(text: str, settings: Settings | None = None) -> str:
    """Remove configured secrets and obvious bearer tokens from a string."""
    settings = settings or get_settings()
    if not text:
        return text
    for secret in (settings.celesto_api_key, settings.llm_api_key):
        if secret and len(secret) >= 6:
            text = text.replace(secret, "[REDACTED]")
    if settings.canary:
        text = text.replace(settings.canary, "[CANARY]")
    text = _BEARER.sub(r"\1[REDACTED]", text)
    text = _SK.sub("sk-[REDACTED]", text)
    return text


def ensure_dirs(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    settings.report_dir.mkdir(parents=True, exist_ok=True)
    (settings.report_dir / "evidence").mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def append_command(
    *,
    sandbox_name: str,
    command: str,
    decision: str,
    reason: str = "",
    exit_code: int | None = None,
    settings: Settings | None = None,
) -> None:
    """Record one command attempt. Blocked attempts are recorded too."""
    settings = settings or get_settings()
    assert decision in {"allow", "block"}, decision
    payload: dict[str, Any] = {
        "ts": utc_now(),
        "sandbox_name": sandbox_name,
        "command": redact(command, settings),
        "decision": decision,
        # Exactly this string: the audit log asserts where commands run.
        "execution_target": EXECUTION_TARGET,
        "reason": redact(reason, settings),
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    _append_jsonl(settings.commands_log, payload)


class FailureTrace:
    """Append-only JSONL trace of the 10-step pipeline and lifecycle."""

    def __init__(
        self,
        path: Path | None = None,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or get_settings()
        self.settings = settings
        self.path = path or (settings.report_dir / "failure_trace.jsonl")

    def event(self, event: str, **fields: Any) -> None:
        payload: dict[str, Any] = {"ts": utc_now(), "event": event}
        payload.update(fields)
        _append_jsonl(self.path, json.loads(redact(json.dumps(payload), self.settings)))


class EvidenceLog:
    """Human-readable evidence file used by capability/containment reports."""

    def __init__(self, name: str, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        ensure_dirs(settings)
        self.path = settings.report_dir / "evidence" / name
        self.settings = settings

    def write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(redact(text, self.settings), encoding="utf-8")

    def append(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(redact(text, self.settings))
