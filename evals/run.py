"""通过共享 ExecutionRunner 执行固定任务，并保存独立验收证据。"""

from __future__ import annotations

import difflib
import json
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from agent.runner import ExecutionRunner, RunRequest
from agent.task import Task
from evals.harness import EvalCase, EvalResult, materialize_case, result_to_dict, verify_case


RunnerFactory = Callable[[EvalCase, Path], ExecutionRunner]
_IGNORED_PATCH_DIRS = {".git", ".pytest_cache", "__pycache__", "logs"}


class EvalRunner:
    """逐个重置 fixture，经 Agent 执行后在 History 外运行隐藏 verifier。"""

    def __init__(self, runner_factory: RunnerFactory, output_dir: str | Path, variant: str = "default") -> None:
        self._runner_factory = runner_factory
        self._output_dir = Path(output_dir).resolve()
        self._variant = variant

    @property
    def results_path(self) -> Path:
        return self._output_dir / "runs.jsonl"

    def run(self, cases: Iterable[EvalCase]) -> list[EvalResult]:
        """运行全部任务，并在每个任务结束后立即追加一条原始证据。"""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        results: list[EvalResult] = []
        with self.results_path.open("w", encoding="utf-8") as stream:
            for case in cases:
                result = self._run_case(case)
                results.append(result)
                stream.write(json.dumps(result_to_dict(result), ensure_ascii=False) + "\n")
                stream.flush()
        return results

    def _run_case(self, case: EvalCase) -> EvalResult:
        repo = materialize_case(case, self._output_dir / "repos" / case.case_id)
        task = Task(case.prompt, str(repo), task_id=case.case_id, require_changes=True)
        started = time.perf_counter()
        agent_result = self._runner_factory(case, repo).run(RunRequest(task))

        # verifier 只在 Agent 完全返回后执行，绝不进入模型可见 History。
        verification = verify_case(case, repo)
        verifier_passed = verification.returncode == 0
        verifier_status = "passed" if verifier_passed else "failed"
        return EvalResult(
            case_id=case.case_id, variant=self._variant,
            passed=agent_result.is_success() and verifier_passed,
            false_finish=agent_result.is_success() and not verifier_passed,
            tokens=agent_result.total_tokens, cost_usd=0.0,
            latency_seconds=time.perf_counter() - started,
            tool_calls=_count_tool_calls(agent_result.trace_path),
            task_prompt=case.prompt, trace_path=agent_result.trace_path,
            patch=_fixture_patch(case, repo), agent_status=agent_result.status.value,
            verifier_status=verifier_status,
        )


def _count_tool_calls(trace_path: str | None) -> int:
    """从原始 Trace 统计真实开始执行的工具调用。"""
    if not trace_path:
        return 0
    events = (json.loads(line) for line in Path(trace_path).read_text(encoding="utf-8").splitlines() if line.strip())
    return sum(event.get("event_type") == "tool_execution_started" for event in events)


def _fixture_patch(case: EvalCase, repo: Path) -> str:
    """用标准库生成 fixture 初始文本到最终文本的统一 diff。"""
    after: dict[str, str] = {}
    for path in repo.rglob("*"):
        if path.is_file() and not any(part in _IGNORED_PATCH_DIRS for part in path.relative_to(repo).parts):
            try:
                after[path.relative_to(repo).as_posix()] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # 当前 fixture 为文本；未来出现二进制任务时再扩展证据格式。

    chunks: list[str] = []
    for relative in sorted(set(case.files) | set(after)):
        before_text, after_text = case.files.get(relative, ""), after.get(relative, "")
        if before_text != after_text:
            chunks.extend(difflib.unified_diff(
                before_text.splitlines(keepends=True), after_text.splitlines(keepends=True),
                fromfile=f"a/{relative}" if relative in case.files else "/dev/null",
                tofile=f"b/{relative}" if relative in after else "/dev/null",
            ))
    return "".join(chunks)
