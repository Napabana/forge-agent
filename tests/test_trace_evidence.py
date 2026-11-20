"""Offline validation of Trace evidence, plus the archived real Harbor trial."""
import json
from pathlib import Path

import pytest

from examples.harbor.trace_evidence import analyze_trace, audit_trial


def event(event_type, call_id="c1", step=1, **payload):
    return dict(event_type=event_type, payload=dict(run_id="r1", step_id=step,
                                                    model_call_id=call_id, **payload))


def fixture(tmp_path, *, pending=False, terminal=True, reward=1.0, reported_usage=None):
    rows = [event("llm_call_started", model="x"), event(
        "llm_call_finished", provider_usage=dict(input_tokens=15, output_tokens=5,
                                                  total_tokens=20, cached_tokens=0,
                                                  reasoning_tokens=4))]
    if pending:
        rows.append(event("llm_call_started", call_id="c2", step=2, model="x"))
    if terminal:
        rows.append(event("run_terminated", status="success", termination_reason="completion_satisfied"))
    trace = tmp_path / "trace.jsonl"
    trace.write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")
    harbor = tmp_path / "harbor.json"
    harbor.write_text(json.dumps(dict(
        exception_info={"exception_type": "AgentTimeoutError"} if pending else None,
        agent_result=reported_usage,
        verifier_result={"rewards": {"reward": reward}},
    )), encoding="utf-8")
    return trace, harbor


def test_complete_trace_and_success(tmp_path):
    t, h = fixture(tmp_path)
    actual = audit_trial(t, h)
    assert actual["trace"]["trace_complete"] is True
    assert actual["trace"]["observed_finished_usage"]["total_tokens"] == 20
    assert actual["observed_rollout"] == "success"
    assert actual["scoreable"] is None
    assert actual["trace"]["observed_finished_usage"]["uncached_input_tokens"] == 15


def test_actual_cost_requires_explicit_reconciled_bill(tmp_path):
    t, h = fixture(tmp_path, reported_usage={"cost_usd": 1.25})
    assert audit_trial(t, h)["provider_billed_cost"] is None
    actual = audit_trial(t, h, reconciled_billed_cost_usd=0.001063)
    assert actual["provider_billed_cost"] == 0.001063
    assert actual["provider_billed_cost_source"] == "operator_reconciled_provider_bill"
    with pytest.raises(ValueError, match="finite and non-negative"):
        audit_trial(t, h, reconciled_billed_cost_usd=float("nan"))


def test_partial_trace_timeout_and_null_usage_are_separate(tmp_path):
    t, h = fixture(tmp_path, pending=True, terminal=False)
    actual = audit_trial(t, h)
    assert actual["trace"]["started_calls"] == 2
    assert actual["trace"]["finished_calls"] == 1
    assert actual["trace"]["unresolved_calls"][0]["step"] == 2
    assert actual["trace"]["usage_is_lower_bound"] is True
    assert actual["harbor"]["agent_usage_reported"]["n_input_tokens"] is None
    assert actual["harbor"]["verifier_reward"] == 1.0
    assert actual["observed_rollout"] == "external_timeout"
    assert actual["failure_domain"] is None


def test_duplicate_start_is_rejected(tmp_path):
    t, _ = fixture(tmp_path)
    rows = t.read_text().splitlines()
    t.write_text(rows[0] + "\n" + rows[0] + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate LLM start"):
        analyze_trace(t)


def test_orphan_finish_is_rejected(tmp_path):
    t, _ = fixture(tmp_path)
    t.write_text(json.dumps(event("llm_call_finished", provider_usage={})) + "\n")
    with pytest.raises(ValueError, match="orphaned"):
        analyze_trace(t)


def test_truncated_jsonl_is_rejected(tmp_path):
    t, _ = fixture(tmp_path)
    t.write_text(t.read_text() + '{"event_type":')
    with pytest.raises(ValueError, match="invalid JSONL"):
        analyze_trace(t)


def test_cross_run_events_are_rejected(tmp_path):
    t, _ = fixture(tmp_path)
    rows = t.read_text().splitlines()
    e = json.loads(rows[1])
    e["payload"]["run_id"] = "r2"
    rows[1] = json.dumps(e)
    t.write_text("\n".join(rows) + "\n")
    with pytest.raises(ValueError, match="multiple run_ids"):
        analyze_trace(t)


def test_retry_event_does_not_count_as_another_call(tmp_path):
    t, _ = fixture(tmp_path)
    rows = t.read_text().splitlines()
    rows.insert(1, json.dumps(event("llm_call_retry", error_type="rate_limit", status_code=429)))
    t.write_text("\n".join(rows) + "\n")
    a = analyze_trace(t)
    assert a["retry_events"] == 1 and a["started_calls"] == 1
    assert a["usage_is_lower_bound"] is True


def test_verifier_disagreement_is_rejected(tmp_path):
    t, h = fixture(tmp_path)
    d = tmp_path / "detail.json"
    d.write_text('{"reward": 0, "parse_status":"ok"}')
    with pytest.raises(ValueError, match="disagree"):
        audit_trial(t, h, verifier_details=d)


def test_failed_model_call_closes_pending_without_usage(tmp_path):
    t, _ = fixture(tmp_path)
    rows = t.read_text().splitlines()[:1]
    rows.append(json.dumps(event("llm_call_failed", error_type="rate_limit")))
    t.write_text("\n".join(rows) + "\n")
    a = analyze_trace(t)
    assert a["failed_calls"] == 1 and not a["unresolved_calls"]
    assert a["usage_is_lower_bound"] is True
    assert a["observed_finished_usage"]["input_tokens"] == 0


def test_negative_provider_tokens_fail_closed(tmp_path):
    t, _ = fixture(tmp_path)
    rows = t.read_text().splitlines()
    row = json.loads(rows[1])
    row["payload"]["provider_usage"]["input_tokens"] = -1
    rows[1] = json.dumps(row)
    t.write_text("\n".join(rows) + "\n")
    with pytest.raises(ValueError, match="invalid provider usage"):
        analyze_trace(t)


def test_missing_provider_token_field_is_not_silently_complete(tmp_path):
    t, _ = fixture(tmp_path)
    rows = t.read_text().splitlines()
    row = json.loads(rows[1])
    del row["payload"]["provider_usage"]["input_tokens"]
    rows[1] = json.dumps(row)
    t.write_text("\n".join(rows) + "\n")
    summary = analyze_trace(t)
    assert summary["observed_finished_usage"]["input_tokens"] is None
    assert summary["observed_finished_usage"]["uncached_input_tokens"] is None
    assert summary["usage_is_lower_bound"] is True
    assert "input_tokens" in summary["usage_fields_missing"]
