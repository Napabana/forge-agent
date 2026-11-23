"""
agent/task.py

整个项目的数据地基。定义 agent 运行过程中所有核心概念：
Task / ToolCall / Action / Observation / Event / RunResult

设计原则：
- 全部使用 dataclass，类型安全，IDE 友好
- 每个类都可以 asdict() 序列化为 JSON，供 EventLog 使用
- 枚举类型用 str enum，序列化后人类可读
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from llm.usage import SessionUsage

if TYPE_CHECKING:
    from runtime.worktree import WorktreeArtifact


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    """Event log 中的事件类型，str enum 序列化后直接是字符串。"""
    TASK_START      = "task_start"
    ACTION          = "action"
    OBSERVATION     = "observation"
    REFLECTION      = "reflection"
    LOOP_DETECTED    = "loop_detected"
    TASK_COMPLETE   = "task_complete"
    TASK_FAILED     = "task_failed"
    # 隔离工作树、权限决策和任务认领事件
    TASK_CLAIMED      = "task_claimed"
    WORKTREE_CREATED  = "worktree_created"
    WORKTREE_RETAINED = "worktree_retained"
    WORKTREE_REMOVED  = "worktree_removed"
    PERMISSION_DECISION = "permission_decision"
    PREPARE_NEXT_TURN_STARTED = "prepare_next_turn_started"
    PREPARE_NEXT_TURN_FINISHED = "prepare_next_turn_finished"
    PREPARE_NEXT_TURN_FAILED = "prepare_next_turn_failed"
    LLM_CALL_STARTED = "llm_call_started"
    LLM_CALL_RETRY = "llm_call_retry"
    LLM_CALL_FINISHED = "llm_call_finished"
    LLM_CALL_FAILED = "llm_call_failed"
    MODEL_PROTOCOL_ERROR = "model_protocol_error"
    TOOL_EXECUTION_STARTED = "tool_execution_started"
    TOOL_INVOCATION_STARTED = "tool_invocation_started"
    REPOSITORY_EFFECT = "repository_effect"
    EXECUTION_CONSTRAINTS = "execution_constraints"
    TOOL_EXECUTION_FINISHED = "tool_execution_finished"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    CONTEXT_COMPACTION_STARTED = "context_compaction_started"
    CONTEXT_COMPACTION_FAILED = "context_compaction_failed"
    CONTEXT_COMPACTED = "context_compacted"
    COMPLETION_REJECTED = "completion_rejected"
    COMPLETION_CANDIDATE = "completion_candidate"
    COMPLETION_CHECK = "completion_check"
    PLAN_CREATED = "plan_created"
    PLAN_STEP_STARTED = "plan_step_started"
    PLAN_STEP_COMPLETED = "plan_step_completed"
    PLAN_REVISED = "plan_revised"
    PLANNING_SKIPPED = "planning_skipped"
    PLAN_REJECTED = "plan_rejected"
    FAILURE_CLASSIFIED = "failure_classified"
    RECOVERY_SELECTED = "recovery_selected"
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    RECOVERY_BLOCKED = "recovery_blocked"
    SKILL_DISCOVERED = "skill_discovered"
    SKILL_SELECTED = "skill_selected"
    SKILL_LOADED = "skill_loaded"
    SKILL_REFERENCE_LOADED = "skill_reference_loaded"
    SKILL_REJECTED = "skill_rejected"
    MCP_SERVER_CAPABILITIES = "mcp_server_capabilities"
    MCP_TOOL_DISCOVERED = "mcp_tool_discovered"
    TASK_INCOMPLETE = "task_incomplete"
    # Trace v2 的 Runner / product 生命周期。旧 JSONL 不包含这些事件也可照常读取。
    RUN_TERMINATED = "run_terminated"
    ACCEPTANCE = "acceptance"
    DELIVERY = "delivery"
    WORKERS_SUMMARY = "workers_summary"
    WORKER_DISPATCHED = "worker_dispatched"
    WORKER_STARTED = "worker_started"
    WORKER_WRAPUP_STARTED = "worker_wrapup_started"
    WORKER_PARTIAL_READY = "worker_partial_ready"
    WORKER_FINISHED = "worker_finished"
    WORKER_RESULT_DELIVERED = "worker_result_delivered"
    WORKER_RESULT_READ = "worker_result_read"
    RUN_CHECKPOINT_COMMITTED = "run_checkpoint_committed"
    TOOL_INTENT_COMMITTED = "tool_intent_committed"
    TOOL_OPERATION_STARTED = "tool_operation_started"
    TOOL_RESULT_COMMITTED = "tool_result_committed"
    TOOL_OUTCOME_UNKNOWN = "tool_outcome_unknown"
    CACHE_AUDIT_RECORDED = "cache_audit_recorded"
    MEMORY_RETRIEVAL_STARTED = "memory_retrieval_started"
    MEMORY_RETRIEVAL_FINISHED = "memory_retrieval_finished"
    MEMORY_RETRIEVAL_FAILED = "memory_retrieval_failed"
    MEMORY_CANDIDATE_CREATED = "memory_candidate_created"
    MEMORY_CONSOLIDATION_STARTED = "memory_consolidation_started"
    MEMORY_CONSOLIDATION_FINISHED = "memory_consolidation_finished"
    MEMORY_CONSOLIDATION_FAILED = "memory_consolidation_failed"


class ActionType(str, Enum):
    """Agent 可以执行的 action 类型。"""
    TOOL_CALL   = "tool_call"    # 调用某个工具
    REFLECTION  = "reflection"   # 触发自我反思
    FINISH      = "finish"       # 宣布任务完成
    GIVE_UP     = "give_up"      # 超出能力范围，主动放弃
    PROTOCOL_ERROR = "protocol_error"  # 模型输出了损坏的工具调用


class ObservationStatus(str, Enum):
    """工具执行结果的状态。"""
    SUCCESS = "success"
    ERROR   = "error"
    TIMEOUT = "timeout"


TestOutcome = Literal["passed", "failed", "no_tests"]


class RunStatus(str, Enum):
    """整次 agent 运行的最终状态。"""
    SUCCESS     = "success"
    INCOMPLETE  = "incomplete"  # 未证明完成，因资源或框架策略停止
    FAILED      = "failed"
    MAX_STEPS   = "max_steps"    # 仅兼容读取旧结果；新运行使用 INCOMPLETE
    GAVE_UP     = "gave_up"      # agent 主动放弃
    CANCELED    = "canceled"     # 外部请求取消


def infer_completion_requirements(description: str) -> tuple[bool, bool]:
    """Infer whether a coding task must change files and run tests."""
    text = description.strip().lower()
    read_only = any(marker in text for marker in (
        "只读取", "只读", "不修改", "不要修改",
        "read-only", "read only", "do not modify", "without modifying",
    ))
    skip_tests = any(marker in text for marker in (
        "不运行测试", "无需测试", "不要测试",
        "do not run tests", "without tests", "no tests required",
    ))
    change_intent = bool(
        re.search(r"\b(fix|change|modify|implement|add|remove|refactor|update)\b", text)
        or any(marker in text for marker in (
            "修复", "修改", "实现", "新增", "添加", "删除", "重构", "更新",
        ))
    )
    test_intent = bool(
        re.search(r"\b(pytest|tests?|verify|verification)\b", text)
        or "测试" in text
        or "验证" in text
    )
    return (change_intent and not read_only, test_intent and not skip_tests)


def explicitly_requests_tests(description: str) -> bool:
    """Recognize commands to run tests; merely mentioning tests is not a command."""
    text = description.strip().lower()
    if any(marker in text for marker in ("不运行测试", "无需测试", "不要测试", "do not run tests", "without tests")):
        return False
    return bool(re.search(r"\b(run|execute)\s+(the\s+)?(tests?|pytest)\b", text)
                or re.search(r"(运行|执行|跑|请跑|必须跑).{0,8}测试", text))
# ---------------------------------------------------------------------------
# Task — 输入
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """
    一次 agent 运行的输入描述。
    由 entry 层（CLI / GitHub Issue）构造，传给 Agent.run()。
    """
    # 必填
    description: str            # 任务描述，自然语言
    repo_path: str              # 目标 repo 的本地路径

    # 可选
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    issue_url: str | None = None        # GitHub issue URL，自动修复模式时填入
    parent_task_id: str | None = None
    agent_id: str | None = None
    role: str | None = None
    test_cmd: str | None = None         # 运行测试的命令，如 "pytest tests/"
    require_changes: bool = False       # completion requires a real repository change
    require_tests: bool = False         # completion requires a successful test run
    inferred_require_changes: bool = False
    inferred_require_tests: bool = False
    max_steps: int = 40                 # 最大循环步数，超出则熔断
    budget_tokens: int = 80_000         # 整次运行的 token 预算

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        return f"Task(id={self.task_id!r}, desc={self.description[:60]!r})"


# ---------------------------------------------------------------------------
# ToolCall — Action 的载体
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """
    agent 决定调用某个工具时的具体参数。
    嵌套在 Action 里，不单独写入 EventLog。
    """
    name: str                   # 工具名称，如 "shell", "file_read"
    params: dict[str, Any]      # 工具参数，由各 Tool 自己定义 schema

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Action — agent 的决策输出
# ---------------------------------------------------------------------------

@dataclass
class Action:
    """
    LLM 每轮输出的决策。
    由 LLMBackend 解析返回，Agent Core 拿到后执行。
    """
    action_type: ActionType
    thought: str                            # LLM 的推理过程，必须保留，调试关键
    tool_call: ToolCall | None = None       # action_type == TOOL_CALL 时非空
    message: str | None = None             # action_type == FINISH / GIVE_UP 时的说明

    def to_dict(self) -> dict[str, Any]:
        d = {
            "action_type": self.action_type.value,
            "thought": self.thought,
            "message": self.message,
            "tool_call": self.tool_call.to_dict() if self.tool_call else None,
        }
        return d

    def is_terminal(self) -> bool:
        """是否是终止 action（不再需要执行工具）。"""
        return self.action_type in (ActionType.FINISH, ActionType.GIVE_UP)

    def __repr__(self) -> str:
        if self.tool_call:
            return f"Action({self.action_type.value}, tool={self.tool_call.name})"
        return f"Action({self.action_type.value})"


# ---------------------------------------------------------------------------
# Observation — 工具执行结果
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    """
    工具执行后返回给 agent 的结果。
    output 字段会被注入下一轮 LLM 的上下文。
    """
    status: ObservationStatus
    output: str                         # 工具输出，已截断到安全长度
    tool_name: str                      # 来自哪个工具
    tokens_used: int = 0                # 这条 observation 消耗的 token 数（估算）
    error: str | None = None            # status == ERROR 时的错误信息
    error_type: str | None = None
    test_outcome: TestOutcome | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.test_outcome is None:
            del data["test_outcome"]
        return data

    def is_success(self) -> bool:
        return self.status == ObservationStatus.SUCCESS

    def __repr__(self) -> str:
        return (
            f"Observation(tool={self.tool_name}, "
            f"status={self.status.value}, "
            f"len={len(self.output)})"
        )


# ---------------------------------------------------------------------------
# Event — 写入 EventLog 的统一单元
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """
    EventLog 中的一条记录。
    所有信息统一封装在这里，append-only 写入 JSONL 文件。

    payload 的内容取决于 event_type：
    - TASK_START:    {"task": Task.to_dict()}
    - ACTION:        {"step": int, "action": Action.to_dict()}
    - OBSERVATION:   {"step": int, "observation": Observation.to_dict()}
    - REFLECTION:    {"step": int, "reason": str, "prompt": str}
    - TASK_COMPLETE: {"steps": int, "summary": str}
    - TASK_FAILED:   {"steps": int, "reason": str}
    - TASK_CLAIMED:      {"task_id": str, "owner": str}
    - WORKTREE_CREATED:  {"name": str, "path": str, "base": str}
    - WORKTREE_RETAINED: {"branch": str, "path": str, "reason": str,
                          "changed_files": list[str]}
    - WORKTREE_REMOVED:  {"name": str, "path": str, "reason": str}
        reason ∈ {"normal", "exception", "preflight_failed"}
    - PERMISSION_DECISION: {"tool": str, "decision": str, "reason": str, "params": dict}
        decision ∈ {"allow","deny","confirm"}
    - RUN_TERMINATED / ACCEPTANCE / DELIVERY: Trace v2 Runner/product lifecycle.
    """
    event_type: EventType
    task_id: str
    payload: dict[str, Any]
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id":   self.event_id,
            "event_type": self.event_type.value,
            "task_id":    self.task_id,
            "timestamp":  self.timestamp,
            "payload":    self.payload,
        }


# ---------------------------------------------------------------------------
# RunResult — 整次运行的最终结果
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """
    Agent.run() 的返回值。
    包含最终状态、patch 内容（如有）、统计信息。
    """
    task_id: str
    status: RunStatus
    summary: str                        # 人类可读的结果摘要
    steps_taken: int
    total_tokens: int = 0
    usage: SessionUsage = field(default_factory=SessionUsage)
    patch: str | None = None            # git diff 格式的修改内容
    error: str | None = None            # status == FAILED 时的原因
    worktree: "WorktreeArtifact | None" = None
    trace_path: str | None = None
    acceptance_status: str = "not_requested"  # Runner 独立验收状态，与 Agent 终态分开
    acceptance_error: str | None = None         # 独立验收失败原因
    delivery_status: str = "not_requested"     # commit/push/PR 交付状态
    termination_reason: str | None = None       # 结构化终止原因
    resource_reason: str | None = None          # 资源终止子原因，例如 max_steps
    usage_complete: bool | None = None          # None=历史未知；False=估算或缺失调用用量
    completion_warnings: tuple[str, ...] = ()
    completion_reports: tuple[str, ...] = ()
    worker_usage: SessionUsage = field(default_factory=SessionUsage)
    run_id: str | None = None

    def __post_init__(self) -> None:
        if self.usage.total_tokens:
            self.total_tokens = self.usage.total_tokens

    def is_success(self) -> bool:
        return self.status == RunStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunResult":
        from runtime.worktree import WorktreeArtifact, WorktreeDisposition

        fields = dict(data)
        fields["status"] = RunStatus(fields["status"])
        for key in ("usage", "worker_usage"):
            fields[key] = SessionUsage.from_dict(fields.get(key))
        for key in ("completion_warnings", "completion_reports"):
            fields[key] = tuple(fields.get(key, ()))
        if fields.get("worktree"):
            worktree = fields["worktree"]
            fields["worktree"] = WorktreeArtifact(
                **{**worktree, "disposition": WorktreeDisposition(worktree["disposition"]),
                   "changed_files": tuple(worktree["changed_files"])},
            )
        return cls(**fields)

    def __repr__(self) -> str:
        return (
            f"RunResult(status={self.status.value}, "
            f"steps={self.steps_taken}, "
            f"tokens={self.total_tokens})"
        )
