"""
agent/core.py

ReAct 主循环。整个 agent 的大脑。

职责（只做这些，不做别的）：
- 维护对话历史，每轮组装 messages 调用 LLM
- 拿到 Action 后调用 ToolExecutor 执行
- 把 Action + Observation 写入 EventLog
- 检测三种终止/Reflection 触发条件
- 返回 RunResult

不负责：
- 任何 LLM 细节（交给 LLMBackend）
- 任何工具实现（交给 Tool）
- 上下文压缩（由 context/ 模块负责）
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent.event_log import EventLog
from agent.loop_detector import LoopDetector, LoopSeverity
from agent.planning import PlanningMode, PlanningRuntime, decide_planning
from context.history import ConversationHistory
from context.incremental_repo_map import PersistentRepoMap
from context.repo_map import RepoMap
from context.repository_state import repository_fingerprint
from context.token_budget import (
    TokenBudget,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    estimate_tool_schemas_tokens,
)
from agent.prompt import (
    build_system_prompt,
    build_task_prompt,
    completion_rejected,
    reflection_loop_detected,
    reflection_no_edit,
    reflection_test_failed,
    step_budget_warning,
)
from agent.task import (
    Action, ActionType, Event, EventType,
    Observation, ObservationStatus, RunResult, RunStatus, Task, ToolCall,
)
from harness.executor import ToolExecutionCanceled, ToolExecutorInfrastructureError
from llm.base import LLMBackend, LLMMessage, LLMToolSchema
from llm.errors import LLMCallbackError, LLMErrorInfo, classify_llm_error
from llm.usage import SessionUsage
from tools.base import ToolErrorType, ToolRegistry

if TYPE_CHECKING:
    from harness.hooks import Hooks

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrepareNextTurnContext:
    """下一次 LLM 调用前可观察的运行上下文。"""

    task: Task
    step: int
    history: ConversationHistory
    repo_map: RepoMap
    token_budget: TokenBudget
    cancel_event: object | None
    event_log: EventLog
    # 以下三项由 Core 只读提供，让 context/ 能按完整下一请求评估压力。
    system_content: str = ""
    repo_map_content: str = ""
    tool_schemas: tuple[LLMToolSchema, ...] = ()


@dataclass(frozen=True)
class PrepareNextTurnResult:
    """prepare_next_turn 的薄结果：持久消息、Repo Map 刷新和一次性模型视图。"""

    messages: tuple[LLMMessage, ...] = ()
    refresh_repo_map: bool = False
    history_override: tuple[LLMMessage, ...] | None = None


PrepareNextTurn = Callable[[PrepareNextTurnContext], PrepareNextTurnResult | None]


@dataclass
class AgentConfig:
    """Agent 运行时配置，从 config/default.yaml 加载后传入。"""
    max_steps: int = 40
    reflection_no_edit_steps: int = 6
    loop_detection_window: int = 3
    loop_detection_max_period: int = 3
    test_tool_names: tuple[str, ...] = ("test", "pytest")
    fatal_tool_error_repeats: int = 2
    budget_tokens: int = 80_000
    history_max_messages: int = 40
    planning_mode: str = "off"
    llm_max_retries: int = 3
    llm_retry_delay: float = 2.0
    llm_retry_max_delay: float = 30.0
    llm_retry_jitter: float = 0.0
    stream: bool = False
    stream_callback: object = None
    thought_callback: object = None
    confirm_dangerous: bool = False
    confirm_callback: object = None
    cancel_event: object = None
    hooks: Hooks | None = None
    prepare_next_turn: PrepareNextTurn | None = None
    # 仅控制模型可见的执行工作区；宿主 repo_path 仍用于文件、Git 与 repository state。
    execution_workspace: str | None = None
    # Production defaults to persistent incremental Repo Map. Other modes are
    # retained as controlled ablation variants.
    repo_map_mode: str = "incremental"
    repo_map_cache_dir: str | None = None


class Agent:
    """同步 ReAct 主循环实现。"""

    def __init__(
        self,
        backend: LLMBackend,
        registry: ToolRegistry,
        config: AgentConfig | None = None,
        executor: "ToolExecutor | None" = None,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._cfg = config or AgentConfig()
        # 未注入 executor 时仍使用透明 ToolExecutor；生产 Runner 会注入 permission 版本。
        self._executor = executor or _default_executor(registry, self._cfg.hooks)
        self._repo_map_cache_key: str | None = None
        self._repo_map_force_refresh = False
        self._repo_map_sync_requested = False
        self._repo_map_build_seconds = 0.0
        self._repo_map_build_calls = 0
        self._prepared_history_override: tuple[LLMMessage, ...] | None = None
        self._planning_runtime: PlanningRuntime | None = None
        self._planning_context_cache = ""

    def reset_run_runtime_state(self) -> None:
        """Clear transient per-run state before a Runner shared-history preflight."""
        self._planning_runtime = None
        self._planning_context_cache = ""

    def invalidate_repo_map_cache(self, repo_path: str | Path | None = None) -> bool:
        """Invalidate the cached repository summary, optionally by repo."""
        if repo_path is not None and self._repo_map_cache_key is not None:
            requested = str(Path(repo_path).resolve())
            current = str(Path(self._repo_map_cache_key).resolve())
            if requested != current:
                return False
        existed = hasattr(self, "_repo_map_cache")
        if existed:
            del self._repo_map_cache
        if self._repo_map_mode() == "incremental":
            self._repo_map_sync_requested = True
            self._repo_map_force_refresh = False
        else:
            self._repo_map_force_refresh = True
        return existed

    def _repo_map_mode(self) -> str:
        mode = (self._cfg.repo_map_mode or "incremental").strip().lower()
        if mode not in {"none", "static", "query_aware", "incremental"}:
            raise ValueError(f"Unsupported repo_map_mode: {self._cfg.repo_map_mode!r}")
        return mode

    def _new_repo_map(self, repo_path: str | Path) -> RepoMap:
        if self._repo_map_mode() == "incremental":
            return PersistentRepoMap(repo_path, cache_dir=self._cfg.repo_map_cache_dir)
        return RepoMap(repo_path)

    @property
    def current_plan(self):
        """Expose current typed plan for diagnostics; completion never depends on it."""
        return self._planning_runtime.current_plan if self._planning_runtime else None

    @property
    def repo_map_telemetry(self) -> dict[str, object]:
        index_metrics = getattr(getattr(self, "_repo_map_instance", None), "metrics", None)
        return {
            "mode": self._repo_map_mode(),
            "build_seconds": self._repo_map_build_seconds,
            "build_calls": self._repo_map_build_calls,
            "index": index_metrics.snapshot() if index_metrics is not None else None,
        }

    def run(
        self,
        task: Task,
        log: EventLog,
        history: ConversationHistory | None = None,
    ) -> RunResult:
        """执行一次完整的 agent 运行。"""
        self._current_repo_path = task.repo_path
        self._model_repo_path = self._cfg.execution_workspace or task.repo_path
        self._repo_map_query = task.description
        cache_key = task.repo_path
        if self._repo_map_cache_key != cache_key:
            self.invalidate_repo_map_cache()
            self._repo_map_force_refresh = False
            self._repo_map_sync_requested = False
            self._repo_map_cache_key = cache_key
            self._repo_map_instance = self._new_repo_map(task.repo_path)
        elif getattr(self, "_repo_map_cache_query", None) != task.description:
            if hasattr(self, "_repo_map_cache"):
                del self._repo_map_cache

        log.log_task_start(task)
        logger.info("Agent starting task %s", task.task_id)
        planning_decision = decide_planning(task, self._cfg.planning_mode)
        self._planning_runtime = PlanningRuntime(planning_decision)
        self._planning_context_cache = ""
        if planning_decision.mode is PlanningMode.AUTO and not planning_decision.enabled:
            log.log_trace(
                EventType.PLANNING_SKIPPED, 0,
                planning_mode=planning_decision.mode.value,
                reason=planning_decision.reason,
            )

        if history is None:
            history = ConversationHistory(max_messages=self._cfg.history_max_messages)
            history.add(LLMMessage(
                role="user",
                content=build_task_prompt(task.description, self._model_repo_path, task.issue_url),
            ))

        token_budget = TokenBudget(total=self._cfg.budget_tokens)
        repo_map = getattr(self, "_repo_map_instance", RepoMap(task.repo_path))
        usage = SessionUsage()
        total_tokens = 0
        steps_without_edit = 0
        loop_detector = LoopDetector(
            repeats=self._cfg.loop_detection_window,
            max_period=self._cfg.loop_detection_max_period,
        )
        test_attempted = False
        last_test_passed: bool | None = None
        last_successful_test_step: int | None = None
        # Completion Guard 与 loop detector 共用真实 repository fingerprint，
        # 不再把“调用过某个写工具”当成仓库确实发生变化的证据。
        initial_repo_state = self._get_repo_state(task.repo_path)
        last_repo_state = initial_repo_state
        last_repo_change_step: int | None = None
        fatal_error_key: str | None = None
        fatal_error_count = 0

        for step in range(1, task.max_steps + 1):
            if self._is_cancel_requested():
                reason = "Canceled by external request"
                logger.info("Agent task %s canceled before step %d", task.task_id, step)
                log.log_task_failed(steps=step - 1, reason=reason)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.CANCELED,
                    summary=reason,
                    steps_taken=step - 1,
                    total_tokens=total_tokens,
                    usage=usage.snapshot(),
                    termination_reason="canceled",
                )

            if step > 1:
                prepare_result = self._prepare_next_turn(
                    task, step, history, repo_map, token_budget, log, total_tokens, usage
                )
                if prepare_result is not None:
                    return prepare_result

            logger.debug("Step %d/%d", step, task.max_steps)
            messages = self._build_messages(history, token_budget, repo_map)
            remaining_steps = task.max_steps - step + 1
            injected_messages: list[LLMMessage] = []
            if remaining_steps <= 3:
                warning = LLMMessage(role="user", content=step_budget_warning())
                messages.append(warning)
                injected_messages.append(warning)
            tools = self._registry.get_schemas()
            token_breakdown = self._trace_token_breakdown(
                messages, tools, injected_messages=injected_messages,
            )
            llm_started = time.perf_counter()
            llm_span = log.log_trace(
                EventType.LLM_CALL_STARTED,
                step,
                model=self._backend.model_name,
                provider=type(self._backend).__name__,
                message_count=len(messages),
                tool_schema_count=len(tools),
                token_breakdown=token_breakdown,
            )
            llm_retries = 0

            def log_retry(attempt: int, error: LLMErrorInfo) -> None:
                nonlocal llm_retries
                llm_retries += 1
                log.log_trace(
                    EventType.LLM_CALL_RETRY,
                    step,
                    span_id=llm_span,
                    attempt=attempt,
                    retry_count=llm_retries,
                    error_type=error.kind.value,
                    status_code=error.status_code,
                    retry_after=error.retry_after,
                )

            try:
                response = self._call_with_retry(messages, tools, on_retry=log_retry)
            except Exception as exc:
                canceled = self._is_cancel_requested()
                error = classify_llm_error(exc)
                log.log_trace(
                    EventType.LLM_CALL_FAILED,
                    step,
                    span_id=llm_span,
                    duration_ms=(time.perf_counter() - llm_started) * 1000,
                    model=self._backend.model_name,
                    provider=type(self._backend).__name__,
                    retries=llm_retries,
                    retry_count=llm_retries,
                    token_breakdown=token_breakdown,
                    error_type="canceled" if canceled else error.kind.value,
                    status_code=error.status_code,
                    error=str(exc),
                    cancel_requested=canceled,
                )
                if canceled:
                    reason = "Canceled by external request"
                    logger.info("Agent task %s canceled during model call", task.task_id)
                    log.log_task_failed(steps=step, reason=reason)
                    return RunResult(
                        task_id=task.task_id,
                        status=RunStatus.CANCELED,
                        summary=reason,
                        steps_taken=step,
                        total_tokens=total_tokens,
                        usage=usage.snapshot(),
                        termination_reason="canceled",
                    )
                logger.error("LLM call failed at step %d after retries: %s", step, exc)
                log.log_task_failed(steps=step, reason=f"LLM error: {exc}")
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.FAILED,
                    summary=f"LLM call failed: {exc}",
                    steps_taken=step,
                    total_tokens=total_tokens,
                    usage=usage.snapshot(),
                    error=str(exc),
                    termination_reason="provider_error",
                )

            log.log_trace(
                EventType.LLM_CALL_FINISHED,
                step,
                span_id=llm_span,
                duration_ms=(time.perf_counter() - llm_started) * 1000,
                model=self._backend.model_name,
                provider=type(self._backend).__name__,
                retries=llm_retries,
                retry_count=llm_retries,
                token_breakdown=token_breakdown,
                usage=response.usage.to_dict(),
                provider_usage=response.usage.to_dict(),
            )
            usage.record(response.usage)
            total_tokens = usage.total_tokens

            # 同步 provider call 无法强杀；返回后第一安全边界优先响应 cancel。
            if self._is_cancel_requested():
                reason = "Canceled by external request"
                logger.info("Agent task %s canceled after model call", task.task_id)
                log.log_task_failed(steps=step, reason=reason)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.CANCELED,
                    summary=reason,
                    steps_taken=step,
                    total_tokens=total_tokens,
                    usage=usage.snapshot(),
                    termination_reason="canceled",
                )

            action = response.action
            action_event_ref = log.log_action(
                step=step,
                action=action,
                raw_content=response.raw_content,
                usage=response.usage,
            )
            logger.info("Step %d: %r", step, action)

            if (
                action.action_type == ActionType.TOOL_CALL
                and action.tool_call
                and self._planning_runtime
                and self._planning_runtime.is_control(action.tool_call.name)
            ):
                control = self._planning_runtime.apply_control(
                    action.tool_call.name, action.tool_call.params
                )
                log.log_trace(control.event_type, step, **control.payload)
                history.add(LLMMessage(role="user", content=control.message))
                continue

            if (
                action.action_type == ActionType.FINISH
                and self._planning_runtime
                and self._planning_runtime.requires_plan
                and self._planning_runtime.current_plan is None
            ):
                detail = "Structured Planning is enabled; create a valid plan before finishing."
                log.log_trace(EventType.PLAN_REJECTED, step, control="finish", error=detail)
                history.add(LLMMessage(
                    role="assistant",
                    content=self._format_action_for_history(action),
                    event_ref=action_event_ref,
                ))
                history.add(LLMMessage(role="user", content="[PLANNING REQUIRED] " + detail))
                continue

            if action.action_type == ActionType.FINISH:
                summary = action.message or "Task complete."
                patch = self._get_git_diff(task.repo_path)
                final_repo_state = self._get_repo_state(task.repo_path)
                rejection_code: str | None = None
                verification_error: str | None = None
                if fatal_error_key is not None:
                    reason = f"Unresolved fatal infrastructure error: {fatal_error_key}"
                    log.log_task_failed(steps=step, reason=reason)
                    return RunResult(
                        task_id=task.task_id, status=RunStatus.FAILED, summary=reason,
                        steps_taken=step, total_tokens=total_tokens, usage=usage.snapshot(),
                        patch=patch, error=reason, termination_reason="infrastructure_error",
                    )
                elif task.require_changes and final_repo_state == initial_repo_state:
                    rejection_code = "REPOSITORY_UNCHANGED"
                    verification_error = (
                        "Task requires repository changes, but the repository state did not change."
                    )
                elif task.require_tests and not test_attempted:
                    rejection_code = "REQUIRED_TEST_MISSING"
                    verification_error = (
                        "Task requires test verification, but no test tool was run."
                    )
                elif test_attempted and last_test_passed is not True:
                    rejection_code = "LATEST_TEST_FAILED"
                    verification_error = (
                        "Agent attempted verification, but the latest test did not pass."
                    )
                elif (
                    test_attempted
                    and last_repo_change_step is not None
                    and (
                        last_successful_test_step is None
                        or last_successful_test_step < last_repo_change_step
                    )
                ):
                    rejection_code = "FINAL_STATE_UNVERIFIED"
                    verification_error = (
                        "Files changed after the latest successful test; the final state is unverified."
                    )

                if verification_error is not None:
                    rejection_event_ref = log.log_completion_rejected(
                        step, rejection_code or "COMPLETION_REQUIREMENT_UNMET", verification_error
                    )
                    history.add(LLMMessage(
                        role="assistant",
                        content=self._format_action_for_history(action),
                        event_ref=action_event_ref,
                    ))
                    history.add(LLMMessage(
                        role="user",
                        content=completion_rejected(
                            rejection_code or "COMPLETION_REQUIREMENT_UNMET", verification_error
                        ),
                        event_ref=rejection_event_ref,
                    ))
                    continue
                log.log_task_complete(steps=step, summary=summary)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.SUCCESS,
                    summary=summary,
                    steps_taken=step,
                    total_tokens=total_tokens,
                    usage=usage.snapshot(),
                    patch=patch,
                    termination_reason="completion_satisfied",
                )

            if action.action_type == ActionType.GIVE_UP:
                reason = action.message or "Agent gave up."
                log.log_task_failed(steps=step, reason=reason)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=step,
                    total_tokens=total_tokens,
                    usage=usage.snapshot(),
                    termination_reason="agent_gave_up",
                )

            if action.action_type == ActionType.TOOL_CALL and action.tool_call:
                if self._is_cancel_requested():
                    reason = "Canceled by external request"
                    logger.info("Agent task %s canceled before tool execution", task.task_id)
                    log.log_task_failed(steps=step, reason=reason)
                    return RunResult(
                        task_id=task.task_id,
                        status=RunStatus.CANCELED,
                        summary=reason,
                        steps_taken=step,
                        total_tokens=total_tokens,
                        usage=usage.snapshot(),
                        termination_reason="canceled",
                    )

                tc = action.tool_call
                if (
                    self._planning_runtime
                    and self._planning_runtime.requires_plan
                    and self._planning_runtime.current_plan is None
                    and self._registry.is_mutating(tc.name, tc.params)
                ):
                    detail = f"Create a structured plan before repository-mutating tool {tc.name!r}."
                    log.log_trace(EventType.PLAN_REJECTED, step, control=tc.name, error=detail)
                    history.add(LLMMessage(
                        role="assistant",
                        content=self._format_action_for_history(action),
                        event_ref=action_event_ref,
                    ))
                    history.add(LLMMessage(role="user", content="[PLANNING REQUIRED] " + detail))
                    continue
                tool_started = time.perf_counter()
                tool_span = log.log_trace(
                    EventType.TOOL_EXECUTION_STARTED,
                    step,
                    tool_name=tc.name,
                )

                def log_permission_decision(name, params, decision) -> None:
                    log.log_permission_decision(
                        task.task_id,
                        name,
                        decision.decision.value,
                        decision.reason,
                        params,
                    )

                try:
                    result = self._executor.execute(
                        tc.name,
                        tc.params,
                        decision_callback=log_permission_decision,
                        cancel_event=self._cfg.cancel_event,
                    )
                except ToolExecutionCanceled as exc:
                    log.log_trace(
                        EventType.TOOL_EXECUTION_FAILED,
                        step,
                        span_id=tool_span,
                        tool_name=tc.name,
                        duration_ms=(time.perf_counter() - tool_started) * 1000,
                        result_bytes=0,
                        error_type="canceled",
                        error=str(exc),
                        lifecycle_phase=exc.phase,
                        cancel_requested=True,
                    )
                    reason = "Canceled by external request"
                    log.log_task_failed(steps=step, reason=reason)
                    return RunResult(
                        task_id=task.task_id,
                        status=RunStatus.CANCELED,
                        summary=reason,
                        steps_taken=step,
                        total_tokens=total_tokens,
                        usage=usage.snapshot(),
                        termination_reason="canceled",
                    )
                except ToolExecutorInfrastructureError as exc:
                    log.log_trace(
                        EventType.TOOL_EXECUTION_FAILED,
                        step,
                        span_id=tool_span,
                        tool_name=tc.name,
                        duration_ms=(time.perf_counter() - tool_started) * 1000,
                        result_bytes=0,
                        error_type=ToolErrorType.INFRASTRUCTURE.value,
                        error=str(exc),
                        lifecycle_phase=exc.phase,
                        framework_error_type=type(exc.original_error).__name__,
                    )
                    reason = f"Tool lifecycle infrastructure failure: {exc}"
                    log.log_task_failed(steps=step, reason=reason)
                    return RunResult(
                        task_id=task.task_id,
                        status=RunStatus.FAILED,
                        summary=reason,
                        steps_taken=step,
                        total_tokens=total_tokens,
                        usage=usage.snapshot(),
                        patch=self._get_git_diff(task.repo_path),
                        error=reason,
                        termination_reason="infrastructure_error",
                    )
                except Exception as exc:
                    # BaseTool 自身异常已由 ToolRegistry 转为 TOOL_EXECUTION；能到这里的是框架故障。
                    log.log_trace(
                        EventType.TOOL_EXECUTION_FAILED,
                        step,
                        span_id=tool_span,
                        tool_name=tc.name,
                        duration_ms=(time.perf_counter() - tool_started) * 1000,
                        result_bytes=0,
                        error_type=ToolErrorType.INFRASTRUCTURE.value,
                        error=str(exc),
                        framework_error_type=type(exc).__name__,
                    )
                    reason = f"Tool executor infrastructure failure: {type(exc).__name__}: {exc}"
                    log.log_task_failed(steps=step, reason=reason)
                    return RunResult(
                        task_id=task.task_id,
                        status=RunStatus.FAILED,
                        summary=reason,
                        steps_taken=step,
                        total_tokens=total_tokens,
                        usage=usage.snapshot(),
                        patch=self._get_git_diff(task.repo_path),
                        error=reason,
                        termination_reason="infrastructure_error",
                    )

                tool_event = (
                    EventType.TOOL_EXECUTION_FINISHED
                    if result.success
                    else EventType.TOOL_EXECUTION_FAILED
                )
                log.log_trace(
                    tool_event,
                    step,
                    span_id=tool_span,
                    tool_name=tc.name,
                    duration_ms=(time.perf_counter() - tool_started) * 1000,
                    result_bytes=len(result.output.encode("utf-8")),
                    error_type=result.error_type.value if result.error_type else None,
                    error=result.error,
                    diagnostics=list(result.diagnostics),
                )
                observation = result.to_observation(tc.name)

                current_repo_state = self._get_repo_state(task.repo_path)
                repository_changed = current_repo_state != last_repo_state
                if repository_changed:
                    steps_without_edit = 0
                    last_repo_change_step = step
                    last_repo_state = current_repo_state
                    if self._repo_map_mode() == "incremental":
                        if hasattr(self, "_repo_map_cache"):
                            del self._repo_map_cache
                        changed_path = (
                            tc.params.get("path")
                            if tc.name in ("file_write", "file_edit", "edit")
                            else None
                        )
                        try:
                            if changed_path and observation.is_success():
                                repo_map.update_paths([changed_path])  # type: ignore[attr-defined]
                                self._repo_map_sync_requested = False
                            else:
                                # shell/git 等也可能真实改仓库；未知路径时下一轮统一 sync。
                                self._repo_map_sync_requested = True
                        except Exception as exc:
                            logger.warning("Incremental Repo Map update failed: %s", exc)
                            self._repo_map_sync_requested = True
                    else:
                        self.invalidate_repo_map_cache(task.repo_path)
                else:
                    steps_without_edit += 1

                if tc.name in self._cfg.test_tool_names:
                    test_attempted = True
                    last_test_passed = observation.is_success()
                    if last_test_passed:
                        last_successful_test_step = step

                observation_event_ref = log.log_observation(
                    step=step,
                    observation=observation,
                )

                # Tool 已经开始时不强杀同步调用；真实结果和 post-hook 先落盘，再在这里取消。
                if self._is_cancel_requested():
                    reason = "Canceled by external request"
                    logger.info("Agent task %s canceled after tool execution", task.task_id)
                    log.log_task_failed(steps=step, reason=reason)
                    return RunResult(
                        task_id=task.task_id,
                        status=RunStatus.CANCELED,
                        summary=reason,
                        steps_taken=step,
                        total_tokens=total_tokens,
                        usage=usage.snapshot(),
                        patch=self._get_git_diff(task.repo_path),
                        termination_reason="canceled",
                    )

                infrastructure_error = self._detect_known_fatal_infrastructure_error(observation)
                if infrastructure_error is not None:
                    if infrastructure_error == fatal_error_key:
                        fatal_error_count += 1
                    else:
                        fatal_error_key = infrastructure_error
                        fatal_error_count = 1

                    if fatal_error_count >= max(1, self._cfg.fatal_tool_error_repeats):
                        reason = "Repeated fatal infrastructure error: " + infrastructure_error
                        logger.error(reason)
                        log.log_task_failed(steps=step, reason=reason)
                        return RunResult(
                            task_id=task.task_id,
                            status=RunStatus.FAILED,
                            summary=reason,
                            steps_taken=step,
                            total_tokens=total_tokens,
                            usage=usage.snapshot(),
                            patch=self._get_git_diff(task.repo_path),
                            error=reason,
                            termination_reason="infrastructure_error",
                        )
                else:
                    runtime_tools = {"shell", "test", "pytest", "git"}
                    if observation.is_success() and tc.name in runtime_tools:
                        fatal_error_key = None
                        fatal_error_count = 0

                history.add(LLMMessage(
                    role="assistant",
                    content=self._format_action_for_history(action),
                    event_ref=action_event_ref,
                ))
                history.add(LLMMessage(
                    role="user",
                    content=self._format_observation_for_history(observation),
                    event_ref=observation_event_ref,
                ))

                test_state = (
                    observation.status.value
                    if tc.name in self._cfg.test_tool_names
                    else None
                )
                loop_signal = loop_detector.observe(
                    action,
                    observation,
                    repo_state=current_repo_state,
                    test_state=test_state,
                )
                if loop_signal is not None:
                    payload = loop_signal.to_payload()
                    log.log_loop_detected(
                        step,
                        severity=payload["severity"],
                        period=payload["period"],
                        repeats=payload["repeats"],
                        occurrence=payload["occurrence"],
                        action_pattern=payload["action_pattern"],
                    )
                    if loop_signal.severity == LoopSeverity.TERMINATE:
                        reason = (
                            "Loop detected again after reflection: "
                            f"period {loop_signal.period} repeated "
                            f"{loop_signal.repeats} times without progress"
                        )
                        logger.warning(reason)
                        log.log_task_incomplete(
                            steps=step, reason=reason, termination_reason="loop_detected"
                        )
                        return RunResult(
                            task_id=task.task_id,
                            status=RunStatus.INCOMPLETE,
                            summary=reason,
                            steps_taken=step,
                            total_tokens=total_tokens,
                            usage=usage.snapshot(),
                            termination_reason="loop_detected",
                        )

                    reflect_prompt = reflection_loop_detected(
                        loop_signal.repeats,
                        loop_signal.period,
                    )
                    log.log_reflection(
                        step=step,
                        reason="loop_detected",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    logger.warning(
                        "Loop detected at step %d; injected recovery reflection", step,
                    )
                    continue

                if tc.name in self._cfg.test_tool_names and not observation.is_success():
                    reflect_prompt = reflection_test_failed()
                    log.log_reflection(
                        step=step,
                        reason="test_failed",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    logger.debug("Reflection triggered: test_failed at step %d", step)
                elif steps_without_edit >= self._cfg.reflection_no_edit_steps:
                    reflect_prompt = reflection_no_edit(steps_without_edit)
                    log.log_reflection(
                        step=step,
                        reason="no_edit",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    steps_without_edit = 0
                    logger.debug("Reflection triggered: no_edit at step %d", step)

            elif action.action_type == ActionType.REFLECTION:
                history.add(LLMMessage(role="assistant", content=action.thought))

        reason = f"Reached max_steps limit ({task.max_steps})"
        log.log_task_incomplete(
            steps=task.max_steps,
            reason=reason,
            termination_reason="resource_exhausted",
            resource_reason="max_steps",
        )
        return RunResult(
            task_id=task.task_id,
            status=RunStatus.INCOMPLETE,
            summary=reason,
            steps_taken=task.max_steps,
            total_tokens=total_tokens,
            usage=usage.snapshot(),
            termination_reason="resource_exhausted",
            resource_reason="max_steps",
        )

    @staticmethod
    def _detect_known_fatal_infrastructure_error(observation: Observation) -> str | None:
        """只检查可能经过 Runtime 的工具，不要所有失败 Observation 都检查。"""
        if observation.is_success():
            return None
        runtime_tools = {"shell", "test", "pytest", "git"}
        if observation.tool_name not in runtime_tools:
            return None
        text = "\n".join(
            part for part in (observation.output, observation.error) if part
        ).lower()
        markers = (
            "duplicate mount point",
            "invalid mount config",
            "cannot connect to the docker daemon",
            "docker is not available",
            "failed to start container",
            "permission denied while trying to connect to the docker daemon",
        )
        for marker in markers:
            if marker in text:
                return marker
        return None

    def _is_cancel_requested(self) -> bool:
        """查询外部取消对象；支持 threading.Event-like 与布尔值。"""
        event = self._cfg.cancel_event
        if event is None:
            return False
        is_set = getattr(event, "is_set", None)
        if callable(is_set):
            return bool(is_set())
        return bool(event)

    def _prepare_next_turn(
        self, task: Task, step: int, history: ConversationHistory, repo_map: RepoMap,
        token_budget: TokenBudget, log: EventLog, total_tokens: int, usage: SessionUsage,
    ) -> RunResult | None:
        """在完整 turn 后集中处理下一轮准备、取消、Trace 和失败语义。"""
        callback = self._cfg.prepare_next_turn
        if callback is None:
            return None
        if self._is_cancel_requested():
            reason = "Canceled by external request"
            log.log_task_failed(steps=step - 1, reason=reason)
            return RunResult(
                task.task_id, RunStatus.CANCELED, reason, step - 1,
                total_tokens, usage.snapshot(), termination_reason="canceled"
            )

        system_content, repo_map_content, schemas = self._render_request_parts(
            token_budget, repo_map
        )
        prepare_started = time.perf_counter()
        prepare_span = log.log_trace(EventType.PREPARE_NEXT_TURN_STARTED, step)
        context = PrepareNextTurnContext(
            task=task,
            step=step,
            history=history,
            repo_map=repo_map,
            token_budget=token_budget,
            cancel_event=self._cfg.cancel_event,
            event_log=log,
            system_content=system_content,
            repo_map_content=repo_map_content,
            tool_schemas=schemas,
        )
        try:
            prepared = callback(context)
        except Exception as exc:
            log.log_trace(
                EventType.PREPARE_NEXT_TURN_FAILED,
                step,
                span_id=prepare_span,
                duration_ms=(time.perf_counter() - prepare_started) * 1000,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            reason = f"prepare_next_turn failed: {type(exc).__name__}: {exc}"
            logger.exception("prepare_next_turn failed before step %d", step)
            log.log_task_failed(steps=step - 1, reason=reason)
            return RunResult(
                task.task_id, RunStatus.FAILED, reason, step - 1,
                total_tokens, usage.snapshot(), error=reason,
                termination_reason="infrastructure_error"
            )

        refreshed = bool(prepared and prepared.refresh_repo_map)
        if refreshed:
            self.invalidate_repo_map_cache(task.repo_path)
        if prepared is not None and prepared.history_override is not None:
            self._prepared_history_override = prepared.history_override
        log.log_trace(
            EventType.PREPARE_NEXT_TURN_FINISHED,
            step,
            span_id=prepare_span,
            duration_ms=(time.perf_counter() - prepare_started) * 1000,
            injected_messages=len(prepared.messages) if prepared else 0,
            history_override_messages=(
                len(prepared.history_override)
                if prepared and prepared.history_override is not None
                else 0
            ),
            repo_map_refreshed=refreshed,
        )
        if self._is_cancel_requested():
            reason = "Canceled by external request"
            logger.info("Agent task %s canceled after prepare_next_turn", task.task_id)
            log.log_task_failed(steps=step - 1, reason=reason)
            return RunResult(
                task.task_id, RunStatus.CANCELED, reason, step - 1,
                total_tokens, usage.snapshot(), termination_reason="canceled"
            )
        if prepared is not None and prepared.messages:
            history.add_many(list(prepared.messages))
        return None

    def _render_request_parts(
        self,
        token_budget: TokenBudget,
        repo_map: RepoMap,
    ) -> tuple[str, str, tuple[LLMToolSchema, ...]]:
        """统一生成下一请求固定部分，供 pressure 计算与最终消息组装复用。"""
        planning_schemas = self._planning_runtime.schemas() if self._planning_runtime else ()
        schemas = tuple(self._registry.get_schemas()) + tuple(planning_schemas)
        mode = self._repo_map_mode()
        if not hasattr(self, "_repo_map_cache"):
            map_budget = token_budget.default_plan().repo_map
            query = None if mode == "static" else getattr(self, "_repo_map_query", None)
            started = time.perf_counter()
            if mode == "none":
                self._repo_map_cache = ""
            elif mode == "incremental":
                if self._repo_map_sync_requested:
                    repo_map.sync()  # type: ignore[attr-defined]
                self._repo_map_cache = repo_map.build(budget=map_budget, query=query)
            elif self._repo_map_force_refresh:
                self._repo_map_cache = repo_map.build(
                    budget=map_budget, force_refresh=True, query=query
                )
            else:
                self._repo_map_cache = repo_map.build(budget=map_budget, query=query)
            self._repo_map_build_seconds += time.perf_counter() - started
            self._repo_map_build_calls += int(mode != "none")
            self._repo_map_force_refresh = False
            self._repo_map_sync_requested = False
            self._repo_map_cache_query = getattr(self, "_repo_map_query", None)

        repo_map_content = self._repo_map_cache
        planning_context = self._planning_runtime.render_context() if self._planning_runtime else ""
        self._planning_context_cache = planning_context
        system_content = build_system_prompt(
            repo_path=getattr(self, "_model_repo_path", "."),
            tools=list(schemas),
            repo_summary=repo_map_content if mode != "none" else None,
            execution_workspace=self._cfg.execution_workspace,
            runtime_context=planning_context or None,
        )
        return system_content, repo_map_content, schemas

    def _build_messages(
        self,
        history: ConversationHistory,
        token_budget: TokenBudget,
        repo_map: RepoMap,
    ) -> list[LLMMessage]:
        """组装发给 LLM 的完整 messages；一次性 override 不写回 canonical history。"""
        system_content, _, schemas = self._render_request_parts(token_budget, repo_map)
        if self._prepared_history_override is not None:
            visible_history = list(self._prepared_history_override)
            self._prepared_history_override = None
            raw_history = [
                {
                    "role": message.role,
                    "content": message.content,
                    **({"tool_call_id": message.tool_call_id} if message.tool_call_id else {}),
                }
                for message in visible_history
            ]
        else:
            raw_history = history.to_dicts()

        history_limit = token_budget.history_limit_for_request(system_content, schemas)
        trimmed_history_dicts = token_budget.trim_history(raw_history, history_limit)
        messages = [LLMMessage(role="system", content=system_content)]
        for item in trimmed_history_dicts:
            messages.append(LLMMessage(
                role=item["role"],
                content=item["content"],
                tool_call_id=item.get("tool_call_id"),
            ))
        return messages

    def _trace_token_breakdown(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
        *,
        injected_messages: list[LLMMessage] | None = None,
    ) -> dict[str, int]:
        """Estimate why a provider request is large without claiming billing truth."""

        def as_dict(message: LLMMessage) -> dict:
            data = {"role": message.role, "content": message.content}
            if message.tool_call_id:
                data["tool_call_id"] = message.tool_call_id
            return data

        message_dicts = [as_dict(message) for message in messages]
        system_message_tokens = (
            estimate_message_tokens(message_dicts[0]) if message_dicts else 0
        )
        repo_map_tokens = estimate_tokens(getattr(self, "_repo_map_cache", ""))
        repo_map_tokens = min(repo_map_tokens, system_message_tokens)
        planning_context = getattr(self, "_planning_context_cache", "")
        planning_tokens = (
            min(
                estimate_tokens(planning_context),
                max(0, system_message_tokens - repo_map_tokens),
            )
            if planning_context
            else 0
        )
        system_tokens = max(0, system_message_tokens - repo_map_tokens - planning_tokens)
        tool_schema_tokens = estimate_tool_schemas_tokens(tools)
        injected_dicts = [as_dict(message) for message in (injected_messages or [])]
        injected_tokens = estimate_messages_tokens(injected_dicts)
        non_system_tokens = estimate_messages_tokens(message_dicts[1:])
        history_tokens = max(0, non_system_tokens - injected_tokens)
        pending_tokens = 0
        context_tokens = history_tokens + injected_tokens + pending_tokens
        estimated_input_tokens = (
            system_tokens
            + repo_map_tokens
            + planning_tokens
            + tool_schema_tokens
            + context_tokens
        )
        return {
            "system_tokens": system_tokens,
            "tool_schema_tokens": tool_schema_tokens,
            "repo_map_tokens": repo_map_tokens,
            "planning_tokens": planning_tokens,
            "history_tokens": history_tokens,
            "context_tokens": context_tokens,
            "pending_tokens": pending_tokens,
            "injected_tokens": injected_tokens,
            "estimated_input_tokens": estimated_input_tokens,
        }

    def _format_action_for_history(self, action: Action) -> str:
        parts = [f"Thought: {action.thought}"]
        if action.tool_call:
            parts.append(f"Action: {action.tool_call.name}")
            parts.append(f"Params: {json.dumps(action.tool_call.params, ensure_ascii=False)}")
        elif action.message:
            parts.append(f"Message: {action.message}")
        return "\n".join(parts)

    def _format_observation_for_history(self, observation: Observation) -> str:
        status = observation.status.value.upper()
        lines = [f"[Tool: {observation.tool_name} | {status}]"]
        if observation.output:
            lines.append(observation.output)
        if observation.error and not observation.is_success():
            lines.append(f"Error: {observation.error}")
        return "\n".join(lines)

    def _call_with_retry(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
        on_retry: Callable[[int, LLMErrorInfo], None] | None = None,
    ):
        """Call the backend with bounded retry; cancel interrupts retry waits, not an active sync call."""
        import random

        attempts = self._cfg.llm_max_retries
        if attempts < 1:
            raise RuntimeError("llm_max_retries must be at least 1")

        for attempt in range(1, attempts + 1):
            if self._is_cancel_requested():
                raise RuntimeError("Canceled by external request")
            stream_output_started = False

            def tracked_callback(callback):
                def dispatch(chunk: str) -> None:
                    nonlocal stream_output_started
                    if not chunk:
                        return
                    stream_output_started = True
                    if callback is None:
                        return
                    try:
                        callback(chunk)
                    except Exception as exc:
                        raise LLMCallbackError(
                            f"stream callback failed: {exc}"
                        ) from exc
                return dispatch

            try:
                if self._cfg.stream:
                    return self._backend.stream(
                        messages,
                        tools,
                        on_text=tracked_callback(self._cfg.stream_callback),
                        on_thought=tracked_callback(self._cfg.thought_callback),
                    )
                return self._backend.complete(messages, tools)
            except LLMCallbackError:
                raise
            except Exception as exc:
                if self._is_cancel_requested():
                    raise
                error = classify_llm_error(exc)
                if stream_output_started or not error.retryable or attempt >= attempts:
                    raise

                requested_delay = (
                    error.retry_after
                    if error.retry_after is not None
                    else self._cfg.llm_retry_delay * (2 ** (attempt - 1))
                )
                max_delay = max(0.0, self._cfg.llm_retry_max_delay)
                delay = min(max_delay, max(0.0, requested_delay))
                jitter_ratio = max(0.0, self._cfg.llm_retry_jitter)
                if delay and jitter_ratio:
                    delay = min(
                        max_delay,
                        delay + random.uniform(0.0, delay * jitter_ratio),
                    )
                logger.warning(
                    "LLM %s error (attempt %d/%d): %s — retrying in %.1fs",
                    error.kind.value,
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                if on_retry is not None:
                    on_retry(attempt, error)
                wait = getattr(self._cfg.cancel_event, "wait", None)
                if delay and callable(wait):
                    if wait(delay):
                        raise
                elif delay:
                    time.sleep(delay)

        raise AssertionError("retry loop exited unexpectedly")

    def _get_git_diff(self, repo_path: str) -> str | None:
        import subprocess
        try:
            proc = subprocess.run(
                ["git", "diff", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo_path,
            )
            diff = proc.stdout.strip()
            return diff if diff else None
        except Exception:
            return None

    def _get_repo_state(self, repo_path: str) -> str:
        return repository_fingerprint(repo_path)


def _default_executor(
    registry: ToolRegistry,
    hooks: "Hooks | None" = None,
) -> "ToolExecutor":
    """构造透明 ToolExecutor；产品入口由 ExecutionRunner 注入 permission。"""
    from harness.executor import ToolExecutor
    return ToolExecutor(registry, hooks=hooks)
