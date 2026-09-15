from __future__ import annotations

import json

from agent.runner import ExecutionRunner
from agent.task import Action, ActionType, ToolCall
from evals.harness import EvalResult, load_cases, materialize_case, summarize, verify_case
from evals.report import build_report
from evals.run import EvalRunner
from llm.base import MockBackend
from tools.base import ToolRegistry
from tools.file_tool import FileWriteTool


def test_fixed_cases_are_resettable_and_use_hidden_verifier(tmp_path):
    cases = load_cases()
    assert len(cases) == 6
    assert {case.category for case in cases} == {
        "single_file_bug", "cross_file_change", "failing_test_diagnosis",
        "hidden_test_feature", "refactor", "false_finish_guard",
    }
    case = cases[0]
    repo = materialize_case(case, tmp_path / "fixture")
    assert verify_case(case, repo).returncode != 0
    (repo / "calc.py").write_text("def divide(left, right):\n    return left / right\n")
    assert verify_case(case, repo).returncode == 0
    materialize_case(case, repo)
    assert "left * right" in (repo / "calc.py").read_text()


def test_ablation_summary_writes_json_and_markdown(tmp_path):
    results = [
        EvalResult("a", "static", True, False, 100, 0.1, 1.0, 2),
        EvalResult("b", "static", False, True, 50, 0.05, 3.0, 1, True),
    ]
    summary = summarize(results)
    assert summary["pass_at_1"] == 0.5
    assert summary["tokens_per_solved"] == 150
    assert summary["p95_latency_seconds"] == 3.0
    source = tmp_path / "runs.jsonl"
    source.write_text("\n".join(json.dumps(result.__dict__) for result in results))
    report = build_report(source, tmp_path / "summary")
    assert report["static"]["false_finish_rate"] == 0.5
    assert (tmp_path / "summary.json").exists()
    assert "| static |" in (tmp_path / "summary.md").read_text()


def test_eval_runner_executes_all_cases_and_keeps_verifier_outside_history(tmp_path):
    cases = load_cases()
    backends = {}

    def runner_factory(case, repo):
        # 只让首个 fixture 成功，其他任务用于证明整批仍会留下失败证据。
        script = ([
            Action(ActionType.TOOL_CALL, "修复除法", ToolCall("file_write", {"path": "calc.py", "content": "def divide(left, right):\n    return left / right\n"})),
            Action(ActionType.FINISH, "完成", message="完成"),
        ] if case.case_id == "single-file-divide" else [Action(ActionType.FINISH, "完成", message="完成")])
        backend = MockBackend(script)
        backends[case.case_id] = backend
        return ExecutionRunner(
            backend=backend, registry=ToolRegistry().register(FileWriteTool(workspace=repo)),
            log_dir=str(tmp_path / "evidence" / "traces" / case.case_id),
        )

    results = EvalRunner(runner_factory, tmp_path / "evidence", variant="mock").run(cases)

    assert len(results) == 6
    assert results[0].passed and results[0].verifier_status == "passed"
    assert "a/calc.py" in results[0].patch and "return left / right" in results[0].patch
    assert results[0].trace_path and results[0].tool_calls == 1
    verifier = cases[0].verifier
    assert all(verifier not in message.content for turn in backends[cases[0].case_id].received_messages for message in turn)
    rows = [json.loads(line) for line in (tmp_path / "evidence" / "runs.jsonl").read_text().splitlines()]
    assert len(rows) == 6 and {"task_prompt", "trace_path", "patch", "agent_status", "verifier_status"} <= rows[0].keys()
