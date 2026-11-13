"""
tools/test_tool.py

pytest 执行工具，返回结构化的测试结果。

关键设计：
- 不只返回原始 stdout，而是解析出"哪些测试失败了 + 错误信息"
- 失败时 output 包含精简的 failure summary，避免把整个 traceback 塞进上下文
- 通过 exit code 判断成功/失败，不依赖字符串匹配
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolEffect, ToolErrorType, ToolResult
from tools.runtime import LocalRuntime, Runtime


PYTEST_TIMEOUT = 120        # pytest 默认超时，比 shell 工具更长
MAX_OUTPUT_CHARS = 6_000    # 测试输出比普通 shell 输出更容易很长


def _classify_pytest_failure(returncode: int, raw: str) -> tuple[ToolErrorType, str]:
    """区分真实测试失败与 pytest/解释器基础设施故障。"""
    lowered = raw.lower()
    if returncode < 0:
        return ToolErrorType.INFRASTRUCTURE, "pytest process could not be started"
    if "no module named pytest" in lowered:
        return ToolErrorType.INFRASTRUCTURE, "pytest is not installed in the configured test Python"
    # pytest: 1=tests failed，5=no tests collected；其余标准退出码表示执行环境/调用损坏。
    if returncode in {2, 3, 4}:
        return ToolErrorType.INFRASTRUCTURE, f"pytest runtime failed with exit code {returncode}"
    return ToolErrorType.TOOL_EXECUTION, f"pytest exited with code {returncode}"


class PytestTool(BaseTool):
    """
    运行 pytest 并返回结构化结果。

    params:
        path (str):  测试文件或目录（默认 "tests/"，不存在则用 "."）
        args (str):  额外的 pytest 参数（如 "-x -v --tb=short"）
        cwd (str):   工作目录（默认当前目录）
    """

    def __init__(
        self,
        runtime: Runtime | None = None,
        default_cwd: str | None = None,
        python_executable: str | None = None,
    ) -> None:
        self._runtime = runtime or LocalRuntime()
        # LLM 未传 cwd 时的默认目录（orchestrator 注入 worktree 路径，M4 第二波）。
        self._default_cwd = default_cwd
        self._python_executable = python_executable

    @property
    def effect(self) -> ToolEffect:
        return ToolEffect.READ_ONLY

    @property
    def name(self) -> str:
        return "test"

    @property
    def description(self) -> str:
        return (
            "Run pytest and return a structured summary of results. "
            "Shows which tests failed and their error messages. "
            "Use path to run specific test files or directories."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Test file or directory to run (default: 'tests/' or '.')",
                },
                "args": {
                    "type": "string",
                    "description": "Extra pytest arguments (e.g. '-x -v --tb=short')",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory",
                },
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        cwd = params.get("cwd", None) or self._default_cwd
        cwd_path = Path(cwd) if cwd else Path.cwd()

        test_path = params.get("path", "")
        if not test_path:
            test_path = "tests/" if (cwd_path / "tests").exists() else "."
        if not isinstance(test_path, str) or not test_path.strip():
            return ToolResult(
                success=False,
                output="",
                error="pytest path must be a non-empty string",
                error_type=ToolErrorType.INVALID_ARGUMENTS,
            )
        test_path = test_path.strip()
        if test_path.startswith("-"):
            return ToolResult(
                success=False,
                output="",
                error="pytest path must not start with '-' (option-like paths are rejected)",
                error_type=ToolErrorType.INVALID_ARGUMENTS,
            )

        extra_args = params.get("args", "")
        if not isinstance(extra_args, str):
            return ToolResult(
                success=False,
                output="",
                error="pytest args must be a string",
                error_type=ToolErrorType.INVALID_ARGUMENTS,
            )
        try:
            parsed_extra_args = shlex.split(extra_args)
        except ValueError as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"invalid pytest args: {exc}",
                error_type=ToolErrorType.INVALID_ARGUMENTS,
            )

        python_bin = (
            self._python_executable
            or getattr(self._runtime, "python_executable", "python3")
        )
        if not isinstance(python_bin, str) or not python_bin.strip():
            return ToolResult(
                success=False,
                output="",
                error="pytest Python executable is not configured",
                error_type=ToolErrorType.INVALID_ARGUMENTS,
            )
        cmd_parts = [
            python_bin,
            "-m",
            "pytest",
            *parsed_extra_args,
            "--tb=short",
            "--no-header",
            "-q",
            "--",
            test_path,
        ]
        exec_args = getattr(self._runtime, "exec_args", None)
        if callable(exec_args):
            run_result = exec_args(
                cmd_parts,
                cwd=cwd,
                timeout=PYTEST_TIMEOUT,
            )
        else:
            # Backward compatibility for duck-typed Runtime adapters.
            run_result = self._runtime.exec(
                shlex.join(cmd_parts),
                cwd=cwd,
                timeout=PYTEST_TIMEOUT,
            )
        if "timed out" in run_result.stderr.lower():
            return ToolResult(
                success=False,
                output="",
                error=f"pytest timed out after {PYTEST_TIMEOUT}s",
                error_type=ToolErrorType.TIMEOUT,
            )
        raw = run_result.output
        success = run_result.returncode == 0

        output = _format_pytest_output(raw, success)

        error_type, error = _classify_pytest_failure(run_result.returncode, raw)
        return ToolResult(
            success=success,
            output=output,
            error=None if success else error,
            error_type=None if success else error_type,
        )


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------

def _format_pytest_output(raw: str, success: bool) -> str:
    """
    把 pytest 原始输出格式化为 agent 友好的摘要。

    成功时：返回通过统计行（如 "5 passed in 0.12s"）
    失败时：提取 FAILED 测试列表 + 每个失败的 short traceback
    """
    if len(raw) > MAX_OUTPUT_CHARS:
        # 失败时 agent 最需要看尾部（错误摘要），头部（收集信息）不重要
        raw = "...[output truncated]...\n" + raw[-MAX_OUTPUT_CHARS:]

    if success:
        # 只返回最后的统计行
        lines = raw.strip().splitlines()
        summary_lines = [l for l in lines if re.search(r"passed|no tests", l)]
        if summary_lines:
            return summary_lines[-1]
        return raw.strip()

    # 失败时：提取 FAILED 列表
    failed_lines = [l for l in raw.splitlines() if l.startswith("FAILED")]
    failed_section = "\n".join(failed_lines) if failed_lines else ""

    # 提取 short test summary info 之后的内容（pytest -q 会输出这块）
    short_summary_match = re.search(
        r"=+ short test summary info =+(.*?)(?:=+|\Z)",
        raw,
        re.DOTALL,
    )
    short_summary = short_summary_match.group(1).strip() if short_summary_match else ""

    # 最终统计行（如 "2 failed, 3 passed in 0.45s"）
    stat_match = re.search(r"\d+ (failed|error).*in \d+\.\d+s", raw)
    stat_line = stat_match.group(0) if stat_match else ""

    parts = []
    if failed_section:
        parts.append(f"Failed tests:\n{failed_section}")
    if short_summary and short_summary != failed_section:
        parts.append(f"Summary:\n{short_summary}")
    if stat_line:
        parts.append(stat_line)

    return "\n\n".join(parts) if parts else raw.strip()
