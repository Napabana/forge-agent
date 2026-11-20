from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from examples.harbor.result_policy import is_benchmark_outcome


ROOT = Path(__file__).resolve().parents[1]
USAGE_HELPER = ROOT / "examples" / "harbor" / "forge_usage_extract.py"


@pytest.mark.parametrize("status", ["incomplete", "gave_up"])
def test_harbor_policy_allows_gradable_nonzero_terminal_states(status):
    assert is_benchmark_outcome({"run_status": status})


@pytest.mark.parametrize("status", ["failed", "canceled", "success", None])
def test_harbor_policy_rejects_non_gradable_nonzero_terminal_states(status):
    assert not is_benchmark_outcome({"run_status": status})


def test_harbor_usage_helper_extracts_only_lead_usage_and_preserves_unknown_totals(tmp_path):
    worker = tmp_path / "worker.jsonl"
    worker.write_text('{"event_type":"task_start","payload":{"entrypoint":"worker"}}\n', encoding="utf-8")
    trace = tmp_path / "lead.jsonl"
    trace.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_type": "llm_call_finished",
                        "payload": {
                            "provider_usage": {
                                "input_tokens": 10,
                                "cached_tokens": 2,
                                "output_tokens": 3,
                                "reasoning_tokens": 1,
                                "total_tokens": 13,
                            },
                            "cost_usd": 0.25,
                        },
                    }
                ),
                json.dumps({"event_type": "llm_call_failed", "payload": {}}),
                json.dumps(
                    {
                        "event_type": "run_terminated",
                        "payload": {
                            "status": "incomplete",
                            "termination_reason": "resource_exhausted",
                            "resource_reason": "max_steps",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(USAGE_HELPER), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    usage = json.loads(completed.stdout)

    assert usage["run_status"] == "incomplete"
    assert usage["termination_reason"] == "resource_exhausted"
    assert usage["resource_reason"] == "max_steps"
    assert usage["finished_calls"] == 1
    assert usage["failed_calls"] == 1
    assert usage["trace"] == "lead.jsonl"
    assert usage["input_tokens"] is None
    assert usage["cached_tokens"] is None
    assert usage["observed_by_component"]["lead"] == {
        "input_tokens": 10, "cached_tokens": 2, "uncached_input_tokens": 8,
        "output_tokens": 3, "reasoning_tokens": 1, "total_tokens": 13,
    }
    assert usage["cost_usd"] is None


def test_harbor_usage_helper_refuses_to_guess_between_two_lead_traces(tmp_path):
    event = '{"event_type":"run_terminated","payload":{"status":"failed"}}\n'
    (tmp_path / "first.jsonl").write_text(event, encoding="utf-8")
    (tmp_path / "second.jsonl").write_text(event, encoding="utf-8")
    result = subprocess.run([sys.executable, str(USAGE_HELPER), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "expected one lead trace" in result.stderr


def test_harbor_usage_helper_reports_missing_trace(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(USAGE_HELPER), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {"trace": None}
