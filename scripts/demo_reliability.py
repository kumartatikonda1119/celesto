"""Demo helper: print the reliability eval summary."""
import json

with open("reports/reliability.json") as f:
    r = json.load(f)

print("=== SafeRepo Reliability Report ===")
print()
print(f"  Runs:           {r['run_count']}")
print(f"  Success Rate:   {r['success_rate']*100:.1f}%")
print(f"  Median Time:    {r['median_wall_clock_s']:.1f}s")
print(f"  P90 Time:       {r['p90_wall_clock_s']:.1f}s")
print(f"  Median Boot:    {r['median_boot_to_ready_s']:.1f}s")
print(f"  Total Cost:     ${r['total_cost_usd']:.4f}")
print()
print("  Failed runs:")
for run in r["runs"]:
    if not run["success"]:
        print(f"    {run['sandbox_name']}  wall={run['wall_clock_s']:.1f}s")

print()
print("=== Step-7 Failure Recovery ===")
print()
with open("reports/failure_trace.jsonl") as f:
    events = [json.loads(l) for l in f if l.strip()]
for e in events:
    if e.get("event") in ("step_failed", "cleanup", "policy_applied"):
        step = e.get("step", "-")
        name = e.get("sandbox_name", e.get("old_sandbox_name", ""))[-20:]
        print(f"  [{e['ts'][-12:]}] {e['event']:20s}  step={step}  {name}")
