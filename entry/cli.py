"""
entry/cli.py

命令行入口。

解析命令行参数、加载配置、创建 LLM backend、创建工具注册表、创建 Runtime、创建 Agent、创建 Task，然后调用 Agent 执行。
定义了 run、chat、log show、log list 这些子命令。

用法：
    # 直接传任务描述
    python -m entry.cli run --repo /path/to/repo --task "Fix the failing test"

    # 从文件读任务描述
    python -m entry.cli run --repo . --task-file task.txt

    # 覆盖模型
    python -m entry.cli run --repo . --task "fix it" --model deepseek-chat

    # 查看 event log 统计
    python -m entry.cli log show logs/abc123_20240101_120000.jsonl

安装为命令行工具后（pyproject.toml 里配置了 scripts）：
    agent run --repo . --task "fix it"
"""

from __future__ import annotations

import logging
import json
import sys
import time
from pathlib import Path

import click

# 把项目根加入 path（直接跑脚本时需要）
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.schema import load_config, merge_cli_overrides   # noqa: E402
from llm.router import create_backend_from_config            # noqa: E402

# 模块级 import（供 patch 使用）
from config.schema import load_config, merge_cli_overrides  # noqa: E402
from entry.event_renderer import RunEventRenderer          # noqa: E402
from llm.router import create_backend_from_config           # noqa: E402


# ---------------------------------------------------------------------------
# 辅助：彩色输出
# ---------------------------------------------------------------------------

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def _rl_c(text: str, code: str) -> str:
    """readline-safe 着色：给 input() 的提示符用。

    GNU readline 接管 input() 提示符渲染时不识别原始 ANSI ESC，会吞掉 \\033
    字节，导致终端显示成字面的 [35m。用 \\001/\\002 把转义标记为不可打印，
    readline 才会正确忽略它、且不算入行宽。仅用于 prompt，普通 print 不要用。
    """
    if not sys.stdout.isatty():
        return text
    return f"\001\033[{code}m\002{text}\001\033[0m\002"


def _rl_magenta(t: str) -> str: return _rl_c(t, "35")

def green(t: str) -> str:  return _c(t, "32")
def yellow(t: str) -> str: return _c(t, "33")
def red(t: str) -> str:    return _c(t, "31")
def cyan(t: str) -> str:   return _c(t, "36")
def bold(t: str) -> str:   return _c(t, "1")
def dim(t: str) -> str:    return _c(t, "2")
def magenta(t: str) -> str: return _c(t, "35")


# ---------------------------------------------------------------------------
# 构建 agent 各组件
# 把 Agent 能调用的工具统一注册进去
# ---------------------------------------------------------------------------

def _build_registry(
    cfg,
    confirm_callback=None,
    runtime=None,
    default_cwd=None,
    workspace=None,
    *,
    allow_git_mutation: bool = True,
    full_access: bool = False,
):
    """根据配置组装工具注册表。

    default_cwd：shell/test/git 工具的默认工作目录；LLM 显式 cwd 仍可覆盖。
    workspace：文件/搜索工具的独立路径边界，不从 default_cwd 隐式推导。
    confirm_callback 仅保留 registry_builder 兼容；生产确认统一由外层 ToolExecutor 执行。
    """
    from tools.base import ToolRegistry
    from tools.file_tool import FileEditTool, FileReadTool, FileViewTool, FileWriteTool
    from tools.git_tool import GitAddTool, GitCommitTool, GitDiffTool, GitStatusTool
    from tools.search_tool import FindFilesTool, FindSymbolTool, SearchTextTool
    from tools.shell_tool import ShellTool
    from tools.test_tool import PytestTool

    process_cwd = str(default_cwd) if default_cwd else None
    fs_workspace = str(workspace) if workspace else None
    test_config = getattr(getattr(cfg, "tools", None), "test", None)
    python_executable = getattr(cfg, "test_python_executable", None)
    if python_executable is None:
        python_executable = getattr(test_config, "python_executable", None)

    registry = (
        ToolRegistry()
        .register(ShellTool(
            runtime=runtime,
            default_cwd=process_cwd,
            allow_git_mutation=allow_git_mutation,
            full_access=full_access,
        ))
        .register(FileReadTool(workspace=fs_workspace))
        .register(FileViewTool(workspace=fs_workspace))
        .register(FileEditTool(workspace=fs_workspace))
        .register(FileWriteTool(workspace=fs_workspace))
        .register(SearchTextTool(workspace=fs_workspace))
        .register(FindFilesTool(workspace=fs_workspace))
        .register(FindSymbolTool(workspace=fs_workspace))
        .register(PytestTool(
            runtime=runtime,
            default_cwd=process_cwd,
            python_executable=python_executable,
        ))
        .register(GitStatusTool(runtime=runtime, default_cwd=process_cwd))
        .register(GitDiffTool(runtime=runtime, default_cwd=process_cwd))
    )
    if allow_git_mutation:
        registry.register(
            GitAddTool(runtime=runtime, default_cwd=process_cwd)
        ).register(
            GitCommitTool(runtime=runtime, default_cwd=process_cwd)
        )
    return registry


