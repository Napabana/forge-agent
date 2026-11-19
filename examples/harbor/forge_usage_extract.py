"""Summarize a single lead trace and its non-overlapping usage components."""

import json
import sys
from pathlib import Path


KEYS = ("input_tokens", "output_tokens", "cached_tokens", "uncached_input_tokens",
        "reasoning_tokens", "total_tokens")


def reported_usage_value(row, key):
    if row.get("estimated") or row.get("estimated_calls"):
        return None
    if key in {"cached_tokens", "uncached_input_tokens"}:
        if row.get("cache_read_reported") is False or row.get("unknown_cache_read_calls"):
            return None
        total, cached = row.get("input_tokens"), row.get("cached_tokens")
        paired = type(total) is int and type(cached) is int
        if key == "uncached_input_tokens":
            return total - cached if paired and 0 <= cached <= total else None
        if paired and cached > total:
            return None
    return row.get(key)


def summarize(events):
    rows = {"lead": [], "workers": [], "semantic": []}
    incomplete = {key: False for key in rows}
    calls = {"finished_calls": 0, "failed_calls": 0, "retry_events": 0, "budget_rejections": 0}
    terminal = None
    workers = None
    pending = set()
    for event in events:
        kind, payload = event["event_type"], event["payload"]
        call_id = payload.get("model_call_id")
        if kind == "llm_call_started":
            pending.add(call_id)
        elif kind in {"llm_call_finished", "llm_call_failed"}:
            pending.discard(call_id)
            if kind == "llm_call_finished":
                calls["finished_calls"] += 1
                rows["lead"].append(payload.get("provider_usage") or {})
            elif payload.get("provider_request_sent") is False:
                calls["budget_rejections"] += 1
            else:
                calls["failed_calls"] += 1
                incomplete["lead"] = True
        elif kind == "llm_call_retry":
            calls["retry_events"] += 1
            incomplete["lead"] = True
        elif kind == "workers_summary":
            if workers is not None:
                raise ValueError("multiple workers summaries in lead trace")
            workers = payload
            rows["workers"].append(payload.get("worker_usage") or {})
            incomplete["workers"] = workers.get("usage_complete") is not True
        elif kind in {"context_compacted", "context_compaction_failed"}:
            usage = payload.get("summary_usage")
            # A failure can also produce a fallback checkpoint carrying the same usage.
            if kind == "context_compaction_failed":
                rows["semantic"].append(usage or {})
                incomplete["semantic"] = True
            elif usage and not payload.get("semantic_error"):
                rows["semantic"].append(usage)
        elif kind == "run_terminated":
            if terminal is not None:
                raise ValueError("multiple run terminations in lead trace")
            terminal = payload

    incomplete["lead"] |= bool(pending)
    if terminal is None:
        incomplete = dict.fromkeys(rows, True)
    observed = {}
    usage = {}
    for component, entries in rows.items():
        observed[component] = {}
        usage[component] = {}
        for key in KEYS:
            values = [reported_usage_value(row, key) for row in entries]
            valid = [value for value in values if type(value) is int and value >= 0]
            count = sum(valid)
            observed[component][key] = count if valid else None
            usage[component][key] = count if not incomplete[component] and len(valid) == len(entries) else None

    result = {key: sum(usage[component][key] for component in rows)
              if all(usage[component][key] is not None for component in rows) else None
              for key in KEYS}
    terminal = terminal or {}
    return {**result, **calls, "by_component": usage, "observed_by_component": observed,
            "worker_metrics": None if workers is None else {
                key: workers[key] for key in ("delegation_count", "worker_count",
                                             "worker_success_count", "worker_failure_count",
                                             "worker_tool_calls", "usage_complete") if key in workers},
            "run_status": terminal.get("status"), "termination_reason": terminal.get("termination_reason"),
            "resource_reason": terminal.get("resource_reason"), "cost_usd": None}


def extract(log_dir: Path) -> dict:
    lead = []
    for path in log_dir.glob("*.jsonl"):
        with path.open(encoding="utf-8") as stream:
            first = next((json.loads(line) for line in stream if line.strip()), None)
            if not first:
                continue
            payload = first.get("payload", {})
            if payload.get("entrypoint") == "worker":
                continue
            lead.append((path, first.get("task_id")))
    if not lead:
        return {"trace": None}
    if len(lead) != 1:
        raise ValueError("expected one lead trace; refusing to infer it by modification time")
    path, task_id = lead[0]
    with path.open(encoding="utf-8") as stream:
        return {"trace": path.name, "task_id": task_id,
                **summarize(json.loads(line) for line in stream if line.strip())}


if __name__ == "__main__":
    print(json.dumps(extract(Path(sys.argv[1]))))
