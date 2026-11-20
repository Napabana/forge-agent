"""
entry/chat.py

交互对话模式。持续会话，每轮用户输入后 agent 继续工作，
history 跨轮保留，像 Claude Code 一样可以持续对话。

架构设计：
- ChatSession 持有 backend / registry / ConversationHistory，在当前进程内跨轮复用
- 每轮创建一个新 Task，并通过 Agent.run(..., history=history) 正式传入共享历史
- EventLog 每轮独立（方便单轮审计），但统计累计显示
- 实时打印通过 EventLog.on_append() 观察事件，只负责显示，不参与记忆传递
- 可选 JsonChatSessionStore 在每轮前后原子保存 history、统计和轮次元数据
- EventLog 仍是单轮审计记录；会话 state.json 才是 Chat 恢复点

用法：
    agent chat --repo /path/to/repo
    agent chat --repo . --model deepseek-chat
"""

from __future__ import annotations

import time
import sys
from pathlib import Path
from typing import Callable

import click

from entry.event_renderer import RunEventRenderer

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# 彩色输出（复用 cli.py 的风格）
# ---------------------------------------------------------------------------

def _c(t: str, code: str) -> str:
    return f"\033[{code}m{t}\033[0m" if sys.stdout.isatty() else t

def green(t: str) -> str:  return _c(t, "32")
def yellow(t: str) -> str: return _c(t, "33")
def red(t: str) -> str:    return _c(t, "31")
def cyan(t: str) -> str:   return _c(t, "36")
def bold(t: str) -> str:   return _c(t, "1")
def dim(t: str) -> str:    return _c(t, "2")
def magenta(t: str) -> str: return _c(t, "35")


def _format_usage(usage) -> str:
    cache_hit_rate = (
        usage.cached_tokens / usage.input_tokens if usage.input_tokens else 0.0
    )
    suffix = f", estimated {usage.estimated_calls}" if usage.estimated_calls else ""
    legacy = (
        f", legacy {usage.unattributed_tokens:,}"
        if usage.unattributed_tokens else ""
    )
    return (
        f"{usage.total_tokens:,} tokens "
        f"({usage.llm_calls} calls; input {usage.input_tokens:,}, "
        f"cached {usage.cached_tokens:,}, cache-write {usage.cache_write_tokens:,}, "
        f"output {usage.output_tokens:,}, reasoning {usage.reasoning_tokens:,}"
        f", cache-hit {cache_hit_rate:.1%}{suffix}{legacy})"
    )


def _repository_revision(repo_path: str | Path) -> str:
    """复用 Context 层统一的 HEAD + working-tree fingerprint。"""
    from context.repository_state import repository_fingerprint

    return repository_fingerprint(repo_path)


_compat_renderer = RunEventRenderer(preview_lines=20, compact=True, show_task=False)


def _print_event_live(event) -> None:
    """Compatibility wrapper; product code uses a per-session shared renderer."""
    _compat_renderer(event)


# ---------------------------------------------------------------------------
# ChatSession — 跨轮共享并可选持久化的会话状态
# ---------------------------------------------------------------------------