def _build_run_registry(
    cfg,
    confirm_callback=None,
    runtime=None,
    default_cwd=None,
    workspace=None,
    full_access: bool | None = None,
):
    """Build run tools; full approval also enables Git mutation tools."""
    if full_access is None:
        full_access = getattr(cfg, "approval_mode", "ask") == "full"
    return _build_registry(
        cfg,
        confirm_callback=confirm_callback,
        runtime=runtime,
        default_cwd=default_cwd,
        workspace=workspace,
        allow_git_mutation=full_access,
        full_access=full_access,
    )


# ---------------------------------------------------------------------------
# CLI 主命令组
# ---------------------------------------------------------------------------

@click.group()
@click.option(
    "--config", "-c",
    default=None,
    help="Path to config YAML file (default: config/default.yaml)",
)
@click.option("--profile", type=click.Choice(["interactive", "safe", "benchmark", "sandboxed"]), default=None,
              help="Run profile; overrides agent.run_profile in YAML")
@click.option("--multi-agent/--single-agent", default=None,
              help="Enable or disable Supervisor workers")
@click.pass_context
def cli(ctx: click.Context, config: str | None, profile: str | None, multi_agent: bool | None) -> None:
    """Coding Agent — autonomous code editing and bug fixing."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    ctx.obj["run_profile"] = profile
    ctx.obj["multi_agent"] = multi_agent


def _load_cli_config(ctx: click.Context):
    cfg = load_config(ctx.obj.get("config_path"))
    if ctx.obj.get("run_profile"):
        cfg.agent.run_profile = ctx.obj["run_profile"]
    if ctx.obj.get("multi_agent") is not None:
        cfg.agent.multi_agent_enabled = ctx.obj["multi_agent"]
    return cfg


def _resolve_run_approval_mode(approval_mode: str | None, confirm: bool | None) -> str:
    if approval_mode is not None and confirm is not None:
        raise click.UsageError("--approval-mode cannot be combined with --confirm/--no-confirm")
    return approval_mode or ("ask" if confirm is True else "deny" if confirm is False else "auto")


@cli.group("config")
def config_commands() -> None:
    """Inspect the resolved configuration."""


@config_commands.command("show")
@click.option("--provider", default=None)
@click.option("--protocol", default=None)
@click.option("--model", default=None)
@click.option("--max-steps", type=int, default=None)
@click.pass_context
def show_config(ctx: click.Context, provider: str | None, protocol: str | None,
                model: str | None, max_steps: int | None) -> None:
    """Show defaults, YAML, environment substitutions, then CLI overrides."""
    cfg = merge_cli_overrides(_load_cli_config(ctx),
                              provider=provider, protocol=protocol, model=model,
                              max_steps=max_steps)
    data = json.loads(json.dumps(cfg, default=lambda value: vars(value)))
    data["llm"]["api_key"] = "<redacted>" if data["llm"]["api_key"] else ""
    for profile in data["model_profiles"].values():
        profile["api_key"] = "<redacted>" if profile["api_key"] else ""
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# run 子命令
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--repo", "-r", default=".", show_default=True, help="Path to the target repository (default: current directory)")
@click.option("--task", "-t", default=None, help="Task description (natural language)")
@click.option("--task-file", "-f", default=None, help="Read task description from file")
@click.option("--model", "-m", default=None, help="Override LLM model name")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option("--protocol", default=None, help="Override LLM protocol: chat_completions or responses")
@click.option("--max-steps", default=None, type=int, help="Override max steps")
@click.option("--stream/--no-stream", "-s", default=True, help="Enable or disable streaming output (default: on)",)
@click.option(
    "--reasoning-stream/--no-reasoning-stream",
    default=None,
    help="Stream model reasoning independently; defaults to the --stream setting.",
)
@click.option("--confirm/--no-confirm", default=None, help="Legacy aliases for ask/deny approval modes")
@click.option(
    "--approval-mode", type=click.Choice(["ask", "auto", "full"]), default=None,
    help="Run permission mode: ask, auto-approve prompts (default), or full tool access.",
)
@click.option("--sandbox", is_flag=True, default=False, help="Run commands in Docker sandbox (requires Docker)")
@click.option("--isolate", is_flag=True, default=False,help="Run in an isolated git worktree + TaskEngine tracking (M4). Combines with --sandbox for Docker hardening.")
@click.option(
    "--result-policy",
    type=click.Choice(["discard", "keep-if-changed"]),
    default="keep-if-changed",
    show_default=True,
    help="How --isolate handles generated changes.",
)
@click.option("--verbose", "-v", is_flag=True, help="Show debug logs")
@click.pass_context
def run(
    ctx: click.Context,
    repo: str,
    task: str | None,
    task_file: str | None,
    model: str | None,
    provider: str | None,
    protocol: str | None,
    max_steps: int | None,
    stream: bool,
    reasoning_stream: bool | None,
    confirm: bool | None,
    approval_mode: str | None,
    sandbox: bool,
    isolate: bool,
    result_policy: str,
    verbose: bool,
) -> None:
    """Run the coding agent on a repository."""
    # 配置日志
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    # 1.加载配置
    config = _load_cli_config(ctx)
    # 2.加载优先级更高的配置
    config = merge_cli_overrides(
        config, provider=provider, protocol=protocol, model=model, max_steps=max_steps
    )
    if config.agent.run_profile == "sandboxed":
        sandbox = True

    # 解析任务描述 
    if task_file:
        description = Path(task_file).read_text(encoding="utf-8").strip()
    elif task:
        description = task
    elif sys.stdin.isatty():
        description = click.prompt("Task").strip()
        if not description:
            raise click.UsageError("task cannot be blank")
    else:
        click.echo(red("Error: provide --task or --task-file"), err=True)
        sys.exit(1)

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_path}"), err=True)
        sys.exit(1)

    # 打印运行信息
    click.echo(bold(f"\n🤖 Coding Agent"))
    click.echo(f"  Provider : {config.llm.provider}")
    click.echo(f"  Protocol : {config.llm.protocol}")
    click.echo(f"  Model    : {config.llm.model}")
    click.echo(f"  Repo     : {repo_path}")
    click.echo(f"  Max steps: {config.agent.max_steps}\n")

    # 3.构建各组件
    try:
        backend = create_backend_from_config({
            "provider": config.llm.provider,
            "protocol": config.llm.protocol,
            "model":    config.llm.model,
            "api_key":  config.llm.api_key or None,
            "base_url": config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
            "strict_tool_schema": config.llm.strict_tool_schema,
        })
    except ValueError as e:
        click.echo(red(f"Error: {e}"), err=True)
        sys.exit(1)

    from tools.shell_tool import terminal_confirm
    from tools.runtime import CONTAINER_WORKDIR, create_runtime
    resolved_approval_mode = _resolve_run_approval_mode(approval_mode, confirm)
    confirm_cb = (
        terminal_confirm if resolved_approval_mode == "ask" else
        (lambda _prompt: True) if resolved_approval_mode in {"auto", "full"} else None
    )
    click.echo(dim(f"  Approval: {resolved_approval_mode}"))
    #runtime是执行器实例， 有生命周期，后续还需要清理
    runtime = (
        create_runtime(
            sandbox=True,
            repo_path=str(repo_path),
            python_executable=config.tools.test.python_executable,
        )
        if sandbox and not isolate
        else None
    )
    if sandbox:
        runtime_name = runtime.name if runtime is not None else "managed by isolate"
        click.echo(dim(f"  Sandbox: Docker ({runtime_name})"))
    # 注册工具
    registry = _build_run_registry(
        config,
        confirm_callback=confirm_cb,
        runtime=runtime,
        default_cwd=str(repo_path),
        workspace=str(repo_path),
        full_access=resolved_approval_mode == "full",
    )

    from agent.core import AgentConfig
    from agent.event_log import EventLog, summarize_run
    from agent.runner import ExecutionRunner, RunRequest
    from agent.task import Task, infer_completion_requirements, explicitly_requests_tests
    try:
        from context.token_budget import is_tiktoken_available
    except ImportError:
        is_tiktoken_available = lambda: False

    from entry.context_common import build_context_policy, configured_agent_options, new_worker_backend
    context_policy = build_context_policy(config, backend)

    resolved_reasoning_stream = stream if reasoning_stream is None else reasoning_stream
    renderer = RunEventRenderer()

    # 流式回调：最终回答正常亮色
    def _stream_cb(text: str) -> None:
        import sys
        sys.stdout.write(text)
        sys.stdout.flush()
        renderer.record_streamed_text(text)

    # 推理回调：思考过程 dim 暗色
    def _thought_cb(text: str) -> None:
        import sys
        sys.stdout.write(dim(text))
        sys.stdout.flush()


    agent_config = AgentConfig(
        **configured_agent_options(config),
        prepare_next_turn=context_policy,
        planning_mode=config.agent.planning_mode,
        recovery_mode=config.agent.recovery_mode,
        recovery_max_attempts=config.agent.recovery_max_attempts,
        skills_enabled=config.agent.skills_enabled,
        skills_global_dir=config.agent.skills_global_dir,
        skills_max_loaded=config.agent.skills_max_loaded,
        skills_max_chars=config.agent.skills_max_chars,
        skills_reference_max_chars=config.agent.skills_reference_max_chars,
        stream=stream or resolved_reasoning_stream,
        stream_callback=_stream_cb if stream else None,
        thought_callback=_thought_cb if resolved_reasoning_stream else None,
        confirm_dangerous=confirm_cb is not None,
        confirm_callback=confirm_cb,
        approval_mode="full" if resolved_approval_mode == "full" else "ask",
        execution_workspace=CONTAINER_WORKDIR if sandbox else None,
    )
    require_changes, require_tests = infer_completion_requirements(description)
    task_obj = Task(
        description=description,
        repo_path=str(repo_path),
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
        require_changes=require_changes,
        require_tests=explicitly_requests_tests(description),
        inferred_require_tests=require_tests and not explicitly_requests_tests(description),
    )
    runner = ExecutionRunner(
        backend=backend,
        registry=registry,
        config=agent_config,
        log_dir=config.agent.log_dir,
        registry_builder=_build_run_registry,
        confirm_callback=confirm_cb,
        mcp_config=config.mcp,
        worker_backend_factory=lambda role: new_worker_backend(config, role),
    )

    # M4 第二波：--isolate 走 async 组合根（worktree + TaskEngine + permission workspace）
    #事务隔离，在worktree中跑：异步async 不用之前的agent实例，只有 backend、task_obj、agent_config、confirm_cb 被复用
    if isolate:
        from ipc.bus import AgentBus

        #持久化状态
        Path(config.agent.log_dir).mkdir(parents=True, exist_ok=True)
        bus = AgentBus() if verbose else None
        runner.bus = bus
        click.echo(dim("  Isolate: worktree + TaskEngine\n"))

        t0 = time.time()
        try:
            result = runner.run(
                RunRequest(
                    task=task_obj,
                    isolate=True,
                    sandbox=sandbox,
                    result_policy=result_policy,
                ),
                on_event=renderer,
            )
        finally:
            runner.close()
        elapsed = time.time() - t0
        _print_run_result(result, elapsed)
        ctx.exit(0 if result.is_success() else 1)

    if verbose:
        click.echo(dim( f"  tiktoken: {'yes' if is_tiktoken_available() else 'no (char estimate)'}\n"))

    # 直接同步运行
    t0 = time.time()
    try:
        with EventLog.create(task_obj, log_dir=config.agent.log_dir) as log:
            click.echo(dim(f"  Log: {log.path}\n"))
            #真正的执行核心。它的职责包括维护对话历史、组装 messages 调用 LLM、拿到 Action 后执行工具、写入 Action 和 Observation 到 EventLog、检测终止条件和 reflection 条件，最后返回 RunResult。
            result = runner.run(
                RunRequest(task=task_obj), log=log, on_event=renderer,
            )
    finally:
        runner.close()
        if runtime is not None:
            runtime.cleanup()

    elapsed = time.time() - t0
    _print_run_result(result, elapsed)

    sys.exit(0 if result.is_success() else 1)


def _print_run_result(result, elapsed: float) -> None:
    """打印 RunResult 摘要（run 同步路径与 --isolate 路径共用）。"""
    click.echo(bold("─" * 60))
    status_str = green("SUCCESS") if result.is_success() else red(result.status.value.upper())
    click.echo(f"Status  : {status_str}")
    click.echo(f"Steps   : {result.steps_taken}")
    click.echo(f"Tokens  : {result.total_tokens:,}")
    click.echo(f"Time    : {elapsed:.1f}s")
    if result.error:
        click.echo(red(f"Error   : {result.error}"))
    artifact = getattr(result, "worktree", None)
    if artifact is not None:
        click.echo(f"Branch  : {artifact.branch}")
        if artifact.path:
            click.echo(green(f"Worktree: {artifact.path}"))
            click.echo(
                f"Changes : {artifact.uncommitted_count} uncommitted, "
                f"{artifact.commit_count} commit(s)"
            )
        elif artifact.changed_files:
            click.echo(dim("Worktree: removed by result policy"))
        if artifact.warning:
            click.echo(red(f"Warning : {artifact.warning}"))
    click.echo(bold("─" * 60) + "\n")



# ---------------------------------------------------------------------------
# chat 子命令 — 交互对话模式
# ---------------------------------------------------------------------------

def _chat_backend(config):
    llm = config.llm
    return create_backend_from_config({
        "provider": llm.provider, "protocol": llm.protocol, "model": llm.model,
        "api_key": llm.api_key or None, "base_url": llm.base_url or None,
        "max_tokens": llm.max_tokens, "strict_tool_schema": llm.strict_tool_schema,
    })

@cli.command()
@click.option("--repo", "-r", default=".", show_default=True, help="Path to the target repository (default: current directory)")
@click.option("--model", "-m", default=None, help="Override LLM model name")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option("--protocol", default=None, help="Override LLM protocol: chat_completions or responses")
@click.option("--max-steps", default=None, type=int, help="Max steps per round")
@click.option(
    "--stream/--no-stream",
    default=True,
    help="Enable or disable streaming output (default: on)",
)
@click.option(
    "--reasoning-stream/--no-reasoning-stream",
    default=None,
    help="Stream model reasoning independently; defaults to the --stream setting.",
)
@click.option("--sandbox", is_flag=True, default=False, help="Run commands in Docker sandbox (requires Docker)")
@click.option(
    "--continue",
    "continue_session",
    is_flag=True,
    help="Resume the most recently used session for this repository",
)
@click.option("--resume", "resume_session", metavar="SESSION_ID", help="Resume a saved chat session")
@click.option("--no-session", is_flag=True, help="Do not persist or resume chat state")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logs")
@click.pass_context
def chat(
    ctx: click.Context,
    repo: str,
    model: str | None,
    provider: str | None,
    protocol: str | None,
    max_steps: int | None,
    stream: bool,
    reasoning_stream: bool | None,
    sandbox: bool,
    continue_session: bool,
    resume_session: str | None,
    no_session: bool,
    verbose: bool,
) -> None:
    """Interactive chat mode — continuous conversation with the agent."""
    import logging
    from agent.session import ChatSessionConflict, ChatSessionError
    from agent.session_store import JsonChatSessionStore
    from config.schema import save_selected_model, select_model
    from entry.context_common import build_context_policy
    from entry.chat import ChatSession
    from entry.chat_input import chat_prompt
    from dataclasses import replace

    if continue_session and resume_session:
        raise click.UsageError("--continue and --resume cannot be used together")
    if no_session and (continue_session or resume_session):
        raise click.UsageError(
            "--no-session cannot be combined with --continue or --resume"
        )

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    config = _load_cli_config(ctx)
    config = merge_cli_overrides(
        config,
        provider=provider,
        protocol=protocol,
        model=model,
        max_steps=max_steps,
    )

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_path}"), err=True)
        sys.exit(1)

    try:
        backend = _chat_backend(config)
    except ValueError as e:
        click.echo(red(f"Error: {e}"), err=True)
        sys.exit(1)

    from tools.shell_tool import terminal_confirm
    from tools.runtime import CONTAINER_WORKDIR, create_runtime
    runtime = (
        create_runtime(
            sandbox=sandbox,
            repo_path=str(repo_path),
            python_executable=config.tools.test.python_executable,
        )
        if sandbox
        else None
    )
    if sandbox:
        click.echo(dim(f"  Sandbox: Docker ({runtime.name})"))
    registry = _build_registry(
        config,
        confirm_callback=terminal_confirm,
        runtime=runtime,
        default_cwd=str(repo_path),
        workspace=str(repo_path),
    )
    session_store = (
        None
        if no_session
        else JsonChatSessionStore(Path(config.agent.log_dir) / "chat")
    )
    context_policy = build_context_policy(config, backend)
    initial_session_id = resume_session
    continued_existing = False
    try:
        if continue_session and session_store is not None:
            latest = session_store.latest_for_repo(repo_path)
            if latest is not None:
                initial_session_id = latest.session_id
                continued_existing = True
        session = ChatSession(
            backend=backend,
            registry=registry,
            config=config,
            repo_path=str(repo_path),
            log_dir=config.agent.log_dir,
            confirm_callback=terminal_confirm,   # chat 模式默认开启确认
            stream=stream,
            reasoning_stream=reasoning_stream,
            session_store=session_store,
            session_id=initial_session_id,
            prepare_next_turn=context_policy,
            execution_workspace=CONTAINER_WORKDIR if sandbox else None,
        )
    except (ChatSessionError, OSError) as exc:
        if runtime is not None:
            runtime.cleanup()
        raise click.ClickException(str(exc)) from exc

    # 欢迎信息
    click.echo(bold(f"\n🤖 Coding Agent — Chat Mode"))
    click.echo(f"  Provider : {config.llm.provider}")
    click.echo(f"  Protocol : {config.llm.protocol}")
    click.echo(f"  Model    : {config.llm.model}")
    click.echo(f"  Repo     : {repo_path}")
    if getattr(session, "is_persisted", False):
        mode = "resumed" if continued_existing or resume_session else "new"
        click.echo(f"  Session  : {session.session_id} ({mode})")
        click.echo(dim(f"  State    : {session.session_path}"))
    else:
        click.echo("  Session  : ephemeral")
    recovery_warning = getattr(session, "recovery_warning", None)
    if recovery_warning:
        click.echo(yellow(f"  Warning  : {recovery_warning}"))
    click.echo(dim(
        "  Type your task. Commands: "
        "/exit /stats /session /new /resume /rename /clear /help /model\n"
    ))

    prompt_session = chat_prompt(repo_path, config.model_profiles)

    # 启用行编辑：退格、方向键、Ctrl+A/E、历史记录（↑↓）
    try:
        import readline as _rl
        import sys as _sys
        # 检测后端：libedit（某些 Linux/macOS）还是 GNU readline
        _is_libedit = "libedit" in getattr(_rl, "__doc__", "") or (
            hasattr(_rl, "parse_and_bind") and _sys.platform == "darwin"
        )
        # 更可靠的检测：尝试 libedit 特有的绑定语法
        try:
            _rl.parse_and_bind("bind -e")   # libedit 启用 Emacs 模式
            _is_libedit = True
        except Exception:
            _is_libedit = False

        if _is_libedit:
            _rl.parse_and_bind("bind -e")           # Emacs 模式：Ctrl+A/E/K 等
            _rl.parse_and_bind("bind ^I rl_complete")  # Tab 补全
        else:
            _rl.parse_and_bind("set editing-mode emacs")  # GNU readline Emacs 模式
            _rl.parse_and_bind("tab: complete")

        _rl.set_history_length(500)   # 历史记录最多 500 条
    except ImportError:
        pass  # Windows 没有 readline，降级为普通 input

    try:
        # 主 REPL 循环
        while True:
            try:
                # 清理当前行（流式输出后 readline 不知道屏幕上有残留字符）
                # \r 回到行首，\033[2K 清除整行，然后显示提示符
                sys.stdout.write("\r\033[2K")
                sys.stdout.flush()
                user_input = (
                    prompt_session.prompt("you > ") if prompt_session is not None
                    else input(_rl_magenta("you") + " > ")
                ).strip()
            except EOFError:
                click.echo()
                break
            except KeyboardInterrupt:
                click.echo()
                break

            if not user_input:
                continue

            # 内置命令
            if user_input.startswith("/"):
                parts = user_input.split(maxsplit=1)
                cmd = parts[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else ""
                if cmd in ("/exit", "/quit", "/q"):
                    break
                elif cmd == "/stats":
                    session.print_stats()
                elif cmd == "/session":
                    session.print_session_info()
                elif cmd == "/new":
                    new_id = session.start_new_session()
                    if new_id:
                        click.echo(dim(f"  Started new session: {new_id}"))
                    else:
                        click.echo(dim("  Started new ephemeral session."))
                elif cmd == "/resume":
                    if not arg:
                        click.echo(dim("  Usage: /resume SESSION_ID"))
                    else:
                        try:
                            session.resume_session(arg)
                            click.echo(dim(f"  Resumed session: {session.session_id}"))
                            if session.recovery_warning:
                                click.echo(yellow(f"  Warning: {session.recovery_warning}"))
                        except ChatSessionError as exc:
                            click.echo(red(f"  Cannot resume session: {exc}"))
                elif cmd == "/rename":
                    if not arg:
                        click.echo(dim("  Usage: /rename NAME"))
                    else:
                        try:
                            session.rename(arg)
                            click.echo(dim(f"  Session renamed: {arg}"))
                        except (ChatSessionError, ValueError) as exc:
                            click.echo(red(f"  Cannot rename session: {exc}"))
                elif cmd == "/clear":
                    session.clear_history()
                    click.echo(dim("  Conversation history cleared."))
                elif cmd == "/model":
                    if not arg:
                        click.echo(f"  Current: {config.llm.model} ({config.llm.provider})")
                        for name, profile in config.model_profiles.items():
                            click.echo(f"    {name}: {profile.model} ({profile.provider})")
                        click.echo(dim("  Usage: /model NAME_OR_MODEL_ID"))
                    else:
                        try:
                            candidate = replace(config, agent=replace(config.agent))
                            if arg in candidate.model_profiles:
                                select_model(candidate, arg)
                            else:
                                merge_cli_overrides(candidate, model=arg)
                            next_backend = _chat_backend(candidate)
                            config.llm = candidate.llm
                            config.agent.budget_tokens = candidate.agent.budget_tokens
                            session.switch_backend(next_backend)
                            click.echo(green(f"  Model: {config.llm.model} ({config.llm.provider})"))
                            if ctx.obj.get("config_path") is None:
                                try:
                                    save_selected_model(arg, profile=arg in config.model_profiles)
                                except OSError as exc:
                                    click.echo(yellow(f"  Could not save model choice: {exc}"))
                        except ValueError as exc:
                            click.echo(red(f"  Cannot switch model: {exc}"))
                elif cmd == "/help":
                    click.echo(dim(
                        "  Commands:\n"
                        "    /exit   — quit\n"
                        "    /stats  — show session statistics\n"
                        "    /session — show current session id and state path\n"
                        "    /new    — start a new session\n"
                        "    /resume SESSION_ID — switch to a saved session\n"
                        "    /rename NAME — name the current session\n"
                        "    /clear  — clear effective conversation history\n"
                        "    /model [NAME_OR_MODEL_ID] — list or switch models\n"
                        "    /help   — show this help\n"
                        "  Anything else is sent to the agent."
                    ))
                else:
                    click.echo(dim(f"  Unknown command: {user_input}. Type /help for help."))
                continue

            # 运行一轮 agent
            click.echo(dim(f"\n  Agent working..."))
            try:
                session.run_round(user_input)
            except KeyboardInterrupt:
                click.echo(yellow("\n  Interrupted. Type /exit to quit or continue with a new task."))
            except ChatSessionConflict:
                # 冲突时保持 fail-closed，不覆盖另一进程已经写入的新状态。
                click.echo(red("\n  Session conflict: another process saved a newer state; this round was not saved."))
                click.echo(yellow(f"  Tool side effects may be partial. Use /resume {session.session_id} to reload, or /new to start separately."))
            except Exception as e:
                click.echo(red(f"\n  Error: {e}"))
                if verbose:
                    import traceback
                    traceback.print_exc()
    finally:
        session.close()
        if runtime is not None:
            runtime.cleanup()

    session.print_stats()
    click.echo(dim("  Bye!\n"))


# ---------------------------------------------------------------------------
# log 子命令组
# ---------------------------------------------------------------------------

@cli.group()
def log() -> None:
    """Inspect event logs."""


@log.command("show")
@click.argument("log_file")
def log_show(log_file: str) -> None:
    """Show a summary of an event log file."""
    from agent.event_log import EventLog, summarize_run

    path = Path(log_file)
    if not path.exists():
        click.echo(red(f"File not found: {path}"), err=True)
        sys.exit(1)

    with EventLog.open_existing(path) as elog:
        events = elog.replay()
        stats = summarize_run(elog)

    click.echo(bold(f"\nEvent Log: {path.name}"))
    click.echo(f"  Total events : {stats['total_events']}")
    click.echo(f"  Actions      : {stats['actions']}")
    click.echo(f"  Reflections  : {stats['reflections']}")
    click.echo(f"  Tool calls   : {stats['tool_calls']}")
    click.echo(f"  Final status : {stats['final_status']}\n")

    click.echo(bold("Events:"))
    for event in events:
        ts = event.timestamp[11:19]   # HH:MM:SS
        etype = event.event_type.value
        detail = ""
        if event.event_type.value == "action":
            tc = event.payload.get("action", {}).get("tool_call")
            detail = f"  tool={tc['name']}" if tc else ""
        elif event.event_type.value == "observation":
            obs = event.payload.get("observation", {})
            detail = f"  status={obs.get('status')}"
        click.echo(f"  {ts}  {etype:<16}{detail}")


@log.command("list")
@click.option("--dir", "log_dir", default="./logs", help="Log directory")
def log_list(log_dir: str) -> None:
    """List all event log files."""
    log_path = Path(log_dir)
    if not log_path.exists():
        click.echo(f"Log directory not found: {log_path}")
        return

    files = sorted(log_path.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        click.echo("No log files found.")
        return

    click.echo(bold(f"\nLog files in {log_path}:\n"))
    for f in files:
        size_kb = f.stat().st_size / 1024
        click.echo(f"  {f.name}  ({size_kb:.1f} KB)")
    click.echo()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
