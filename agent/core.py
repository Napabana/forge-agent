"""
agent/core.py

ReAct 主循环。整个 agent 的大脑。

职责（只做这些，不做别的）：
- 维护对话历史，每轮组装 messages 调用 LLM
- 拿到 Action 后调用 ToolRegistry 执行
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
from agent.loop_detector import LoopDetector, LoopSeverity, snapshot_repository
from context.history import ConversationHistory
from context.repo_map import RepoMap
from context.repository_state import repository_fingerprint
from context.token_budget import TokenBudget
from agent.prompt import (
    build_system_prompt,
    build_task_prompt,
    reflection_loop_detected,
    reflection_no_edit,
    reflection_test_failed,
    step_budget_warning,
)
from agent.task import (
    Action, ActionType, Event, EventType,
    Observation, ObservationStatus, RunResult, RunStatus, Task, ToolCall,
)
from llm.base import LLMBackend, LLMMessage, LLMToolSchema
from llm.errors import LLMCallbackError, LLMErrorInfo, classify_llm_error
from llm.usage import SessionUsage
from tools.base import ToolRegistry

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
    refresh_repo_map: bool = False  # True 时让下一次消息组装重新扫描当前仓库
    # 仅下一次 LLM 调用可见；不会写回 canonical ConversationHistory。
    history_override: tuple[LLMMessage, ...] | None = None


PrepareNextTurn = Callable[
    [PrepareNextTurnContext],
    PrepareNextTurnResult | None,
]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Agent 运行时配置，从 config/default.yaml 加载后传入。"""
    max_steps: int = 40
    reflection_no_edit_steps: int = 6   # 连续 N 步无文件写操作触发 Reflection
    loop_detection_window: int = 3       # 同一周期至少重复 N 次
    loop_detection_max_period: int = 3   # 检测 AAA / ABABAB / ABCABCABC
    test_tool_names: tuple[str, ...] = ("test", "pytest")  # 触发 Reflection 的工具名
    fatal_tool_error_repeats: int = 2     # repeated fatal infrastructure errors before abort
    budget_tokens: int = 80_000            # 总 token 预算
    history_max_messages: int = 40         # 兼容旧配置；canonical history 不再按条数破坏性裁剪
    llm_max_retries: int = 3               # LLM 调用失败最大重试次数
    llm_retry_delay: float = 2.0           # 重试间隔（秒，指数退避）
    llm_retry_max_delay: float = 30.0      # 单次等待上限
    llm_retry_jitter: float = 0.0          # 随机抖动比例（0=关闭）
    stream: bool = False                   # 是否启用流式输出
    stream_callback: object = None         # StreamCallback，最终回答流式回调
    thought_callback: object = None        # StreamCallback，推理模型专用
    confirm_dangerous: bool = False        # 是否对危险命令要求用户确认
    confirm_callback: object = None        # ConfirmCallback，None=跳过确认
    cancel_event: object = None            # threading.Event-like；set 后协作取消
    hooks: Hooks | None = None              # 可选工具生命周期 hooks
    prepare_next_turn: PrepareNextTurn | None = None


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Agent:
    """
    ReAct 主循环实现。

    用法：
        agent = Agent(backend, registry, config)
        result = agent.run(task, log)
    """

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
        # 默认透明直通：未注入 executor 时用一个无 hooks/permission 的
        # ToolExecutor 包住 registry，行为等价于直接 registry.execute_tool。
        # 需要安全管线的地方（如 --confirm / 多智能体入口）显式注入 executor。
        self._executor = executor or _default_executor(registry, self._cfg.hooks)
        self._repo_map_cache_key: str | None = None
        self._repo_map_force_refresh = False
        self._prepared_history_override: tuple[LLMMessage, ...] | None = None

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
        self._repo_map_force_refresh = True
        return existed

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run(
        self,
        task: Task,
        log: EventLog,
        history: ConversationHistory | None = None,
    ) -> RunResult:
        """
        执行一次完整的 agent 运行。

        Args:
            task: 任务描述
            log:  已初始化的 EventLog（由调用方创建并传入）
            history: 可选共享对话历史。chat/session 模式传入后跨轮复用；
                     None 时为单次 run 新建 history。

        Returns:
            RunResult，包含最终状态和统计信息
        """
        self._current_repo_path = task.repo_path
        self._repo_map_query = task.description
        cache_key = task.repo_path
        if self._repo_map_cache_key != cache_key:
            self.invalidate_repo_map_cache()
            self._repo_map_force_refresh = False
            self._repo_map_cache_key = cache_key
            self._repo_map_instance = RepoMap(task.repo_path)
        elif getattr(self, "_repo_map_cache_query", None) != task.description:
            if hasattr(self, "_repo_map_cache"):
                del self._repo_map_cache

        log.log_task_start(task)
        logger.info("Agent starting task %s", task.task_id)

        if history is None:
            history = ConversationHistory(max_messages=self._cfg.history_max_messages)
            history.add(LLMMessage(
                role="user",
                content=build_task_prompt(task.description, task.repo_path, task.issue_url),
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
        successful_write = False
        last_write_step: int | None = None
        initial_repo_state = (
            self._get_repo_state(task.repo_path) if task.require_changes else None
        )
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
                )

            if step > 1:
                prepare_result = self._prepare_next_turn(
                    task, step, history, repo_map, token_budget, log, total_tokens, usage
                )
                if prepare_result is not None:
                    return prepare_result

            logger.debug("Step %d/%d", step, task.max_steps)

            messages = self._build_messages(history, token_budget, repo_map)
            # Step 上限是最坏情况熔断器；最后三轮给模型显式收尾信号，避免突然硬切。
            remaining_steps = task.max_steps - step + 1
            if remaining_steps <= 3:
                messages.append(LLMMessage(
                    role="user",
                    content=step_budget_warning(remaining_steps),
                ))
            tools = self._registry.get_schemas()
            llm_started = time.perf_counter()
            llm_span = log.log_trace(
                EventType.LLM_CALL_STARTED,
                step,
                model=self._backend.model_name,
                provider=type(self._backend).__name__,
                message_count=len(messages),
                tool_schema_count=len(tools),
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
                    error_type=error.kind.value,
                    status_code=error.status_code,
                    retry_after=error.retry_after,
                )

            try:
                response = self._call_with_retry(messages, tools, on_retry=log_retry)
            except Exception as exc:
                error = classify_llm_error(exc)
                log.log_trace(
                    EventType.LLM_CALL_FAILED,
                    step,
                    span_id=llm_span,
                    duration_ms=(time.perf_counter() - llm_started) * 1000,
                    model=self._backend.model_name,
                    provider=type(self._backend).__name__,
                    retries=llm_retries,
                    error_type=error.kind.value,
                    status_code=error.status_code,
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
                )

            log.log_trace(
                EventType.LLM_CALL_FINISHED,
                step,
                span_id=llm_span,
                duration_ms=(time.perf_counter() - llm_started) * 1000,
                model=self._backend.model_name,
                provider=type(self._backend).__name__,
                retries=llm_retries,
                usage=response.usage.to_dict(),
            )
            usage.record(response.usage)
            total_tokens = usage.total_tokens
            action = response.action

            action_event_ref = log.log_action(
                step=step,
                action=action,
                raw_content=response.raw_content,
                usage=response.usage,
            )
            logger.info("Step %d: %r", step, action)

            if action.action_type == ActionType.FINISH:
                summary = action.message or "Task complete."
                patch = self._get_git_diff(task.repo_path)
                final_repo_state = (
                    self._get_repo_state(task.repo_path)
                    if task.require_changes
                    else None
                )
                verification_error: str | None = None
                if fatal_error_key is not None:
                    verification_error = (
                        f"Unresolved fatal infrastructure error: {fatal_error_key}"
                    )
                elif task.require_changes and not successful_write:
                    verification_error = (
                        "Task requires repository changes, but no write tool completed successfully."
                    )
                elif (
                    task.require_changes
                    and initial_repo_state is not None
                    and final_repo_state == initial_repo_state
                ):
                    verification_error = (
                        "Task requires repository changes, but the repository state did not change."
                    )
                elif task.require_tests and not test_attempted:
                    verification_error = (
                        "Task requires test verification, but no test tool was run."
                    )
                elif test_attempted and last_test_passed is not True:
                    verification_error = (
                        "Agent attempted verification, but the latest test did not pass."
                    )
                elif (
                    test_attempted
                    and last_write_step is not None
                    and (
                        last_successful_test_step is None
                        or last_successful_test_step < last_write_step
                    )
                ):
                    verification_error = (
                        "Files changed after the latest successful test; the final state is unverified."
                    )

                if verification_error is not None:
                    log.log_task_failed(steps=step, reason=verification_error)
                    return RunResult(
                        task_id=task.task_id,
                        status=RunStatus.FAILED,
                        summary=verification_error,
                        steps_taken=step,
                        total_tokens=total_tokens,
                        usage=usage.snapshot(),
                        patch=patch,
                        error=verification_error,
                    )
                log.log_task_complete(steps=step, summary=summary)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.SUCCESS,
                    summary=summary,
                    steps_taken=step,
                    total_tokens=total_tokens,
                    usage=usage.snapshot(),
                    patch=patch,
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
                    )

                tc = action.tool_call
                tool_started = time.perf_counter()
                tool_span = log.log_trace(
                    EventType.TOOL_EXECUTION_STARTED,
                    step,
                    tool_name=tc.name,
                )
                try:
                    result = self._executor.execute(tc.name, tc.params)
                except Exception as exc:
                    log.log_trace(
                        EventType.TOOL_EXECUTION_FAILED,
                        step,
                        span_id=tool_span,
                        tool_name=tc.name,
                        duration_ms=(time.perf_counter() - tool_started) * 1000,
                        result_bytes=0,
                        error_type=type(exc).__name__,
                    )
                    raise

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
                    diagnostics=list(result.diagnostics),
                )
                observation = result.to_observation(tc.name)

                if tc.name in ("file_write", "file_edit", "edit"):
                    steps_without_edit = 0
                    if observation.is_success():
                        successful_write = True
                        last_write_step = step
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

                infrastructure_error = self._detect_known_fatal_infrastructure_error(
                    observation
                )
                if infrastructure_error is not None:
                    if infrastructure_error == fatal_error_key:
                        fatal_error_count += 1
                    else:
                        fatal_error_key = infrastructure_error
                        fatal_error_count = 1

                    if fatal_error_count >= max(1, self._cfg.fatal_tool_error_repeats):
                        reason = (
                            "Repeated fatal infrastructure error: "
                            f"{infrastructure_error}"
                        )
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
                    repo_state=snapshot_repository(task.repo_path),
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
                        log.log_task_failed(steps=step, reason=reason)
                        return RunResult(
                            task_id=task.task_id,
                            status=RunStatus.GAVE_UP,
                            summary=reason,
                            steps_taken=step,
                            total_tokens=total_tokens,
                            usage=usage.snapshot(),
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
                        "Loop detected at step %d; injected recovery reflection",
                        step,
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
                history.add(LLMMessage(
                    role="assistant",
                    content=action.thought,
                ))

        reason = f"Reached max_steps limit ({task.max_steps})"
        log.log_task_failed(steps=task.max_steps, reason=reason)
        return RunResult(
            task_id=task.task_id,
            status=RunStatus.MAX_STEPS,
            summary=reason,
            steps_taken=task.max_steps,
            total_tokens=total_tokens,
            usage=usage.snapshot(),
        )

    @staticmethod
    def _detect_known_fatal_infrastructure_error(
        observation: Observation,
    ) -> str | None:
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

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

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
                total_tokens, usage.snapshot()
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
            )
            reason = f"prepare_next_turn failed: {type(exc).__name__}: {exc}"
            logger.exception("prepare_next_turn failed before step %d", step)
            log.log_task_failed(steps=step - 1, reason=reason)
            return RunResult(
                task.task_id, RunStatus.FAILED, reason, step - 1,
                total_tokens, usage.snapshot(), error=reason
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
                total_tokens, usage.snapshot()
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
        schemas = tuple(self._registry.get_schemas())
        if not hasattr(self, "_repo_map_cache"):
            map_budget = token_budget.default_plan().repo_map
            if self._repo_map_force_refresh:
                self._repo_map_cache = repo_map.build(
                    budget=map_budget,
                    force_refresh=True,
                    query=getattr(self, "_repo_map_query", None),
                )
            else:
                self._repo_map_cache = repo_map.build(
                    budget=map_budget,
                    query=getattr(self, "_repo_map_query", None),
                )
            self._repo_map_force_refresh = False
            self._repo_map_cache_query = getattr(self, "_repo_map_query", None)

        repo_map_content = self._repo_map_cache
        system_content = build_system_prompt(
            repo_path=getattr(self, "_current_repo_path", "."),
            tools=list(schemas),
            repo_summary=repo_map_content,
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

        history_limit = token_budget.history_limit_for_request(
            system_content,
            schemas,
        )
        trimmed_history_dicts = token_budget.trim_history(raw_history, history_limit)

        messages = [LLMMessage(role="system", content=system_content)]
        for item in trimmed_history_dicts:
            messages.append(LLMMessage(
                role=item["role"],
                content=item["content"],
                tool_call_id=item.get("tool_call_id"),
            ))
        return messages

    def _format_action_for_history(self, action: Action) -> str:
        """把 Action 格式化为 assistant 消息，写入对话历史。"""
        parts = [f"Thought: {action.thought}"]
        if action.tool_call:
            parts.append(f"Action: {action.tool_call.name}")
            parts.append(
                f"Params: {json.dumps(action.tool_call.params, ensure_ascii=False)}"
            )
        elif action.message:
            parts.append(f"Message: {action.message}")
        return "\n".join(parts)

    def _format_observation_for_history(self, observation: Observation) -> str:
        """把 Observation 格式化为 user 消息，写入对话历史。"""
        status = "SUCCESS" if observation.is_success() else "ERROR"
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
        """Call the backend with bounded retry for whitelisted transient errors."""
        import random

        attempts = self._cfg.llm_max_retries
        if attempts < 1:
            raise RuntimeError("llm_max_retries must be at least 1")

        for attempt in range(1, attempts + 1):
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
                time.sleep(delay)

        raise AssertionError("retry loop exited unexpectedly")

    def _get_git_diff(self, repo_path: str) -> str | None:
        """抓取 git diff HEAD 作为 patch，失败时静默返回 None。"""
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
        """返回 HEAD + working-tree 指纹，提交后的 clean 状态也能与基线区分。"""
        return repository_fingerprint(repo_path)


def _default_executor(
    registry: ToolRegistry,
    hooks: "Hooks | None" = None,
) -> "ToolExecutor":
    """
    构造一个透明直通的 ToolExecutor：无 hooks、无 permission，
    行为等价于直接调 registry.execute_tool。延迟 import 避免 agent <-> harness 循环。
    """
    from harness.executor import ToolExecutor
    return ToolExecutor(registry, hooks=hooks)