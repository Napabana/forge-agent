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
from dataclasses import dataclass

from agent.event_log import EventLog
from context.history import ConversationHistory
from context.repo_map import RepoMap
from context.token_budget import TokenBudget
from agent.prompt import (
    build_system_prompt,
    build_task_prompt,
    reflection_no_edit,
    reflection_test_failed,
)
from agent.task import (
    Action, ActionType, Event, EventType,
    Observation, ObservationStatus, RunResult, RunStatus, Task, ToolCall,
)
from llm.base import LLMBackend, LLMMessage, LLMToolSchema
from tools.base import ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Agent 运行时配置，从 config/default.yaml 加载后传入。"""
    max_steps: int = 40
    reflection_no_edit_steps: int = 6   # 连续 N 步无文件写操作触发 Reflection
    loop_detection_window: int = 3       # 连续 N 步完全相同 action 判定死循环
    test_tool_names: tuple[str, ...] = ("test", "pytest")  # 触发 Reflection 的工具名
    budget_tokens: int = 80_000            # 总 token 预算
    history_max_messages: int = 40         # 历史最大条数
    llm_max_retries: int = 3               # LLM 调用失败最大重试次数
    llm_retry_delay: float = 2.0           # 重试间隔（秒，指数退避）
    stream: bool = False                   # 是否启用流式输出
    stream_callback: object = None         # StreamCallback，最终回答流式回调
    thought_callback: object = None        # StreamCallback，推理过程流式回调（推理模型专用）
    confirm_dangerous: bool = False        # 是否对危险命令要求用户确认
    confirm_callback: object = None        # ConfirmCallback，None=跳过确认
    cancel_event: object = None            # threading.Event-like；set 后协作取消



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
        self._executor = executor or _default_executor(registry)

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
        
        #5步初始化
        
        #1.根据 task.repo_path 判断 repo map 缓存是否需要失效。
        ##本质就是一个简单的 per-repository cache invalidation（按仓库粒度的缓存失效机制）。
        #同一个 Agent 实例可能被用来跑不同仓库的任务（比如 chat 模式跨轮、或多次 run），而 repo_map 缓存必须跟着仓库走。
        self._current_repo_path = task.repo_path
        # 按 repo_path 隔离 repo_map 缓存，换 repo 时自动重建
        cache_key = task.repo_path
        #用getattr和hasattr：
            #因为 repo_map_cache 和它的 key 都是运行时才挂上的属性，不在 __init__ 里声明。如果写 if self._repo_map_cache_key != cache_key，第一次调用就会 AttributeError
        if getattr(self, "_repo_map_cache_key", None) != cache_key:
            if hasattr(self, "_repo_map_cache"):
                del self._repo_map_cache  #只是先让旧缓存失效，还没有重建repo_map()
            self._repo_map_cache_key = cache_key#删掉旧缓存，强迫重建
        
        #2.写入 TASK_START 事件
        log.log_task_start(task)
        logger.info("Agent starting task %s", task.task_id)

        # chat/session 模式可以传入共享 history
        #单次 run 新建。
        #3.如果没有传入共享 ConversationHistory，则新建历史，并把任务 prompt 作为第一条 user 消息。
        if history is None:
            history = ConversationHistory(max_messages=self._cfg.history_max_messages)
            # 单次模式：把任务描述作为第一条 user 消息
            from agent.prompt import build_task_prompt
            history.add(LLMMessage(
                role="user",
                content=build_task_prompt(task.description, task.repo_path, task.issue_url),
            ))
            
        #4.创建 TokenBudget
        #在上下文窗口装不下时，决定砍掉哪些内容 ；控制当前给LLM的上下文
        token_budget = TokenBudget(total=self._cfg.budget_tokens)
        #把根路径 resolve() 存下来，扫描仓库生成一段给 LLM 看的目录+符号摘要
        #5.创建 RepoMap。
        repo_map = RepoMap(task.repo_path)

        total_tokens = 0
        steps_without_edit = 0

        #核心循环
        for step in range(1, task.max_steps + 1):
            #检查用户是否取消请求
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
                )
            
            logger.debug("Step %d/%d", step, task.max_steps)

            # ── 1. 组装 messages，调用 LLM ──────────────────────────────
            messages = self._build_messages(history, token_budget, repo_map)
            tools = self._registry.get_schemas()#得到概述

            try:
                #调用、分类、等待、重试、抛异常
                response = self._call_with_retry(messages, tools)
            except Exception as exc:
                #写任务失败日志
                # → 构造 RunResult
                # → 将任务状态设为 FAILED
                logger.error("LLM call failed at step %d after retries: %s", step, exc)
                log.log_task_failed(steps=step, reason=f"LLM error: {exc}")
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.FAILED,
                    summary=f"LLM call failed: {exc}",
                    steps_taken=step,
                    total_tokens=total_tokens,
                    error=str(exc),
                )

            total_tokens += response.total_tokens
            action = response.action

            # ── 2. 写入 Action event ────────────────────────────────────
            log.log_action(step=step, action=action, raw_content=response.raw_content)
            logger.info("Step %d: %r", step, action)

            # ── 3. 检测死循环（连续相同 action）────────────────────────
            #先写入再检测 一次检测到循环 → 立即硬熔断
            if self._is_looping(log):
                reason = f"Loop detected: same action repeated {self._cfg.loop_detection_window} times"
                logger.warning(reason)
                log.log_task_failed(steps=step, reason=reason)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=step,
                    total_tokens=total_tokens,
                )

            # ── 4. 终止 action ──────────────────────────────────────────
            if action.action_type == ActionType.FINISH:
                summary = action.message or "Task complete."
                patch = self._get_git_diff(task.repo_path)
                log.log_task_complete(steps=step, summary=summary)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.SUCCESS,
                    summary=summary,
                    steps_taken=step,
                    total_tokens=total_tokens,
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
                )

            # ── 5. 执行工具 ─────────────────────────────────────────────
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
                    )

                tc = action.tool_call
                result = self._executor.execute(tc.name, tc.params)
                observation = result.to_observation(tc.name)

                # 追踪是否有文件写操作
                if tc.name in ("file_write", "file_edit", "edit"):
                    steps_without_edit = 0
                else:
                    steps_without_edit += 1

                log.log_observation(step=step, observation=observation)

                # 把 action 和 observation 加入对话历史
                history.add(LLMMessage(
                    role="assistant",
                    content=self._format_action_for_history(action),
                ))
                history.add(LLMMessage(
                    role="user",
                    content=self._format_observation_for_history(observation),
                ))

                # ── 6. Reflection 触发判断 ──────────────────────────────

                # 触发条件 A：测试工具失败
                if (
                    tc.name in self._cfg.test_tool_names
                    and not observation.is_success()
                ):
                    reflect_prompt = reflection_test_failed()
                    log.log_reflection(
                        step=step,
                        reason="test_failed",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    logger.debug("Reflection triggered: test_failed at step %d", step)

                # 触发条件 B：连续 N 步无编辑
                elif steps_without_edit >= self._cfg.reflection_no_edit_steps:
                    reflect_prompt = reflection_no_edit(steps_without_edit)
                    log.log_reflection(
                        step=step,
                        reason="no_edit",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    steps_without_edit = 0  # 重置计数，避免每步都触发
                    logger.debug("Reflection triggered: no_edit at step %d", step)

            elif action.action_type == ActionType.REFLECTION:
                # LLM 主动要求 reflection（预留，当前 MockBackend 不产生）
                history.add(LLMMessage(
                    role="assistant",
                    content=action.thought,
                ))

        # ── 7. 超出步数上限 ─────────────────────────────────────────────
        reason = f"Reached max_steps limit ({task.max_steps})"
        log.log_task_failed(steps=task.max_steps, reason=reason)
        return RunResult(
            task_id=task.task_id,
            status=RunStatus.MAX_STEPS,
            summary=reason,
            steps_taken=task.max_steps,
            total_tokens=total_tokens,
        )

    def _is_cancel_requested(self) -> bool:
        """is_set 是取消对象提供的状态查询方法。
        外部通过 cancel_event.set() 发出取消信号，Agent 使用 getattr() 安全取得 is_set 方法，
        确认它可调用后执行 is_set()；返回 True 就表示已经收到取消请求。"""
        event = self._cfg.cancel_event
        if event is None:
            return False
        #event.is_set()    # 查询当前状态
        is_set = getattr(event, "is_set", None)
        #callable() 判断对象能不能像函数一样调用。
        if callable(is_set):
            return bool(is_set())
        return bool(event)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        history: ConversationHistory,
        token_budget: TokenBudget,
        repo_map: RepoMap,
    ) -> list[LLMMessage]:
        """
        组装发给 LLM 的完整 messages，含 token 裁剪。
        """
        schemas = self._registry.get_schemas()

        # 生成 repo-map（带缓存：只在第一步生成，之后复用）
        #repo_map.build() 要 rglob 扫整个仓库、给每个源码文件提取符号，预算是15%
        #文件修改后不会实时扫描
        if not hasattr(self, "_repo_map_cache"):
            self._repo_map_cache = repo_map.build(
                budget=token_budget.default_plan().repo_map
            )

        #生成系统提示词
        system_content = build_system_prompt(
            repo_path=getattr(self, "_current_repo_path", "."),
            tools=schemas,
            repo_summary=self._repo_map_cache,
        )

        # 裁剪历史 #可以用fitall替代？
        trimmed_history_dicts = token_budget.trim_history(
            history.to_dicts(),
            token_budget.default_plan().history,
        )

        # 组装：system + 裁剪后的 history
        messages = [LLMMessage(role="system", content=system_content)]
        for d in trimmed_history_dicts:
            messages.append(LLMMessage(role=d["role"], content=d["content"]))
        return messages

    def _format_action_for_history(self, action: Action) -> str:
        """把 Action 格式化为 assistant 消息，写入对话历史。"""
        parts = [f"Thought: {action.thought}"]
        if action.tool_call:
            parts.append(f"Action: {action.tool_call.name}")
            parts.append(f"Params: {json.dumps(action.tool_call.params, ensure_ascii=False)}")
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

    #TODO：每次检查时都要重新构造actions，完全可以再执行的时候遇到actions就添加进入actionslist，然后进行对比呀
    def _is_looping(self, log: EventLog) -> bool:
        """
        检测是否陷入死循环：最近 N 条 action 完全相同。
        比较 (tool_name, params) 元组。
        """
        n = self._cfg.loop_detection_window
        actions = log.get_actions()
        #如果全部action的次数不足就退出，那再get action的时候也可以倒序只取n条呀，
        if len(actions) < n:
            return False

        #倒序取最近n次的
        recent = actions[-n:]
        # 只对 TOOL_CALL 类型做检测 如果这三个不都是 toolcall就退出
        if not all(a.action_type == ActionType.TOOL_CALL for a in recent):
            return False
        if not all(a.tool_call is not None for a in recent):
            return False

        first = recent[0].tool_call
        return all(
            a.tool_call.name == first.name and a.tool_call.params == first.params
            for a in recent[1:]
        )

    #TODO：改进
    def _call_with_retry(
        self,
        messages: list[LLMMessage],#name role id
        tools: list[LLMToolSchema],#name description para
    ):
        """
        带指数退避重试的 LLM 调用。
        stream=True 时走 backend.stream()，否则走 complete()。
        不重试：认证失败（401/403）、参数错误（400）。
        """
        import time as _time

        #最近一次 LLM 调用产生的异常
        last_exc: Exception | None = None
        #退避时长，每次翻倍 初始为2s
        delay = self._cfg.llm_retry_delay

        #llm_max_retries=3,最多翻倍3次，最多调用3次,重试2次，所以时2s->4s->不等待
        for attempt in range(1, self._cfg.llm_max_retries + 1):
            try:
                if self._cfg.stream:#流式
                    #取得流式回调
                    cb = self._cfg.stream_callback
                    thought_cb = self._cfg.thought_callback
                    if hasattr(self._backend, "stream"):
                        #一旦 stream 成功返回，整个 _call_with_retry() 立即结束，不会继续循环。
                        #这里调用的是具体backend的stream，不是基类的stream
                        #Core 不关心当前使用的是 Anthropic、OpenAI-compatible 还是 MockBackend。
                        return self._backend.stream(
                            messages, tools,
                            #接收模型最终回答的文本增量
                            on_text=cb,
                            #接收推理模型的思考内容
                            on_thought=thought_cb,
                        )
                return self._backend.complete(messages, tools)
            except Exception as exc:
                #每次捕获异常后更新
                last_exc = exc
                exc_str = str(exc).lower()
                #对于401/403/认证问题，400/参数错误，直接报错，因为重试没用
                if any(kw in exc_str for kw in (
                    "401", "403", "invalid api key", "authentication",
                    "400", "bad request",
                )):
                    #原样重新抛出当前捕获到的异常，保留原始异常类型和 traceback
                    raise
                #其他问题可以尝试重试，每次重试时长翻倍
                if attempt < self._cfg.llm_max_retries:
                    logger.warning(
                        "LLM call failed (attempt %d/%d): %s — retrying in %.1fs",
                        #第几次尝试/最多尝试次数/异常/等待时间
                        attempt, self._cfg.llm_max_retries, exc, delay,
                    )
                    #Agent Core 刻意保持同步，同步阻塞等待
                    _time.sleep(delay)
                    delay *= 2
        #所有调用都失败 抛出错误
        #raise last_exc  
        if last_exc is None:
            raise RuntimeError("llm_max_retries must be at least 1")
        raise last_exc # type: ignore[misc]

    def _get_git_diff(self, repo_path: str) -> str | None:
        """抓取 git diff HEAD 作为 patch，失败时静默返回 None。"""
        import subprocess
        try:
            #subprocess.run不经过终端，相当于
            # 程序：git
            # 参数：["diff", "HEAD"]
            proc = subprocess.run(
                ["git", "diff", "HEAD"],
                capture_output=True, text=True, timeout=10, cwd=repo_path,
            )
            diff = proc.stdout.strip()
            return diff if diff else None
        except Exception:
            return None


def _default_executor(registry: ToolRegistry) -> "ToolExecutor":
    """
    构造一个透明直通的 ToolExecutor：无 hooks、无 permission，
    行为等价于直接调 registry.execute_tool。延迟 import 避免 agent <-> harness 循环。
    """
    from harness.executor import ToolExecutor
    return ToolExecutor(registry)
