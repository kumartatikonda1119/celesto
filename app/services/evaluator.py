"""Reliability statistics.

The arithmetic here is dictated by the challenge contract, so it is written
explicitly rather than delegated to statistics.quantiles() (whose interpolation
would not match the stated rule):

  * median of an even-length series = mean of the two central values (1-based)
  * p90 = the value at 1-based rank ceil(0.90 * n)
  * success_rate = successes / n
  * total_cost = sum of per-run costs

Runs are written sorted by wall_clock_s ascending and failed runs are never
dropped.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable


def median(values: Iterable[float]) -> float:
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0


def p90(values: Iterable[float]) -> float:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return 0.0
    rank = math.ceil(0.90 * len(ordered))
    rank = max(1, min(rank, len(ordered)))
    return ordered[rank - 1]


def summarise(runs: list[dict[str, Any]], postmortem_path: str = "reports/postmortem.md") -> dict[str, Any]:
    if not runs:
        raise ValueError("cannot summarise zero runs")
    ordered = sorted(runs, key=lambda r: float(r["wall_clock_s"]))
    wall = [float(r["wall_clock_s"]) for r in ordered]
    boot = [float(r["boot_to_ready_s"]) for r in ordered]
    successes = sum(1 for r in ordered if r.get("success") is True)
    return {
        "run_count": len(ordered),
        "runs": ordered,
        "success_rate": round(successes / len(ordered), 6),
        "median_wall_clock_s": round(median(wall), 3),
        "p90_wall_clock_s": round(p90(wall), 3),
        "median_boot_to_ready_s": round(median(boot), 3),
        "total_cost_usd": round(sum(float(r["cost_usd"]) for r in ordered), 6),
        "postmortem_path": postmortem_path,
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def write_reliability(runs: list[dict[str, Any]], out_path: Path) -> dict[str, Any]:
    summary = summarise(runs)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
