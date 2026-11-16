from __future__ import annotations

import subprocess
from types import SimpleNamespace

from context.token_budget import TokenBudget, estimate_messages_tokens
from evals.context_policy_agent_ablation import (
    PruningOnlyPolicy,
    _aggregate,
    _init_fixture_repo,
    build_history,
    load_manifest,
)
from evals.harness import materialize_case


def test_b2_manifest_has_three_long_history_cases_and_current_task_once(tmp_path):
    defaults, cases = load_manifest()

    assert [case.base.case_id for case in cases] == [
        "long-hard-constraint",
        "huge-tool-history",
        "superseded-state",
    ]
    assert defaults["budget_tokens"] == 8_000
    assert defaults["max_steps"] == 12

    for case in cases:
        repo = materialize_case(case.base, tmp_path / case.base.case_id)
        history = build_history(case, repo)
        visible = "\n".join(message.content for message in history.to_list())
        assert visible.count(case.base.prompt) == 1
        assert estimate_messages_tokens(history.to_dicts()) > 2_000


def test_b2_fixture_repo_is_standalone_and_tracks_changes(tmp_path):
    _, cases = load_manifest()
    case = cases[0]
    repo = materialize_case(case.base, tmp_path / case.base.case_id)

    _init_fixture_repo(repo)

    top = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], cwd=repo, text=True
    ).strip()
    assert top == str(repo.resolve())
    assert subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=repo, text=True
    ) == ""

    (repo / "textops.py").write_text("def normalize_title(value):\n    return value\n", encoding="utf-8")
    assert "textops.py" in subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=repo, text=True
    )


def test_pruning_only_policy_prunes_old_large_tool_output_but_keeps_current_task(tmp_path):
    _, cases = load_manifest()
    case = next(item for item in cases if item.base.case_id == "huge-tool-history")
    repo = materialize_case(case.base, tmp_path / case.base.case_id)
    history = build_history(case, repo)
    policy = PruningOnlyPolicy(threshold=0.5, keep_recent_tokens=700)
    context = SimpleNamespace(
        history=history,
        token_budget=TokenBudget(total=4_000),
        system_content="system",
        repo_map_content="",
        tool_schemas=(),
    )

    result = policy(context)

    assert result is not None and result.history_override is not None
    text = "\n".join(message.content for message in result.history_override)
    assert "[Pruned old tool output]" in text
    assert case.base.prompt in text
    assert policy.pruned_units >= 1


def test_b2_aggregate_counts_semantic_cost_and_separates_verifier_from_completion():
    rows = [
        {
            "variant": "baseline",
            "passed": True,
            "false_finish": False,
            "agent_status": "success",
            "verifier_status": "passed",
            "agent_tokens": 100,
            "semantic_tokens": 0,
            "total_tokens_with_context": 100,
            "latency_seconds": 2.0,
            "tool_calls": 2,
            "context_checkpoints": 0,
            "semantic_calls": 0,
            "semantic_error_count": 0,
            "max_pressure_ratio": None,
        },
        {
            "variant": "pruning_only",
            "passed": False,
            "false_finish": False,
            "agent_status": "max_steps",
            "termination_reason": None,
            "resource_reason": None,
            "verifier_status": "passed",
            "agent_tokens": 80,
            "semantic_tokens": 0,
            "total_tokens_with_context": 80,
            "latency_seconds": 4.0,
            "tool_calls": 3,
            "context_checkpoints": 0,
            "semantic_calls": 0,
            "semantic_error_count": 0,
            "max_pressure_ratio": 1.1,
        },
        {
            "variant": "hybrid_compaction",
            "passed": True,
            "false_finish": False,
            "agent_status": "success",
            "verifier_status": "passed",
            "agent_tokens": 70,
            "semantic_tokens": 20,
            "total_tokens_with_context": 90,
            "latency_seconds": 3.0,
            "tool_calls": 2,
            "context_checkpoints": 1,
            "semantic_calls": 1,
            "semantic_error_count": 0,
            "max_pressure_ratio": 1.2,
        },
    ]

    report = _aggregate(rows)

    assert report["baseline"]["tokens_per_solved"] == 100
    assert report["hybrid_compaction"]["tokens_per_solved"] == 90
    assert report["hybrid_compaction"]["mean_semantic_tokens"] == 20
    assert report["hybrid_compaction"]["context_trigger_rate"] == 1.0
    assert report["hybrid_compaction"]["mean_max_pressure_ratio"] == 1.2
    assert report["baseline"]["verifier_pass_rate"] == 1.0
    assert report["baseline"]["max_steps_exhausted_rate"] == 0.0
    assert report["pruning_only"]["pass_at_1"] == 0.0
    assert report["pruning_only"]["verifier_pass_rate"] == 1.0
    assert report["pruning_only"]["max_steps_exhausted_rate"] == 1.0


def test_b2_aggregate_accepts_v2_rows_and_counts_v3_incomplete():
    rows = [{
        "variant": "baseline", "passed": False, "false_finish": False,
        "agent_status": "incomplete", "termination_reason": "resource_exhausted",
        "resource_reason": "max_steps", "verifier_status": "passed",
        "agent_tokens": 10, "semantic_tokens": 0, "total_tokens_with_context": 10,
        "latency_seconds": 1.0, "tool_calls": 0, "context_checkpoints": 0,
        "semantic_calls": 0, "semantic_error_count": 0, "max_pressure_ratio": None,
        "completion_rejections": 1,
        "completion_rejection_reasons": {"REQUIRED_TEST_MISSING": 1},
    }]
    report = _aggregate(rows)["baseline"]
    assert report["max_steps_exhausted_rate"] == 1.0
    assert report["completion_rejection_reasons"] == {"REQUIRED_TEST_MISSING": 1}
