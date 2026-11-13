"""Read-only audit of Forge Trace v2 and Harbor result artifacts.

No model calls. The output is observational evidence, not a benchmark score.
The file only reads approved event fields and does not echo tool arguments,
model messages, environment variables, or secrets.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

_USAGE_KEYS = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
_LLM_EVENTS = {"llm_call_started", "llm_call_finished", "llm_call_failed", "llm_call_retry"}


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path.name}")
    return value


def analyze_trace(path: str | Path) -> dict[str, Any]:
    """Validate event correlation and sum *finished* provider usage only."""
    path = Path(path)
    counters: Counter[str] = Counter()
    started: dict[str, int] = {}
    ended: set[str] = set()
    pending: set[str] = set()
    run_ids: set[str] = set()
    models: set[str] = set()
    terminal: dict[str, Any] | None = None
    totals = {key: 0 for key in _USAGE_KEYS}
    events_seen = 0

    with path.open(encoding="utf-8") as stream:
        for line_no, raw in enumerate(stream, start=1):
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_no}") from exc
            if not isinstance(event, dict):
                raise ValueError(f"event at line {line_no} is not an object")
            event_type = event.get("event_type")
            payload = event.get("payload")
            if not isinstance(event_type, str) or not isinstance(payload, dict):
                raise ValueError(f"invalid event envelope at line {line_no}")
            events_seen += 1
            counters[event_type] += 1
            run_id = payload.get("run_id")
            if isinstance(run_id, str) and run_id:
                run_ids.add(run_id)
                if len(run_ids) > 1:
                    raise ValueError("multiple run_ids in one trace")

            if event_type in _LLM_EVENTS:
                call_id = payload.get("model_call_id")
                step_id = payload.get("step_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError(f"missing model_call_id at line {line_no}")
                if type(step_id) is not int or step_id < 1:
                    raise ValueError(f"invalid step_id at line {line_no}")
                if event_type == "llm_call_started":
                    if call_id in started:
                        raise ValueError(f"duplicate LLM start at line {line_no}")
                    started[call_id] = step_id
                    pending.add(call_id)
                    model = payload.get("model")
                    if isinstance(model, str) and model:
                        models.add(model)
                else:
                    if call_id not in started or started[call_id] != step_id:
                        raise ValueError(f"orphaned or mismatched LLM event at line {line_no}")
                    if event_type in {"llm_call_finished", "llm_call_failed"}:
                        if call_id in ended:
                            raise ValueError(f"duplicate LLM end at line {line_no}")
                        ended.add(call_id)
                        pending.discard(call_id)
                    if event_type == "llm_call_finished":
                        usage = payload.get("provider_usage")
                        if not isinstance(usage, dict):
                            raise ValueError(f"missing provider_usage at line {line_no}")
                        for key in totals:
                            value = usage.get(key)
                            if value is None:
                                continue
                            if type(value) is not int or value < 0:
                                raise ValueError(f"invalid provider usage {key} at line {line_no}")
                            totals[key] += value
            elif event_type == "run_terminated":
                if terminal is not None:
                    raise ValueError("duplicate run_terminated")
                terminal = {
                    "status": payload.get("status"),
                    "termination_reason": payload.get("termination_reason"),
                    "resource_reason": payload.get("resource_reason"),
                }
    if not events_seen:
        raise ValueError("empty trace")
    if not run_ids:
        raise ValueError("trace has no run_id")
    return {
        "run_id": next(iter(run_ids)),
        "model_names": sorted(models),
        "events": events_seen,
        "started_calls": counters["llm_call_started"],
        "finished_calls": counters["llm_call_finished"],
        "failed_calls": counters["llm_call_failed"],
        "retry_events": counters["llm_call_retry"],
        "unresolved_calls": [{"step": started[cid], "model_call_id": cid}
                             for cid in sorted(pending, key=lambda c: started[c])],
        "terminal": terminal,
        "trace_complete": terminal is not None and not pending,
        "observed_finished_usage": totals,
        "usage_is_lower_bound": bool(pending),
    }


def analyze_harbor(path: str | Path, *, verifier_details: str | Path | None = None) -> dict[str, Any]:
    """Extract only structured Harbor results; never infer missing usage as zero."""
    result = _json_object(Path(path))
    agent = result.get("agent_result") or {}
    verifier = result.get("verifier_result") or {}
    rewards = verifier.get("rewards") or {}
    reward = rewards.get("reward")
    if reward is not None and (isinstance(reward, bool) or not isinstance(reward, (int, float))):
        raise ValueError("Harbor reward must be numeric or null")
    detail = None
    if verifier_details is not None:
        detail = _json_object(Path(verifier_details))
        if reward is not None and detail.get("reward") != reward:
            raise ValueError("Harbor reward and verifier details disagree")
    exception = result.get("exception_info") or {}
    exception_type = exception.get("exception_type")
    # Harbor's exception is structured evidence about its supervisory phase.
    # A timeout does NOT prove the model itself is at fault.
    return {
        "exception_type": exception_type,
        "agent_execution_started_at": (result.get("agent_execution") or {}).get("started_at"),
        "agent_execution_finished_at": (result.get("agent_execution") or {}).get("finished_at"),
        "agent_usage_reported": {
            key: agent.get(key) for key in ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd")
        },
        "verifier_reward": reward,
        "verifier_result_recorded": reward is not None,
        "verifier_details_parse_status": detail.get("parse_status") if detail else None,
    }


def audit_trial(trace_path: str | Path, harbor_path: str | Path,
                *, verifier_details: str | Path | None = None) -> dict[str, Any]:
    trace = analyze_trace(trace_path)
    harbor = analyze_harbor(harbor_path, verifier_details=verifier_details)
    exception_type = harbor["exception_type"]
    if exception_type == "AgentTimeoutError":
        observed_rollout = "external_timeout"
    elif trace["terminal"] is not None:
        observed_rollout = trace["terminal"]["status"]
    else:
        observed_rollout = "unknown_no_terminal"
    return {
        "trace": trace,
        "harbor": harbor,
        "observed_rollout": observed_rollout,
        "scoreable": None,  # needs a PRE-FROZEN benchmark policy
        "failure_domain": None,  # cannot assign culpability from timeout alone
        "provider_billed_cost": None,  # trace usage is not the invoice
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--harbor-result", type=Path, required=True)
    parser.add_argument("--verifier-details", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit_trial(args.trace, args.harbor_result,
                                 verifier_details=args.verifier_details),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
