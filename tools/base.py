"""
tools/base.py

工具层基础设施：
- ToolResult     工具执行结果
- BaseTool       所有工具的抽象基类
- ToolRegistry   工具注册表，core.py 通过它执行工具、生成 schema

新增工具只需：
    1. 继承 BaseTool，实现 execute() 和 schema 属性
    2. 调用 registry.register(MyTool())
    不需要改任何其他代码。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

from agent.task import Observation, ObservationStatus
from llm.base import LLMToolSchema


class ToolEffect(str, Enum):
    """Repository effect classification used by Structured Planning gates."""
    READ_ONLY = "read_only"
    MAY_MUTATE_REPOSITORY = "may_mutate_repository"


class ToolErrorType(str, Enum):
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    TOOL_EXECUTION = "tool_execution"
    REMOTE_CAPABILITY = "remote_capability"
    INFRASTRUCTURE = "infrastructure"
    HOOK_BLOCKED = "hook_blocked"
    HOOK_FAILED = "hook_failed"


_JSON_TYPES = {
    "string": str,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    expected_type = _JSON_TYPES.get(expected)
    return expected_type is None or isinstance(value, expected_type)


def _validate_tool_params(params: Any, schema: dict[str, Any]) -> str | None:
    if not isinstance(params, dict):
        return "Tool arguments must be an object"

    missing = [name for name in schema.get("required", []) if name not in params]
    if missing:
        return f"Missing required argument(s): {', '.join(missing)}"

    properties = schema.get("properties", {})
    for name, value in params.items():
        expected = properties.get(name, {}).get("type")
        if isinstance(expected, str) and not _matches_json_type(value, expected):
            return f"Argument '{name}' must be {expected}"

    return None


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """
    工具执行的原始结果，由各 Tool.execute() 返回。
    core.py 把它转换为 Observation 后写入 EventLog。
    """
    success: bool
    output: str                         # 工具的文本输出，已做截断处理
    error: str | None = None            # 失败时的错误信息
    error_type: ToolErrorType | None = None
    diagnostics: tuple[str, ...] = ()   # 不改变结果的观察阶段诊断

    def to_observation(self, tool_name: str) -> Observation:
        """转换为 Observation；TIMEOUT 保留独立状态，其余失败统一为 ERROR。"""
        if self.success:
            status = ObservationStatus.SUCCESS
        elif self.error_type is ToolErrorType.TIMEOUT:
            status = ObservationStatus.TIMEOUT
        else:
            status = ObservationStatus.ERROR
        return Observation(
            status=status,
            output=self.output,
            tool_name=tool_name,
            error=self.error,
            error_type=self.error_type.value if self.error_type is not None else None,
        )


# ---------------------------------------------------------------------------
# BaseTool
# ---------------------------------------------------------------------------

class BaseTool(ABC):
    """
    所有工具的抽象基类。

    子类必须实现：
    - name:     工具名称（与 LLM function calling 的函数名对应）
    - schema:   JSON Schema 描述，告诉 LLM 这个工具怎么用
    - execute(): 实际执行逻辑
    """

    @property
    def effect(self) -> ToolEffect:
        """Unknown/new tools fail safe as mutation-capable unless explicitly read-only."""
        return ToolEffect.MAY_MUTATE_REPOSITORY

    @property
    def metadata(self) -> dict[str, Any]:
        """Optional non-secret execution metadata for policy and Trace correlation."""
        return {}

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，如 "shell", "file_read"。必须全局唯一。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具功能描述，注入 LLM 的 system prompt 和 tool schema。"""
        ...

    @property
    @abstractmethod
    def parameters_schema(self) -> dict[str, Any]:
        """
        参数的 JSON Schema。示例：
        {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Shell command to run"},
            },
            "required": ["cmd"],
        }
        """
        ...

    @abstractmethod
    def execute(self, params: dict[str, Any]) -> ToolResult:
        """执行工具，返回 ToolResult。不抛异常，错误封装在 ToolResult.error 里。"""
        ...

    def to_llm_schema(self) -> LLMToolSchema:
        """生成供 LLM 使用的 schema，由 ToolRegistry 调用。"""
        return LLMToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.parameters_schema,
        )


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """
    工具注册表。core.py 持有一个 registry 实例，通过它：
    1. 查找工具并执行（execute_tool）
    2. 生成所有工具的 schema 列表注入 LLM（get_schemas）

    线程安全：当前 v1 单线程，不加锁。
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> "ToolRegistry":
        """
        注册一个工具。支持链式调用：
            registry.register(ShellTool()).register(FileTool())
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool
        return self

    def is_mutating(self, name: str, params: Any | None = None) -> bool:
        """Conservative gate: unknown tools may mutate repository state."""
        tool = self._tools.get(name)
        return tool is None or tool.effect is ToolEffect.MAY_MUTATE_REPOSITORY

    def get_tool(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def get_metadata(self, name: str) -> dict[str, Any]:
        tool = self._tools.get(name)
        return dict(tool.metadata) if tool is not None else {}

    def validate_tool_call(self, name: str, params: Any) -> ToolResult | None:
        if name not in self._tools:
            available = ", ".join(self._tools) or "none"
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown tool '{name}'. Available tools: {available}",
                error_type=ToolErrorType.UNKNOWN_TOOL,
            )

        error = _validate_tool_params(params, self._tools[name].parameters_schema)
        if error is not None:
            return ToolResult(
                success=False,
                output="",
                error=error,
                error_type=ToolErrorType.INVALID_ARGUMENTS,
            )

        return None

    def execute_tool(self, name: str, params: dict[str, Any]) -> ToolResult:
        invalid = self.validate_tool_call(name, params)
        if invalid is not None:
            return invalid

        try:
            return self._tools[name].execute(params)
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Tool '{name}' raised an unexpected error: {exc}",
                error_type=ToolErrorType.TOOL_EXECUTION,
            )

    def get_schemas(self) -> list[LLMToolSchema]:
        """返回所有已注册工具的 schema，供注入 LLM。"""
        return [tool.to_llm_schema() for tool in self._tools.values()]

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"ToolRegistry(tools={self.tool_names})"


# ---------------------------------------------------------------------------
# NoopTool — 测试辅助
# ---------------------------------------------------------------------------

class NoopTool(BaseTool):
    """
    测试专用工具，execute() 直接返回成功，不做任何实际操作。
    用于在不依赖真实文件系统/shell 的情况下测试 core.py 流程。
    """

    def __init__(self, tool_name: str = "noop", output: str = "ok") -> None:
        self._name = tool_name
        self._output = output
        self.call_count = 0
        self.last_params: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"No-op tool '{self._name}' for testing."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Anything"},
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        self.call_count += 1
        self.last_params = params
        return ToolResult(success=True, output=self._output)


class FailingTool(BaseTool):
    """
    测试专用工具，execute() 始终返回失败。
    用于测试 Reflection 触发（测试失败路径）。
    """

    def __init__(self, tool_name: str = "test", error_msg: str = "AssertionError: 1 != 2") -> None:
        self._name = tool_name
        self._error_msg = error_msg
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Always-failing tool '{self._name}' for testing."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, params: dict[str, Any]) -> ToolResult:
        self.call_count += 1
        return ToolResult(
            success=False,
            output=self._error_msg,
            error=self._error_msg,
        )
