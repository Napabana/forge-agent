import json
import sys
from pathlib import Path


log_dir = Path(sys.argv[1])
traces = sorted(log_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
if not traces:
    print(json.dumps({"trace": None}))
    raise SystemExit(0)

totals = {
    "input_tokens": 0,
    "cached_tokens": 0,
    "output_tokens": 0,
    "reasoning_tokens": 0,
    "total_tokens": 0,
}
cost_usd = 0.0
cost_reported = False
finished_calls = 0
failed_calls = 0
run_termination = {}
for line in traces[-1].read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    event = json.loads(line)
    event_type = event.get("event_type")
    if event_type == "llm_call_finished":
        finished_calls += 1
        payload = event.get("payload", {})
        usage = payload.get("provider_usage") or {}
        for key in totals:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                totals[key] += int(value)
        value = payload.get("cost_usd")
        if isinstance(value, (int, float)):
            cost_usd += float(value)
            cost_reported = True
    elif event_type == "llm_call_failed":
        failed_calls += 1
    elif event_type == "run_terminated":
        payload = event.get("payload", {})
        run_termination = {
            "run_status": payload.get("status"),
            "termination_reason": payload.get("termination_reason"),
            "resource_reason": payload.get("resource_reason"),
        }

print(
    json.dumps(
        {
            "trace": traces[-1].name,
            "finished_calls": finished_calls,
            "failed_calls": failed_calls,
            **run_termination,
            **totals,
            "cost_usd": cost_usd if cost_reported else None,
        }
    )
)