class ChatSession:
    """
    Chat 会话。ChatSession 实例存活期间跨轮保留：
    - backend / registry（不变）
    - ConversationHistory（核心：让 agent 记得之前做了什么）
    - 累计 token / 步数统计
    - repo_map 缓存（仓库指纹变化时自动失效）

    传入 session_store 后，ConversationHistory、轮次和统计会原子落盘，可在
    新进程中恢复。每轮 EventLog 仍独立保存，负责审计而不是充当恢复真相源。
    """

    def __init__(
        self,
        backend,
        registry,
        config,
        repo_path: str,
        log_dir: str,
        confirm_callback=None,
        stream: bool = True,
        reasoning_stream: bool | None = None,
        session_store=None,
        session_id: str | None = None,
        prepare_next_turn=None,
        execution_workspace: str | None = None,
    ) -> None:
        from agent.core import AgentConfig
        from entry.context_common import configured_agent_options, new_worker_backend
        from agent.runner import ExecutionRunner
        from context.history import ConversationHistory

        self.repo_path = repo_path
        self.log_dir = log_dir
        self.config = config
        self._confirm_callback = confirm_callback
        self._session_store = session_store
        self._prepare_next_turn = prepare_next_turn
        bind_backend = getattr(self._prepare_next_turn, "bind_backend", None)
        if callable(bind_backend):
            bind_backend(backend)
        self._state = None
        self.recovery_warning: str | None = None
        self._history_max_messages = config.context.history_window * 2
        resolved_reasoning_stream = stream if reasoning_stream is None else reasoning_stream
        self._event_renderer = RunEventRenderer(
            preview_lines=20, compact=True, show_task=False,
        )

        # 流式回调：每个 token 立刻 flush 到终端
        _stream_started = [False]
        _thought_printed = [False]  # 标记是否打过 thought，用于 message 前换行

        def _thought_cb(text: str) -> None:
            """推理过程：dim 暗色，表示模型在思考"""
            import sys
            if not _stream_started[0]:
                sys.stdout.write("\r  ")
                sys.stdout.flush()
                _stream_started[0] = True
            sys.stdout.write(dim(text))
            sys.stdout.flush()
            _thought_printed[0] = True

        def _stream_cb(text: str) -> None:
            """最终回答：正常亮色"""
            import sys
            if not _stream_started[0]:
                # 第一次打 message，之前没打过任何内容
                sys.stdout.write("\r  ")
                sys.stdout.flush()
                _stream_started[0] = True
            elif _thought_printed[0]:
                # 之前打过 thought，先换两行作为分隔再打 message
                sys.stdout.write("\n\n")
                sys.stdout.flush()
                _thought_printed[0] = False  # 只换一次
            sys.stdout.write(text)
            sys.stdout.flush()
            self._event_renderer.record_streamed_text(text)

        agent_cfg = AgentConfig(
            **configured_agent_options(config),
            planning_mode=config.agent.planning_mode,
        recovery_mode=config.agent.recovery_mode,
        recovery_max_attempts=config.agent.recovery_max_attempts,
        skills_enabled=config.agent.skills_enabled,
        skills_global_dir=config.agent.skills_global_dir,
        skills_max_loaded=config.agent.skills_max_loaded,
        skills_max_chars=config.agent.skills_max_chars,
        skills_reference_max_chars=config.agent.skills_reference_max_chars,
            llm_max_retries=3,
            llm_retry_delay=1.0,
            stream=stream or resolved_reasoning_stream,
            stream_callback=_stream_cb if stream else None,
            thought_callback=_thought_cb if resolved_reasoning_stream else None,
            confirm_dangerous=confirm_callback is not None,
            confirm_callback=confirm_callback,
            prepare_next_turn=prepare_next_turn,
            execution_workspace=execution_workspace,
        )
        self.runner = ExecutionRunner(
            backend=backend,
            registry=registry,
            config=agent_cfg,
            log_dir=log_dir,
            confirm_callback=confirm_callback,
            mcp_config=config.mcp,
            worker_backend_factory=lambda role: new_worker_backend(config, role),
        )
        self.agent = self.runner.agent
        self._shared_history = ConversationHistory(
            max_messages=self._history_max_messages
        )
        self._repo_revision = _repository_revision(self.repo_path)

        # 累计统计
        from llm.usage import SessionUsage
        self.usage = SessionUsage()
        self.total_tokens = 0
        self.total_steps = 0
        self.round_count = 0

        if session_id is None:
            reset_lineage = getattr(
                self._prepare_next_turn,
                "reset_checkpoint_lineage",
                None,
            )
            if callable(reset_lineage):
                reset_lineage()

        if self._session_store is not None:
            if session_id:
                self._restore_session(session_id)
            else:
                self._state = self._session_store.create(self.repo_path)
                self._checkpoint()

    def run_round(self, user_input: str, *, resume: bool = False) -> bool:
        """
        执行一轮对话。

        Args:
            user_input: 用户这轮的输入

        Returns:
            True 表示成功/正常结束，False 表示失败
        """
        from agent.event_log import EventLog
        from agent.session import ChatRoundState, PendingRoundState
        from uuid import uuid4
        from agent.session_store import utc_now
        from agent.task import Task
        from llm.base import LLMMessage

        # 用户可能在两轮之间手动改仓库；先检查再构建本轮 Repo Map。
        current_revision = _repository_revision(self.repo_path)
        if current_revision != self._repo_revision:
            self.agent.invalidate_repo_map_cache(self.repo_path)
            self._repo_revision = current_revision

        prior_round = self._state.rounds[-1] if resume and self._state and self._state.rounds else None
        if resume and (prior_round is None or prior_round.status != "interrupted" or not prior_round.run_id):
            raise ValueError("This session has no interrupted Durable Run to resume.")
        round_number = prior_round.round_number if prior_round else self.round_count + 1

        # 把用户输入追加到共享 history
        if not resume:
            self._shared_history.add(LLMMessage(role="user", content=user_input))

        # 构建这轮的 Task（repo_path 固定，description 用用户输入）
        task = Task(
            description=user_input,
            repo_path=self.repo_path,
            max_steps=self.config.agent.max_steps,
            budget_tokens=self.config.agent.budget_tokens,
        )

        t0 = time.time()
        settings = self.runner.config.durable_runtime_config
        run_id = uuid4().hex if settings and settings.enabled else None
        if prior_round:
            import json
            from durable_runtime.store import DurableRuntimeStore
            from project_memory.identity import ProjectIdentityResolver

            if not settings or not settings.enabled:
                raise ValueError("Set durable_runtime.enabled to resume this round.")
            run_id = prior_round.run_id
            user_input = prior_round.user_input
            with DurableRuntimeStore(ProjectIdentityResolver.resolve(self.repo_path), settings.root,
                                     timeout_ms=settings.busy_timeout_ms) as store:
                row = store._conn.execute("SELECT task_snapshot_json FROM runs WHERE run_id=? AND scope_id=?",
                                          (run_id, self.session_id)).fetchone()
            if row is None:
                raise ValueError("The interrupted Run is not present in this session.")
            task = Task(**json.loads(row[0]))
            if Path(task.repo_path).resolve() != Path(self.repo_path).resolve():
                raise ValueError("The interrupted Run belongs to a different checkout.")
        previous_history = self._shared_history.to_list() if resume else None
        started_at = utc_now()
        log_dir = (
            str(self._session_store.round_log_dir(self._state))
            if self._session_store is not None and self._state is not None
            else self.log_dir
        )
        try:
            with EventLog.create(task, log_dir=log_dir, session_id=self.session_id) as log:
                self.round_count = round_number
                if self._state is not None:
                    self._state.pending_round = PendingRoundState(
                        round_number=round_number,
                        task_id=task.task_id,
                        user_input=user_input,
                        started_at=started_at,
                        log_path=str(log.path),
                        run_id=run_id,
                        scope_id=(self.session_id or run_id) if run_id else None,
                    )
                    # 在任何 LLM 或工具调用前写恢复点；保存失败则不开始执行。
                    self._checkpoint()
                # 实时打印只观察 EventLog；共享记忆由 Agent.run(history=...) 传入。
                try:
                    result = self._run_with_live_print(task, log, run_id=run_id, resume=resume) if run_id else self._run_with_live_print(task, log)
                except BaseException as exc:
                    self._merge_interrupted_context_usage()
                    if resume:
                        self._shared_history.replace(previous_history)
                        self._state.pending_round = None
                        self._checkpoint()
                        raise
                    if self._state is not None:
                        self._shared_history.add(LLMMessage(
                            role="assistant",
                            content=(
                                f"[Round {round_number} interrupted]\n"
                                "The previous run did not complete. Inspect the repository "
                                "and round log before assuming tool side effects succeeded."
                            ),
                        ))
                        self._state.rounds.append(ChatRoundState(
                            round_number=round_number,
                            task_id=task.task_id,
                            user_input=user_input,
                            status="interrupted",
                            log_path=str(log.path),
                            started_at=started_at,
                            finished_at=utc_now(),
                            error=f"{type(exc).__name__}: {exc}",
                            run_id=run_id,
                        ))
                        self._state.pending_round = None
                        self._checkpoint()
                    raise
        finally:
            # 本轮若修改或提交了代码，让下一轮重新生成 Repo Map。
            current_revision = _repository_revision(self.repo_path)
            if current_revision != self._repo_revision:
                self.agent.invalidate_repo_map_cache(self.repo_path)
                self._repo_revision = current_revision

        elapsed = time.time() - t0
        self.usage.add(result.usage)
        self.total_tokens = self.usage.total_tokens
        self.total_steps += result.steps_taken

        # 把 agent 这轮的最后回复追加到共享 history
        # 这样下一轮 agent 能看到自己上一轮说了什么
        if result.summary:
            self._shared_history.add(LLMMessage(
                role="assistant",
                content=f"[Round {self.round_count} complete]\n{result.summary}",
            ))

        if self._state is not None:
            if prior_round:
                self._state.rounds.pop()
            self._state.rounds.append(ChatRoundState(
                round_number=round_number,
                task_id=task.task_id,
                user_input=user_input,
                status=result.status.value,
                summary=result.summary or "",
                log_path=str(log.path),
                steps=result.steps_taken,
                tokens=result.total_tokens,
                usage=result.usage.snapshot(),
                started_at=started_at,
                finished_at=utc_now(),
                error=result.error,
                run_id=run_id,
                history_digest=self._session_store.save_history(self._state, self._shared_history.to_dicts()),
            ))
            self._state.pending_round = None
            self._checkpoint()

        # 流式输出结束后打换行，重置 readline 的行状态
        import sys as _sys
        _sys.stdout.write("\n")
        _sys.stdout.flush()

        # 打印轮次统计
        click.echo(dim(
            f"  ─── Round {self.round_count} · "
            f"{result.steps_taken} steps · "
            f"{_format_usage(result.usage)} · "
            f"{elapsed:.1f}s ───"
        ))

        return result.is_success() or result.status.value == "gave_up"

    @property
    def is_persisted(self) -> bool:
        return self._session_store is not None and self._state is not None

    @property
    def session_id(self) -> str | None:
        return self._state.session_id if self._state is not None else None

    @property
    def session_path(self) -> Path | None:
        if self._session_store is None or self._state is None:
            return None
        return self._session_store.state_path(self._state)

    def clear_history(self) -> None:
        """清空当前会话的有效 LLM 上下文并立即保存。"""
        store = self._durable_archive_store()
        if store is not None:
            from context.archive import ContextArchive

            with store:
                owner = store.claim(self.session_id)
                archive = ContextArchive(store, self.session_id)
                with store.transaction(owner, owner.revision) as db:
                    archive.capture_history(db, self._shared_history.to_dicts())
                    archive.clear(db)
        self._shared_history.clear()
        reset_lineage = getattr(
            self._prepare_next_turn,
            "reset_checkpoint_lineage",
            None,
        )
        if callable(reset_lineage):
            reset_lineage()
        self._checkpoint()

    def _durable_archive_store(self):
        settings = self.runner.config.durable_runtime_config
        if not self.session_id or not settings or not settings.enabled or not settings.archive_enabled:
            return None
        from durable_runtime.store import DurableRuntimeStore
        from project_memory.identity import ProjectIdentityResolver

        return DurableRuntimeStore(ProjectIdentityResolver.resolve(self.repo_path), settings.root,
                                   timeout_ms=settings.busy_timeout_ms)

    def show_runtime(self, command: str, archive_id: str | None = None) -> None:
        import json
        from context.archive import ContextArchive
        from durable_runtime.store import StoreFormatError

        store = self._durable_archive_store()
        if store is None:
            click.echo("This session has no durable archive.")
            return
        try:
            with store:
                if command == "/archive":
                    archive = ContextArchive(store, self.session_id)
                    rows = archive.read(archive_id, 0, 4096) if archive_id else archive.records()[-20:]
                elif command == "/cache":
                    rows = [dict(row) for row in store._conn.execute(
                        """SELECT a.request_id, a.model, a.prompt_tokens, a.cached_tokens, a.changed_components_json
                           FROM cache_audit_events a JOIN runs r ON r.run_id=a.run_id
                           WHERE r.scope_id=? ORDER BY a.created_at DESC LIMIT 20""", (self.session_id,),
                    )]
                else:
                    rows = [dict(row) for row in store._conn.execute(
                        """SELECT run_id, operation_id, name, state FROM operations WHERE scope_id=?
                           AND state IN ('started', 'outcome_unknown') ORDER BY created_at DESC LIMIT 20""",
                        (self.session_id,),
                    )]
                click.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        except (ValueError, StoreFormatError) as exc:
            click.echo(f"Runtime state could not be read: {exc}", err=True)

    def switch_backend(self, backend) -> None:
        """Use the new backend from the next round while retaining this session's history."""
        self.runner.backend = backend
        self.runner.agent._backend = backend
        self.agent = self.runner.agent
        self.runner.config.budget_tokens = self.config.agent.budget_tokens
        bind_backend = getattr(self._prepare_next_turn, "bind_backend", None)
        if callable(bind_backend):
            bind_backend(backend)

    def start_new_session(self) -> str | None:
        """结束当前上下文并创建一个全新的 Chat 会话。"""
        from context.history import ConversationHistory

        reset_lineage = getattr(
            self._prepare_next_turn,
            "reset_checkpoint_lineage",
            None,
        )
        if callable(reset_lineage):
            reset_lineage()
        self._shared_history = ConversationHistory(
            max_messages=self._history_max_messages
        )
        self.total_tokens = 0
        from llm.usage import SessionUsage
        self.usage = SessionUsage()
        self.total_steps = 0
        self.round_count = 0
        self.recovery_warning = None
        self.agent.invalidate_repo_map_cache(self.repo_path)
        self._repo_revision = _repository_revision(self.repo_path)
        if self._session_store is not None:
            self._state = self._session_store.create(self.repo_path)
            self._checkpoint()
        else:
            self._state = None
        return self.session_id

    def resume_session(self, session_id: str) -> None:
        """切换到同一仓库的一条已保存会话。"""
        if self._session_store is None:
            from agent.session import ChatSessionError

            raise ChatSessionError("session persistence is disabled")
        self._restore_session(session_id)
        self.agent.invalidate_repo_map_cache(self.repo_path)

    def rewind(self, round_number: int) -> str:
        """Fork this Chat at a completed round and switch to the new session."""
        from dataclasses import replace
        from uuid import uuid4
        from agent.session import ChatSessionError
        from agent.session_store import utc_now
        from context.archive import ContextArchive
        from context.history import ConversationHistory
        from llm.usage import SessionUsage

        if not self.is_persisted:
            raise ChatSessionError("session persistence is disabled")
        source = self._session_store.load(self.session_id)
        target = next((item for item in source.rounds if item.round_number == round_number), None)
        if source.pending_round or target is None or target.status == "interrupted" or not target.finished_at:
            raise ChatSessionError("select a finished round in an inactive session")
        history = (self._session_store.load_history(source, target.history_digest) if target.history_digest
                   else self._legacy_rewind_history(source, target))
        history = ConversationHistory.from_dicts(history).to_dicts()
        checksum = self._session_store.save_history(source, history)
        rounds = [
            replace(item, origin_session_id=item.origin_session_id or source.session_id,
                    history_digest=checksum if item.round_number == round_number else item.history_digest)
            for item in source.rounds if item.round_number <= round_number
        ]
        usage = SessionUsage()
        for item in rounds:
            usage.add(item.usage)
        state = replace(
            source, session_id=uuid4().hex[:12], created_at=utc_now(), revision=0, history=history,
            forked_from_session_id=source.session_id, forked_from_round=round_number,
            pending_round=None, rounds=rounds, round_count=round_number, compaction_checkpoints=[],
            total_steps=sum(item.steps for item in rounds), total_tokens=usage.total_tokens, usage=usage,
            repo_revision=_repository_revision(self.repo_path),
        )
        store = self._durable_archive_store()
        if store is not None:
            with store:
                owner = store.claim(state.session_id)
                archive = ContextArchive(store, state.session_id, session_id=state.session_id)
                with store.transaction(owner, owner.revision) as db:
                    imported = archive.import_history(db, history, source.session_id)
                    archive._append(db, "boundary", {"reason": "chat rewind", "source_session_id": source.session_id,
                                                     "source_round": round_number}, [imported])
        self._session_store.save(state)
        self.resume_session(state.session_id)
        return state.session_id

    def _legacy_rewind_history(self, session, round_state) -> list[dict]:
        from agent.session import ChatSessionError
        from durable_runtime.run_manager import checked_payload

        if round_state.run_id:
            store = self._durable_archive_store()
            if store is not None:
                with store:
                    row = store._conn.execute(
                        """SELECT c.payload_json, c.payload_digest FROM runs r JOIN run_checkpoints c
                           ON c.run_id=r.run_id AND r.checkpoint_id=c.checkpoint_id
                           WHERE r.run_id=? AND r.task_id=? AND r.scope_id=? AND c.kind='terminal'""",
                        (round_state.run_id, round_state.task_id, round_state.origin_session_id or session.session_id),
                    ).fetchone()
                    if row:
                        history = checked_payload(*row).get("history")
                        if isinstance(history, list):
                            if round_state.summary:
                                history.append({"role": "assistant",
                                                "content": f"[Round {round_state.round_number} complete]\n{round_state.summary}"})
                            return history
        raise ChatSessionError("this older round has no verified history checkpoint")

    def rename(self, title: str) -> None:
        if self._state is None:
            from agent.session import ChatSessionError

            raise ChatSessionError("session persistence is disabled")
        title = title.strip()
        if not title:
            raise ValueError("session title cannot be empty")
        self._state.title = title
        self._checkpoint()

    def print_session_info(self) -> None:
        if self._state is None:
            click.echo("  Session  : ephemeral (--no-session)")
            return
        click.echo(f"  Session  : {self._state.session_id}")
        click.echo(f"  Title    : {self._state.title or '-'}")
        click.echo(f"  State    : {self.session_path}")
        click.echo(f"  Rounds   : {self.round_count}")
        if self._state.forked_from_session_id:
            click.echo(f"  Rewound  : {self._state.forked_from_session_id} round {self._state.forked_from_round}")

    def _restore_session(self, session_id: str) -> None:
        from agent.session import (
            ChatRoundState,
            ChatSessionRepoMismatch,
        )
        from agent.session_store import repo_key_for_path, utc_now
        from context.history import ConversationHistory
        from llm.base import LLMMessage

        state = self._session_store.load(session_id)
        current_key = repo_key_for_path(self.repo_path)
        if state.repo_key != current_key:
            raise ChatSessionRepoMismatch(
                f"session {session_id} belongs to another repository"
            )

        self._state = state
        self._shared_history = ConversationHistory.from_dicts(
            state.history,
            max_messages=self._history_max_messages,
        )
        self.usage = state.usage.snapshot()
        self.total_tokens = self.usage.total_tokens
        self.total_steps = state.total_steps
        self.round_count = state.round_count
        self._repo_revision = state.repo_revision or _repository_revision(self.repo_path)
        self.recovery_warning = None

        restore_lineage = getattr(
            self._prepare_next_turn,
            "restore_checkpoint_lineage",
            None,
        )
        if callable(restore_lineage):
            previous_checkpoint_id = None
            if state.compaction_checkpoints:
                previous_checkpoint_id = state.compaction_checkpoints[-1].get(
                    "checkpoint_id"
                )
            restore_lineage(previous_checkpoint_id)

        if not state.pending_round and state.rounds and state.rounds[-1].run_id:
            self._restore_compacted_view(state.rounds[-1].run_id)

        pending = state.pending_round
        if pending is not None:
            self.round_count = max(self.round_count, pending.round_number)
            self.recovery_warning = (
                f"Recovered interrupted round {pending.round_number} "
                f"(task {pending.task_id}); inspect {pending.log_path} and the "
                "repository before retrying."
            )
            if pending.run_id:
                self.recovery_warning += f" Durable Run ID: {pending.run_id}."
            self._shared_history.add(LLMMessage(
                role="assistant",
                content=(
                    f"[Round {pending.round_number} interrupted]\n"
                    "The process stopped before the round completed. Tool side effects "
                    "may be partial; inspect the repository and round log before retrying."
                ),
            ))
            state.rounds.append(ChatRoundState(
                round_number=pending.round_number,
                task_id=pending.task_id,
                user_input=pending.user_input,
                status="interrupted",
                log_path=pending.log_path,
                started_at=pending.started_at,
                finished_at=utc_now(),
                error="process ended before the post-round checkpoint",
                run_id=pending.run_id,
            ))
            state.pending_round = None

        state.repo_path = str(Path(self.repo_path).resolve())
        self._checkpoint()

    def _restore_compacted_view(self, run_id: str) -> None:
        from context.archive import ContextArchive
        from context.compaction import TraceableCompaction
        from durable_runtime.run_manager import checked_payload
        from durable_runtime.store import StoreFormatError

        policy = self._prepare_next_turn
        if not isinstance(policy, TraceableCompaction):
            return
        store = self._durable_archive_store()
        if store is None:
            return
        try:
            with store:
                row = store._conn.execute(
                    """SELECT c.payload_json, c.payload_digest FROM runs r JOIN run_checkpoints c
                       ON c.run_id=r.run_id AND c.checkpoint_id=r.checkpoint_id
                       WHERE r.run_id=? AND r.scope_id=? AND r.status='completed'""",
                    (run_id, self.session_id),
                ).fetchone()
                if row is None:
                    return
                saved = checked_payload(*row)
                compacted = saved.get("compaction_state")
                archive = ContextArchive(store, self.session_id)
                revision, _ = archive.verified_projection("model_view")
                if compacted and revision and revision == saved.get("model_archive_revision"):
                    # The completed round has already merged the compaction call usage.
                    policy.restore({**compacted, "pending_usage": {}}, self._shared_history)
        except (StoreFormatError, ValueError) as exc:
            self.recovery_warning = f"Saved compaction view could not be verified: {exc}"

    def _checkpoint(self) -> None:
        if self._session_store is None or self._state is None:
            return
        self._state.repo_path = str(Path(self.repo_path).resolve())
        self._state.history = self._shared_history.to_dicts()
        self._state.round_count = self.round_count
        self._state.total_steps = self.total_steps
        self._state.total_tokens = self.total_tokens
        self._state.usage = self.usage.snapshot()
        self._state.repo_revision = self._repo_revision
        checkpoints = getattr(self._prepare_next_turn, "checkpoints", ())
        known = {item.get("checkpoint_id") for item in self._state.compaction_checkpoints}
        self._state.compaction_checkpoints.extend(
            checkpoint.to_dict()
            for checkpoint in checkpoints
            if checkpoint.checkpoint_id not in known
        )
        self._session_store.save(self._state)

    def _merge_interrupted_context_usage(self) -> None:
        """中断时也保存已发生的 semantic side-call usage，避免统计泄漏。"""
        consume = getattr(self._prepare_next_turn, "consume_usage", None)
        if not callable(consume):
            return
        side_usage = consume()
        if not side_usage.llm_calls and not side_usage.total_tokens:
            return
        self.usage.add(side_usage)
        self.total_tokens = self.usage.total_tokens

    def _run_with_live_print(self, task, log, *, run_id=None, resume=False):
        """
        运行 agent，同时实时打印 event。

        Agent.run() 是同步的，但 EventLog 每次 append 后会调用公开观察者。
        这个回调只影响终端显示，不读取或修改 ConversationHistory。
        """
        from agent.runner import RunRequest

        self._event_renderer.reset()

        return self.runner.run(
            RunRequest(
                task=task,
                history=self._shared_history,
                prepare_next_turn=self._prepare_next_turn,
                session_id=self.session_id,
                run_id=run_id,
                resume=resume,
            ),
            log=log,
            on_event=self._event_renderer,
        )

    def close(self) -> None:
        """Close external capability connections owned by this chat session."""
        self.runner.close()

    def print_stats(self) -> None:
        """打印会话总统计。"""
        click.echo(bold(f"\n{'─'*50}"))
        click.echo(f"  Session stats:")
        click.echo(f"    Rounds  : {self.round_count}")
        click.echo(f"    Steps   : {self.total_steps}")
        click.echo(f"    Tokens  : {_format_usage(self.usage)}")
        click.echo(bold(f"{'─'*50}\n"))
