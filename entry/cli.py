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
import re
import sys
import time
from dataclasses import replace
from pathlib import Path

import click
import yaml

# 把项目根加入 path（直接跑脚本时需要）
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.schema import load_config, merge_cli_overrides  # noqa: E402
from llm.router import create_backend_from_config           # noqa: E402


def _terminal_logging(ctx, ui, verbose):
    """Let Rich print diagnostics above Live, and keep redirected diagnostics on stderr."""
    root = logging.getLogger()
    if root.handlers:
        return
    if ui.rich:
        from rich.logging import RichHandler
        handler = RichHandler(console=ui.console, show_path=False, markup=False)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s — %(message)s"))
    previous_level = root.level
    root.setLevel(logging.DEBUG if verbose else logging.WARNING)
    root.addHandler(handler)

    @ctx.call_on_close
    def restore():
        root.removeHandler(handler)
        root.setLevel(previous_level)
        handler.close()


# ---------------------------------------------------------------------------
# 辅助：彩色输出
# ---------------------------------------------------------------------------

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
    help="Config YAML (default: ~/.config/forge-agent/config.yaml when present)",
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


@cli.command("init")
@click.option("--provider")
@click.option("--model")
@click.option("--protocol", type=click.Choice(["chat_completions", "responses", "messages"]))
@click.option("--base-url")
@click.option("--api-key-env")
@click.option("--keyring", "use_keyring", is_flag=True, help="Store a prompted key in the system keyring.")
@click.option("--context-window", type=click.IntRange(min=1))
@click.option("--max-output-tokens", type=click.IntRange(min=1))
@click.option("--repo", default=".", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--non-interactive", is_flag=True)
@click.option("--force", is_flag=True)
@click.pass_context
def init_config(ctx, provider, model, protocol, base_url, api_key_env, context_window, max_output_tokens, repo,
                non_interactive, force, use_keyring):
    """Create or update the user model configuration after a small validation request."""
    from entry.onboarding import initialize

    path = _user_config_path(ctx)
    grant_project_skills = False
    if path.exists() and not force:
        raise click.ClickException("Configuration already exists. Use --force to update it.")
    if use_keyring and (api_key_env or non_interactive):
        raise click.UsageError("--keyring requires interactive credential entry")
    if non_interactive:
        if not provider or not model:
            raise click.UsageError("--non-interactive requires --provider and --model")
        key = None
    else:
        provider = provider or click.prompt("Provider", default="openai")
        model = model or click.prompt("Model")
        key = click.prompt("API key", hide_input=True) if not api_key_env and provider != "ollama" else None
        if (repo / ".agents" / "skills").is_dir():
            from project_memory.identity import ProjectIdentityResolver

            identity = ProjectIdentityResolver.resolve(repo)
            grant_project_skills = click.confirm(f"Trust project Skills from {repo.resolve()} for {identity.key}?")
        if not click.confirm("Send a small request to validate the model and save this configuration?"):
            raise click.Abort()
    try:
        initialize(path, provider=provider, model=model, protocol=protocol,
                   base_url=base_url, key_env=api_key_env, key=key, force=force,
                   context_window=context_window, max_output_tokens=max_output_tokens, use_keyring=use_keyring)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise click.ClickException(f"Configuration could not be saved ({type(exc).__name__}).") from None
    click.echo(f"Configured {provider}/{model} in {path}")
    if grant_project_skills:
        _workspace_command(ctx, repo, scope="project_skills", grant=True)
    if not non_interactive:
        from entry.onboarding import environment_summary

        click.echo(json.dumps(environment_summary(_load_cli_config(ctx), path, repo), ensure_ascii=False))


def _user_config_path(ctx: click.Context) -> Path:
    selected = ctx.obj.get("config_path")
    return Path(selected).expanduser() if selected else Path.home() / ".config" / "forge-agent" / "config.yaml"


def _maybe_onboard(ctx: click.Context, repo: str) -> None:
    if ctx.obj.get("config_path") is None and sys.stdin.isatty() and not _user_config_path(ctx).exists():
        defaults = _load_cli_config(ctx)
        if defaults.onboarding_enabled and defaults.onboarding_auto_start and click.confirm("Create a user model configuration now?", default=True):
            ctx.invoke(init_config, repo=Path(repo))


@cli.group("workspace")
def workspace_commands():
    """Manage trust for project-level resources."""


def _workspace_command(ctx, repo, *, scope=None, grant=False):
    from durable_runtime.store import DurableRuntimeStore
    from durable_runtime.workspace_trust import change_trust, read_trust
    from project_memory.identity import ProjectIdentityResolver

    settings = _load_cli_config(ctx).durable_runtime
    identity = ProjectIdentityResolver.resolve(repo)
    with DurableRuntimeStore(identity, settings.root, timeout_ms=settings.busy_timeout_ms) as store:
        scopes = change_trust(store, scope, grant=grant) if scope else read_trust(store)
    click.echo(json.dumps({"project_id": identity.project_id, "granted_scopes": sorted(scopes)}, ensure_ascii=False))


@workspace_commands.command("trust")
@click.option("--repo", default=".", type=click.Path(exists=True, file_okay=False))
@click.option("--scope", default="project_skills",
              type=click.Choice(["project_skills", "project_guidance", "project_mcp", "project_hooks"]))
@click.pass_context
def workspace_trust(ctx, repo, scope):
    _workspace_command(ctx, repo, scope=scope, grant=True)


@workspace_commands.command("status")
@click.option("--repo", default=".", type=click.Path(exists=True, file_okay=False))
@click.pass_context
def workspace_status(ctx, repo):
    _workspace_command(ctx, repo)


@workspace_commands.command("revoke")
@click.option("--repo", default=".", type=click.Path(exists=True, file_okay=False))
@click.option("--scope", required=True,
              type=click.Choice(["project_skills", "project_guidance", "project_mcp", "project_hooks"]))
@click.pass_context
def workspace_revoke(ctx, repo, scope):
    _workspace_command(ctx, repo, scope=scope)


@cli.command("doctor")
@click.option("--json", "as_json", is_flag=True)
@click.option("--repo", default=".", type=click.Path(exists=True, file_okay=False))
@click.option("--check-model", is_flag=True, help="Send a small validation request to the selected model.")
@click.pass_context
def doctor(ctx, as_json, repo, check_model):
    """Check local configuration and environment without exposing credentials."""
    from entry.onboarding import environment_summary

    try:
        checks = environment_summary(_load_cli_config(ctx), _user_config_path(ctx), Path(repo).resolve(),
                                     check_model=check_model)
    except Exception as exc:
        raise click.ClickException(f"Configuration could not be loaded ({type(exc).__name__}).") from exc
    if as_json:
        click.echo(json.dumps(checks, ensure_ascii=False))
    else:
        for key, value in checks.items():
            click.echo(f"{key}: {value}")


def _resolve_run_approval_mode(approval_mode: str | None, confirm: bool | None) -> str:
    if approval_mode is not None and confirm is not None:
        raise click.UsageError("--approval-mode cannot be combined with --confirm/--no-confirm")
    return approval_mode or ("ask" if confirm is True else "deny" if confirm is False else "auto")


def _approval_confirm_callback(mode: str, ask_callback):
    if mode == "ask":
        return ask_callback
    if mode in {"auto", "full"}:
        return lambda _prompt: True
    return None


@cli.group("config")
def config_commands() -> None:
    """Inspect the resolved configuration."""


@cli.group("memory")
def memory_commands() -> None:
    """Inspect and approve local project memory."""


@cli.group("runtime")
@click.option("--repo", default=".", type=click.Path(exists=True, file_okay=False))
@click.pass_context
def runtime_commands(ctx, repo):
    """Inspect local durable execution state."""
    from durable_runtime.store import DurableRuntimeStore
    from project_memory.identity import ProjectIdentityResolver

    settings = _load_cli_config(ctx).durable_runtime
    ctx.obj = ctx.with_resource(DurableRuntimeStore(
        ProjectIdentityResolver.resolve(repo), settings.root, timeout_ms=settings.busy_timeout_ms,
    ))


@runtime_commands.command("verify")
@click.pass_obj
def runtime_verify(store):
    store.integrity_check()
    click.echo("ok")


@runtime_commands.command("status")
@click.pass_obj
def runtime_status(store):
    rows = store._conn.execute("SELECT status, count(*) AS count FROM runs GROUP BY status")
    click.echo(json.dumps({"project_id": store.identity.project_id, "database": str(store.path),
                          "runs": {row["status"]: row["count"] for row in rows}}, ensure_ascii=False))


@runtime_commands.command("backup")
@click.argument("destination", type=click.Path(dir_okay=False))
@click.pass_obj
def runtime_backup(store, destination):
    click.echo(str(store.backup(destination)))


@runtime_commands.command("runs")
@click.option("--status")
@click.pass_obj
def runtime_runs(store, status):
    rows = store._conn.execute(
        """SELECT run_id, scope_id, task_id, entrypoint, status, step, revision, owner_epoch
           FROM runs WHERE ? IS NULL OR status=? ORDER BY created_at DESC""", (status, status),
    )
    click.echo(json.dumps([dict(row) for row in rows], ensure_ascii=False))


@runtime_commands.command("cache")
@click.option("--run-id")
@click.option("--prune-expired", is_flag=True, help="Remove expired cache audit rows across this project.")
@click.pass_context
def runtime_cache(ctx, run_id, prune_expired):
    store = ctx.obj
    if prune_expired:
        if run_id:
            raise click.UsageError("--prune-expired applies to the project and cannot be combined with --run-id")
        count = store.prune_cache_audit(_load_cli_config(ctx.find_root()).cache_audit_retention_days)
        click.echo(json.dumps({"pruned": count}))
        return
    rows = store._conn.execute(
        """SELECT a.*, m.request_fingerprint AS request_digest FROM cache_audit_events a
           LEFT JOIN model_calls m ON m.model_call_id=a.request_id
           WHERE ? IS NULL OR a.run_id=? ORDER BY a.created_at""", (run_id, run_id),
    )
    report = []
    for row in rows:
        event = dict(row)
        event["cache_hit_ratio"] = (
            event["cached_tokens"] / event["prompt_tokens"] if event["cache_read_reported"]
            and event["prompt_tokens"] and event["cached_tokens"] is not None else None
        )
        report.append(event)
    click.echo(json.dumps(report, ensure_ascii=False))


@runtime_commands.command("archive")
@click.argument("scope_id")
@click.argument("archive_id", required=False)
@click.option("--offset", type=int, default=0)
@click.option("--limit", type=int, default=4096)
@click.pass_obj
def runtime_archive(store, scope_id, archive_id, offset, limit):
    from context.archive import ContextArchive
    from durable_runtime.store import StoreFormatError

    archive = ContextArchive(store, scope_id)
    try:
        result = archive.read(archive_id, offset, limit) if archive_id else archive.records()
    except (StoreFormatError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False))


@runtime_commands.command("archive-export")
@click.argument("scope_id")
@click.argument("destination", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--id", "archive_ids", multiple=True, help="Export only these roots and their sources.")
@click.pass_obj
def runtime_archive_export(store, scope_id, destination, archive_ids):
    """Export redacted records and their verified source graph."""
    import os
    from context.archive import ContextArchive, MAX_ARCHIVE_BUNDLE_BYTES
    from durable_runtime.run_manager import encode

    try:
        payload = encode(ContextArchive(store, scope_id).export_bundle(archive_ids)).encode("utf-8")
        if len(payload) > MAX_ARCHIVE_BUNDLE_BYTES:
            raise ValueError("archive bundle exceeds 32 MiB; select fewer roots")
        store._create_private_file(destination, exclusive=True)
        try:
            with destination.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(str(destination.resolve()))


@runtime_commands.command("archive-import")
@click.argument("scope_id")
@click.argument("bundle_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def runtime_archive_import(ctx, scope_id, bundle_path):
    """Attach a verified bundle to another Scope of this project."""
    from context.archive import ContextArchive, MAX_ARCHIVE_BUNDLE_BYTES

    settings = _load_cli_config(ctx.find_root()).durable_runtime
    if not settings.archive_enabled:
        raise click.ClickException("durable archive is disabled")
    store = ctx.obj
    try:
        with bundle_path.open("rb") as stream:
            contents = stream.read(MAX_ARCHIVE_BUNDLE_BYTES + 1)
        if len(contents) > MAX_ARCHIVE_BUNDLE_BYTES:
            raise ValueError("archive bundle exceeds 32 MiB")
        bundle = json.loads(contents.decode("utf-8"))
        owner = store.claim(scope_id)
        archive = ContextArchive(store, scope_id, settings.archive_inline_limit_bytes)
        with store.transaction(owner, owner.revision) as db:
            receipt = archive.import_bundle(db, bundle)
    except (OSError, ValueError, RuntimeError, RecursionError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(receipt))


@runtime_commands.command("import-chat")
@click.argument("session_id")
@click.pass_context
def runtime_import_chat(ctx, session_id):
    """Import a saved Chat history into its own Runtime scope."""
    import re
    from agent.session_store import JsonChatSessionStore
    from context.archive import ContextArchive
    from project_memory.identity import ProjectIdentityResolver

    if not re.fullmatch(r"[a-fA-F0-9]{12}", session_id):
        raise click.ClickException("invalid Chat session ID")
    config = _load_cli_config(ctx.find_root())
    if not config.durable_runtime.archive_enabled:
        raise click.ClickException("durable archive is disabled")
    store = ctx.obj
    try:
        session = JsonChatSessionStore(Path(config.agent.log_dir) / "chat").load(session_id)
        if session.session_id != session_id or ProjectIdentityResolver.resolve(session.repo_path) != store.identity:
            raise click.ClickException("Chat session belongs to a different project")
        owner = store.claim(session_id)
        archive = ContextArchive(store, session_id, config.durable_runtime.archive_inline_limit_bytes,
                                 session_id=session_id)
        with store.transaction(owner, owner.revision) as db:
            record = archive.import_history(db, session.history, session_id)
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(record)


@runtime_commands.command("inspect")
@click.argument("run_id")
@click.pass_obj
def runtime_inspect(store, run_id):
    from durable_runtime.run_manager import checked_payload

    row = store._conn.execute(
        """SELECT r.run_id, r.scope_id, r.task_id, r.entrypoint, r.status, r.revision,
                  r.step, c.kind, c.payload_json, c.payload_digest
           FROM runs r JOIN run_checkpoints c ON c.checkpoint_id=r.checkpoint_id AND c.run_id=r.run_id
           WHERE r.run_id=?""", (run_id,),
    ).fetchone()
    if row is None:
        raise click.ClickException("run checkpoint does not exist in this project")
    data = dict(row)
    try:
        data["checkpoint"] = checked_payload(data.pop("payload_json"), data.pop("payload_digest"))
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(data, ensure_ascii=False))


@runtime_commands.command("resume")
@click.argument("run_id")
@click.pass_context
def runtime_resume(ctx, run_id):
    """Resume an interrupted direct run using its original configuration."""
    repo = ctx.parent.params["repo"]
    ctx.obj = ctx.find_root().obj
    return ctx.invoke(run, repo=repo, resume_id=run_id)


@runtime_commands.command("pause")
@click.argument("run_id")
@click.pass_obj
def runtime_pause(store, run_id):
    try:
        store.request_pause(run_id)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Pause requested at the next safe boundary.")


@runtime_commands.command("owners")
@click.pass_obj
def runtime_owners(store):
    rows = store._conn.execute(
        "SELECT scope_id, host_id, boot_id, pid, owner_epoch, revision FROM runtime_scopes ORDER BY scope_id"
    )
    click.echo(json.dumps([dict(row) for row in rows]))


@runtime_commands.command("takeover")
@click.argument("scope_id")
@click.option("--previous-host", required=True)
@click.option("--previous-boot", required=True)
@click.option("--confirm-previous-owner-stopped", is_flag=True, required=True)
@click.pass_obj
def runtime_takeover(store, scope_id, previous_host, previous_boot, confirm_previous_owner_stopped):
    """Claim a scope after confirming the host or boot recorded in the store has stopped."""
    if not confirm_previous_owner_stopped:
        raise click.UsageError("Confirm the previous owner cannot continue executing.")
    try:
        owner = store.claim(scope_id, previous_identity=(previous_host, previous_boot))
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({"scope_id": scope_id, "owner_epoch": owner.owner_epoch}))


@runtime_commands.group("recover")
def runtime_recover():
    """Inspect and resolve unknown operation outcomes."""


@runtime_recover.command("list")
@click.option("--run-id")
@click.pass_obj
def runtime_recover_list(store, run_id):
    rows = store._conn.execute(
        """SELECT operation_id, run_id, name, state, effect FROM operations
           WHERE state IN ('started', 'outcome_unknown') AND (? IS NULL OR run_id=?) ORDER BY created_at""",
        (run_id, run_id),
    )
    click.echo(json.dumps([dict(row) for row in rows], ensure_ascii=False))


@runtime_recover.command("inspect")
@click.argument("operation_id")
@click.option("--online", is_flag=True, help="Query matching Git or GitHub delivery state.")
@click.option("--mcp-query", is_flag=True, help="Call the configured MCP read-only receipt query.")
@click.pass_context
def runtime_recover_inspect(ctx, operation_id, online, mcp_query):
    from durable_runtime.reconcilers import inspect_file_operation, inspect_git_commit, inspect_remote_delivery, inspect_mcp

    store = ctx.obj

    row = store._conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None:
        raise click.ClickException("operation does not exist in this project")
    decisions = store._conn.execute(
        """SELECT decision_id, action, actor, explanation, evidence_json, created_at
           FROM recovery_decisions WHERE operation_id=? ORDER BY created_at""", (operation_id,),
    )
    evidence = (inspect_file_operation(row, store.identity) or inspect_git_commit(row, store.identity)
                or (inspect_remote_delivery(row, store.identity) if online else None))
    if evidence is None and mcp_query:
        evidence = inspect_mcp(row, _load_cli_config(ctx.find_root()).mcp)
    click.echo(json.dumps({"operation": dict(row), "decisions": [dict(item) for item in decisions],
                          "evidence": evidence},
                          ensure_ascii=False))


@runtime_recover.command("resolve")
@click.argument("operation_id")
@click.option("--decision", type=click.Choice(["resolve_applied", "resolve_not_applied", "accept_unknown"]), required=True)
@click.option("--reason", required=True)
@click.option("--evidence", required=True, help="Describe the observed effect and where it was verified.")
@click.option("--decision-id")
@click.pass_obj
def runtime_recover_resolve(store, operation_id, decision, reason, evidence, decision_id):
    import getpass
    import uuid
    from durable_runtime.recovery import resolve_operation

    decision_id = decision_id or uuid.uuid4().hex
    try:
        resolve_operation(store, operation_id, decision_id, action=decision, actor=getpass.getuser(),
                          reason=reason, evidence={"operator_observation": evidence})
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(decision_id)


@runtime_recover.command("retry")
@click.argument("operation_id")
@click.option("--reason", required=True)
@click.pass_obj
def runtime_recover_retry(store, operation_id, reason):
    """Authorize one new attempt of a verified file write or Git push."""
    import getpass
    import uuid
    from durable_runtime.recovery import authorize_file_retry

    decision_id = uuid.uuid4().hex
    try:
        authorize_file_retry(store, operation_id, decision_id, actor=getpass.getuser(), reason=reason)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(decision_id)


@runtime_recover.command("delivery")
@click.argument("run_id")
@click.pass_obj
def runtime_recover_delivery(store, run_id):
    """Read GitHub to recover a missing PR receipt without creating another PR."""
    from durable_runtime.recovery import reconcile_delivery
    from durable_runtime.run_manager import DurableRun

    try:
        click.echo(reconcile_delivery(DurableRun(store, run_id, run_id)))
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


def _memory_for_cli(ctx: click.Context, repo: str):
    from project_memory.runtime import MemoryRuntime
    settings = _load_cli_config(ctx).memory
    return MemoryRuntime(repo, root=settings.root, budget=settings.context_budget_tokens,
                         max_results=settings.max_results)


@memory_commands.command("status")
@click.option("--repo", default=".")
@click.pass_context
def memory_status(ctx: click.Context, repo: str) -> None:
    runtime = _memory_for_cli(ctx, repo)
    click.echo(json.dumps({"project_id": runtime.store.identity.project_id,
        "path": str(runtime.store.base), "formal_count": len(runtime.store.read_records()),
        "pending_count": sum(row.get("status") == "pending" for row in runtime.writer.pending()),
        "latest_activity": (runtime.store.read_activity()[-1:] or [None])[0]}, ensure_ascii=False, indent=2))


@memory_commands.command("show")
@click.option("--repo", default=".")
@click.pass_context
def memory_show(ctx: click.Context, repo: str) -> None:
    path = _memory_for_cli(ctx, repo).store.path("memory/MEMORY.md")
    click.echo(path.read_text(encoding="utf-8") if path.exists() else "No project memory yet.")


@memory_commands.command("search")
@click.argument("query")
@click.option("--repo", default=".")
@click.pass_context
def memory_search(ctx: click.Context, query: str, repo: str) -> None:
    runtime = _memory_for_cli(ctx, repo)
    for row in runtime.retrieval.search(query, limit=runtime.max_results):
        click.echo(f"{row['memory_id']}\t{row['title']}\t{row.get('path', '')}\tstale={row['stale']}")


@memory_commands.command("approve")
@click.argument("candidate_id")
@click.option("--repo", default=".")
@click.pass_context
def memory_approve(ctx: click.Context, candidate_id: str, repo: str) -> None:
    try:
        runtime = _memory_for_cli(ctx, repo)
        runtime.writer.resolve(candidate_id, status="approved", repo=runtime.repo)
    except (OSError, ValueError, KeyError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Approved {candidate_id}")


@memory_commands.command("reject")
@click.argument("candidate_id")
@click.option("--repo", default=".")
@click.pass_context
def memory_reject(ctx: click.Context, candidate_id: str, repo: str) -> None:
    try:
        runtime = _memory_for_cli(ctx, repo)
        runtime.writer.resolve(candidate_id, status="rejected", repo=runtime.repo)
    except (OSError, ValueError, KeyError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Rejected {candidate_id}")


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
    for server in data["mcp"]["servers"]:
        server["env"] = {key: "<redacted>" for key in server["env"]}
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


@config_commands.command("path")
@click.pass_context
def config_path(ctx: click.Context) -> None:
    """Show the YAML file Forge reads for this command."""
    selected = ctx.obj.get("config_path")
    user = Path.home() / ".config" / "forge-agent" / "config.yaml"
    click.echo(Path(selected).expanduser() if selected else user if user.exists()
               else Path(__file__).parent.parent / "config" / "default.yaml")


@config_commands.command("models")
@click.pass_context
def config_models(ctx: click.Context) -> None:
    """List configured model names without showing credentials."""
    config = _load_cli_config(ctx)
    for name, model in sorted(config.model_profiles.items()):
        click.echo(f"{name}\t{model.provider}\t{model.model}")
    if not config.model_profiles:
        click.echo("No named models configured. Add entries under models: in config.yaml.")


@cli.group("skill")
def skill_commands() -> None:
    """Install or list Agent Skills."""


@skill_commands.command("add")
@click.argument("source")
@click.option("--local", "project", is_flag=True, help="Install in the target repository")
@click.option("--repo", default=".", help="Target repository for --local")
@click.pass_context
def skill_add(ctx: click.Context, source: str, project: bool, repo: str) -> None:
    """Install one local Skill folder or GitHub tree URL."""
    from entry.resource_commands import install_skill

    root = (Path(repo).resolve() / ".agents" / "skills") if project else None
    if root is None:
        global_dir = _load_cli_config(ctx).agent.skills_global_dir
        if global_dir is None:
            raise click.ClickException("Global Skill directory is disabled; use --local")
        root = Path(global_dir)
    try:
        installed = install_skill(source, root)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Installed Skill: {installed}")
    click.echo("Use run --skills to make it available to the agent.")


@skill_commands.command("list")
@click.option("--repo", default=".", help="Repository whose project Skills are included")
@click.pass_context
def skill_list(ctx: click.Context, repo: str) -> None:
    """List Skills visible in the current repository."""
    from skills.catalog import SkillCatalog

    catalog = SkillCatalog.discover(Path(repo).resolve(),
                                    global_root=_load_cli_config(ctx).agent.skills_global_dir)
    for skill in catalog.skills:
        click.echo(f"{skill.name}\t{skill.source}\t{skill.root}")
    if not catalog.skills:
        click.echo("No Skills found. Use forgeAgent skill add SOURCE.")
    for issue in catalog.issues:
        click.echo(f"Invalid Skill {issue.directory}: {issue.error}", err=True)


def _managed_skill_root(ctx, repo, project):
    root = (Path(repo).resolve() / ".agents" / "skills") if project else _load_cli_config(ctx).agent.skills_global_dir
    if root is None:
        raise click.ClickException("Global Skill directory is disabled; use --local")
    return Path(root)


def _skill_management(command):
    def action(ctx, name, repo, project):
        from entry.resource_commands import managed_skill, update_managed_skill

        root = _managed_skill_root(ctx, repo, project)
        try:
            data = (managed_skill(root, name)[1] if command == "inspect"
                    else update_managed_skill(root, name, remove=command == "remove"))
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(data, ensure_ascii=False))

    command_function = click.pass_context(action)
    command_function = click.option("--local", "project", is_flag=True)(command_function)
    command_function = click.option("--repo", default=".")(command_function)
    command_function = click.argument("name")(command_function)
    skill_commands.command(command)(command_function)


for _skill_command in ("inspect", "update", "remove"):
    _skill_management(_skill_command)


@cli.group("mcp")
def mcp_commands() -> None:
    """Configure or list MCP servers."""


@mcp_commands.command("add")
@click.argument("name")
@click.option("--url", help="Streamable HTTP MCP endpoint")
@click.option("--command", help="Stdio MCP server executable")
@click.option("--arg", "args", multiple=True, help="One stdio argument; repeat as needed")
@click.option("--env", "env_names", multiple=True, help="Pass an existing environment variable by name")
@click.pass_context
def mcp_add(ctx: click.Context, name: str, url: str | None, command: str | None,
            args: tuple[str, ...], env_names: tuple[str, ...]) -> None:
    """Add one MCP server to the existing config.yaml."""
    from entry.resource_commands import add_mcp_server

    if bool(url) == bool(command):
        raise click.UsageError("Provide exactly one of --url or --command")
    if url and (args or env_names):
        raise click.UsageError("--arg and --env are for stdio servers")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
        raise click.UsageError("MCP name must match [A-Za-z][A-Za-z0-9_-]{0,63}")
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in env_names):
        raise click.UsageError("--env expects environment variable names, not values")
    server = {"id": name, "transport": "streamable_http" if url else "stdio"}
    if url:
        server["url"] = url
    else:
        server.update(command=command, args=list(args))
        if env_names:
            server["env"] = {key: "${" + key + "}" for key in env_names}
    selected = ctx.obj.get("config_path")
    config_path = Path(selected).expanduser() if selected else Path.home() / ".config" / "forge-agent" / "config.yaml"
    template = None if selected else Path(__file__).parent.parent / "config" / "default.yaml"
    try:
        add_mcp_server(config_path, server, default_template=template)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Added MCP server {name} to {config_path}")
    click.echo("Use run --mcp to enable configured servers for a run.")


@mcp_commands.command("list")
@click.pass_context
def mcp_list(ctx: click.Context) -> None:
    """List configured MCP servers without displaying credentials."""
    servers = _load_cli_config(ctx).mcp.servers
    for server in servers:
        click.echo(f"{server.id}\t{server.transport}\t{'enabled' if server.enabled else 'disabled'}")
    if not servers:
        click.echo("No MCP servers configured. Use forgeAgent mcp add NAME --url URL.")


def _single_mcp_server(ctx, name):
    config = _load_cli_config(ctx).mcp
    server = next((entry for entry in config.servers if entry.id == name), None)
    if server is None:
        raise click.ClickException(f"No MCP server named {name}")
    return config, server


@mcp_commands.command("inspect")
@click.argument("name")
@click.pass_context
def mcp_inspect(ctx, name):
    _, server = _single_mcp_server(ctx, name)
    click.echo(json.dumps({"id": server.id, "transport": server.transport, "enabled": server.enabled,
                          "command": server.command, "argument_count": len(server.args),
                          "env_names": sorted(server.env)}, ensure_ascii=False))


@mcp_commands.command("test")
@click.argument("name")
@click.pass_context
def mcp_test(ctx, name):
    from dataclasses import asdict
    from mcp_integration.manager import MCPClientManager

    config, server = _single_mcp_server(ctx, name)
    manager = MCPClientManager(replace(config, enabled=True, servers=(replace(server, enabled=True),)))
    started = time.perf_counter()
    try:
        manager.start()
        click.echo(json.dumps({"id": server.id, "tools": len(manager.tools),
                              "elapsed_seconds": time.perf_counter() - started,
                              "servers": [asdict(snapshot) for snapshot in manager.snapshots]}))
    except Exception as exc:
        raise click.ClickException(f"MCP connection or discovery failed ({type(exc).__name__})") from exc
    finally:
        manager.close()


def _mcp_management(command):
    def action(ctx, name):
        from entry.resource_commands import change_mcp_server

        try:
            change_mcp_server(_user_config_path(ctx), name, command)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(name)

    mcp_commands.command(command)(click.argument("name")(click.pass_context(action)))


for _mcp_command in ("enable", "disable", "remove"):
    _mcp_management(_mcp_command)


# ---------------------------------------------------------------------------
# run 子命令
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--repo", "-r", default=".", show_default=True, help="Path to the target repository (default: current directory)")
@click.option("--task", "-t", default=None, help="Task description (natural language)")
@click.option("--task-file", "-f", default=None, help="Read task description from file")
@click.option("--resume-id", default=None, help="Resume a compatible interrupted direct run")
@click.option("--model", "-m", default=None, help="Select a configured model name or override the model ID")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option("--protocol", default=None, help="Override LLM protocol: chat_completions or responses")
@click.option("--plan/--no-plan", default=None, help="Enable or disable planning for this run")
@click.option("--skills/--no-skills", default=None, help="Enable or disable skills for this run")
@click.option("--mcp/--no-mcp", default=None, help="Enable or disable configured MCP servers for this run")
@click.option("--max-steps", default=None, type=int, help="Override max steps")
@click.option("--stream/--no-stream", "-s", default=True, help="Enable or disable streaming output (default: on)",)
@click.option(
    "--reasoning-stream/--no-reasoning-stream",
    default=False,
    help="Show model reasoning (opt in independently of answer streaming).",
)
@click.option("--confirm/--no-confirm", default=None, help="Legacy aliases for ask/deny approval modes")
@click.option(
    "--approval-mode", type=click.Choice(["ask", "auto", "full"]), default=None,
    help="Run permission mode: ask, auto-approve prompts (default), or full tool access.",
)
@click.option("--sandbox", is_flag=True, default=False, help="Run commands in Docker sandbox (requires Docker)")
@click.option("--isolate", is_flag=True, default=False, help="Run in an isolated Git worktree with task tracking. Combine with --sandbox for Docker.")
@click.option(
    "--result-policy",
    type=click.Choice(["discard", "keep-if-changed"]),
    default="keep-if-changed",
    show_default=True,
    help="How --isolate handles generated changes.",
)
@click.option("--ui", type=click.Choice(["auto", "rich", "plain"]), help="Terminal presentation mode")
@click.option("--no-progress", is_flag=True, help="Disable live status updates")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed events, tool output and debug logs")
@click.pass_context
def run(
    ctx: click.Context,
    repo: str,
    task: str | None,
    task_file: str | None,
    resume_id: str | None,
    model: str | None,
    provider: str | None,
    protocol: str | None,
    plan: bool | None,
    skills: bool | None,
    mcp: bool | None,
    max_steps: int | None,
    stream: bool,
    reasoning_stream: bool | None,
    confirm: bool | None,
    approval_mode: str | None,
    sandbox: bool,
    isolate: bool,
    result_policy: str,
    ui: str | None,
    no_progress: bool,
    verbose: bool,
) -> None:
    """Run the coding agent on a repository."""

    _maybe_onboard(ctx, repo)
    # 1.加载配置
    config = _load_cli_config(ctx)
    # 2.加载优先级更高的配置
    config = merge_cli_overrides(
        config, provider=provider, protocol=protocol, model=model, max_steps=max_steps
    )
    if plan is not None:
        config.agent.planning_mode = "always" if plan else "off"
    if skills is not None:
        config.agent.skills_enabled = skills
    if mcp is not None:
        if mcp and not config.mcp.servers:
            raise click.UsageError("--mcp requires mcp.servers in the user config")
        config.mcp = replace(config.mcp, enabled=mcp)
    if config.agent.run_profile == "sandboxed":
        sandbox = True

    repo_path = Path(repo).resolve()
    saved_task = None
    if resume_id:
        from agent.task import Task
        from durable_runtime.store import DurableRuntimeStore
        from project_memory.identity import ProjectIdentityResolver

        settings = config.durable_runtime
        if not settings.enabled or task or task_file:
            raise click.UsageError("--resume-id requires an enabled durable runtime without a new task")
        with DurableRuntimeStore(ProjectIdentityResolver.resolve(repo_path), settings.root,
                                 timeout_ms=settings.busy_timeout_ms) as store:
            row = store._conn.execute(
                "SELECT task_snapshot_json, request_snapshot_json FROM runs WHERE run_id=? AND scope_id=? AND entrypoint='cli'",
                (resume_id, resume_id),
            ).fetchone()
        if row is None:
            raise click.ClickException("Run does not exist in this repository")
        saved_task = Task(**json.loads(row[0]))
        saved_request = json.loads(row[1])
        isolate, sandbox = saved_request["isolate"], saved_request["sandbox"]
        result_policy = saved_request.get("result_policy", result_policy)
        if Path(saved_task.repo_path).resolve() != repo_path:
            raise click.ClickException("Run belongs to a different checkout of this repository")
        description = saved_task.description
    elif task_file:
        description = Path(task_file).read_text(encoding="utf-8").strip()
    elif task:
        description = task
    elif sys.stdin.isatty():
        description = click.prompt("Task").strip()
        if not description:
            raise click.UsageError("task cannot be blank")
    else:
        click.echo("Error: provide --task or --task-file", err=True)
        sys.exit(1)

    if not repo_path.exists():
        click.echo(f"Error: repo path does not exist: {repo_path}", err=True)
        sys.exit(1)

    from entry.ui.summary import repo_branch
    from entry.ui.terminal import TerminalUI

    renderer = TerminalUI(replace(config.ui, mode=ui or config.ui.mode),
                          verbose=verbose, no_progress=no_progress)
    resolved_approval_mode = _resolve_run_approval_mode(approval_mode, confirm)
    _terminal_logging(ctx, renderer, verbose)
    renderer.header("FORGE AGENT · RUN", {
        "Status": "INITIALIZING", "Task": description, "Repo": repo_path, "Branch": repo_branch(repo_path),
        **({"Provider": config.llm.provider, "Model": config.llm.model, "Protocol": config.llm.protocol}
           if config.ui.show_model_metadata else {}),
        "Approval": resolved_approval_mode, "Sandbox": "Docker" if sandbox else "local",
        "Isolate": isolate, "Planning": config.agent.planning_mode, "Skills": config.agent.skills_enabled,
        "MCP": config.mcp.enabled, "Workers": config.agent.multi_agent_enabled,
        "Max steps": config.agent.max_steps,
    })

    # 3.构建各组件
    backend_options = {
        "provider": config.llm.provider,
        "protocol": config.llm.protocol,
        "model": config.llm.model,
        "api_key": config.llm.api_key or None,
        "base_url": config.llm.base_url or None,
        "max_tokens": config.llm.max_tokens,
        "strict_tool_schema": config.llm.strict_tool_schema,
    }
    from entry.context_common import (
        bind_run_backends, build_context_policy, configured_agent_options, new_worker_backend,
    )
    evaluation_factory = ctx.obj.get("evaluation_backend_factory")
    pool_factory = ctx.obj.get("evaluation_pool_factory")
    create_run_backend = evaluation_factory or create_backend_from_config

    def worker_backend_factory(role):
        if pool_factory is not None:
            return new_worker_backend(config, role, factory=lambda opts: pool_factory(opts, "worker"))
        if evaluation_factory is not None:
            return new_worker_backend(config, role, factory=evaluation_factory)
        return new_worker_backend(config, role)

    eval_requests = ctx.obj.get("evaluation_requests")
    if eval_requests is not None and pool_factory is not None:
        raise click.UsageError("Choose either local trial budgets or a host pool factory")
    if (eval_requests is not None or pool_factory is not None) and (
        config.llm.protocol != "responses" or stream or reasoning_stream or config.agent.worker_models
    ):
        raise click.UsageError("Trial request budgets require non-streaming Responses and one model")
    try:
        backend = (pool_factory(backend_options, "lead") if pool_factory is not None
                   else create_run_backend(backend_options))
        compaction_backend = backend
        if pool_factory is not None and config.context.compaction_mode != "off":
            compaction_backend = pool_factory(backend_options, "compaction")
        if eval_requests is not None:
            compaction_backend, worker_backend_factory = bind_run_backends(
                config, backend, lambda: create_run_backend(backend_options),
                worker_backend_factory, budget=eval_requests[0], input_upper_bound=eval_requests[1],
            )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    from tools.shell_tool import terminal_confirm
    from tools.runtime import CONTAINER_WORKDIR, create_runtime
    confirm_cb = _approval_confirm_callback(resolved_approval_mode, renderer.confirm_with(terminal_confirm))
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

    context_policy = build_context_policy(config, compaction_backend)

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
        stream=stream or bool(reasoning_stream),
        stream_callback=renderer.on_stream_text if stream else None,
        thought_callback=renderer.on_reasoning_text if reasoning_stream else None,
        confirm_dangerous=confirm_cb is not None,
        confirm_callback=confirm_cb,
        approval_mode="full" if resolved_approval_mode == "full" else "ask",
        execution_workspace=CONTAINER_WORKDIR if sandbox else None,
    )
    require_changes, require_tests = infer_completion_requirements(description)
    task_obj = saved_task or Task(
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
        worker_backend_factory=worker_backend_factory,
    )

    # 隔离运行绑定工作树、任务状态与权限范围。
    #事务隔离，在worktree中跑：异步async 不用之前的agent实例，只有 backend、task_obj、agent_config、confirm_cb 被复用
    if isolate:
        from ipc.bus import AgentBus

        #持久化状态
        Path(config.agent.log_dir).mkdir(parents=True, exist_ok=True)
        bus = AgentBus() if verbose else None
        runner.bus = bus
        renderer.status("Initializing isolated execution")

        t0 = time.time()
        try:
            result = runner.run(
                RunRequest(
                    task=task_obj,
                    run_id=resume_id, resume=bool(resume_id),
                    isolate=True,
                    sandbox=sandbox,
                    result_policy=result_policy,
                ),
                on_event=renderer,
            )
        finally:
            renderer.close()
            runner.close()
        elapsed = time.time() - t0
        renderer.finish(result, elapsed)
        ctx.exit(0 if result.is_success() else 1)

    if verbose:
        renderer.line(f"  tiktoken: {'yes' if is_tiktoken_available() else 'no (char estimate)'}", "text.secondary")

    renderer.status("Initializing execution")
    t0 = time.time()
    try:
        with EventLog.create(task_obj, log_dir=config.agent.log_dir) as log:
            renderer.line(f"Log: {log.path}", "text.secondary")
            #真正的执行核心。它的职责包括维护对话历史、组装 messages 调用 LLM、拿到 Action 后执行工具、写入 Action 和 Observation 到 EventLog、检测终止条件和 reflection 条件，最后返回 RunResult。
            result = runner.run(
                RunRequest(task=task_obj, run_id=resume_id, resume=bool(resume_id)), log=log, on_event=renderer,
            )
    finally:
        renderer.close()
        runner.close()
        if runtime is not None:
            runtime.cleanup()

    elapsed = time.time() - t0
    renderer.finish(result, elapsed)

    sys.exit(0 if result.is_success() else 1)


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
@click.option("--model", "-m", default=None, help="Select a configured model name or override the model ID")
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
    default=False,
    help="Show model reasoning (opt in independently of answer streaming).",
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
    from agent.session import ChatSessionConflict, ChatSessionError
    from agent.session_store import JsonChatSessionStore
    from config.schema import save_selected_model, select_model
    from entry.context_common import build_context_policy
    from entry.chat import APPROVAL_MODES, ChatSession
    from entry.chat_input import chat_prompt, choose_model
    from entry.ui.terminal import TerminalUI
    from dataclasses import replace

    if continue_session and resume_session:
        raise click.UsageError("--continue and --resume cannot be used together")
    if no_session and (continue_session or resume_session):
        raise click.UsageError(
            "--no-session cannot be combined with --continue or --resume"
        )


    _maybe_onboard(ctx, repo)
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
        click.echo(f"Error: repo path does not exist: {repo_path}", err=True)
        sys.exit(1)

    try:
        backend = _chat_backend(config)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
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
    renderer = TerminalUI(config.ui, verbose=verbose, chat=True)
    _terminal_logging(ctx, renderer, verbose)
    confirm_cb = renderer.confirm_with(terminal_confirm)

    def build_chat_registry(mode: str):
        return _build_run_registry(
            config, runtime=runtime, default_cwd=str(repo_path), workspace=str(repo_path),
            full_access=mode == "full",
        )

    registry = build_chat_registry("ask")
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
            confirm_callback=confirm_cb,   # chat 模式默认开启确认
            ui=renderer,
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

    def footer():
        items = [f"Approval: {session.approval_mode}"]
        if config.ui.show_model_metadata:
            items.append(config.llm.model)
        if config.ui.show_token_usage:
            items.append(f"{session.total_tokens:,} tokens")
        return " · ".join(items)

    prompt_session = chat_prompt(repo_path, footer) if renderer.rich else None
    heading = ["Forge Agent", repo_path.name]
    if prompt_session is None and config.ui.show_model_metadata:
        heading.append(config.llm.model)
    if continued_existing or resume_session:
        heading.append("resumed")
    if sandbox:
        heading.append("Docker")
    if prompt_session is None:
        heading.append(f"Approval: {session.approval_mode}")
    renderer.line(" · ".join(heading), "app.title")
    if getattr(session, "recovery_warning", None):
        renderer.line(session.recovery_warning, "status.warning")

    try:
        # 主 REPL 循环
        while True:
            try:
                user_input = (
                    prompt_session.prompt("you > ") if prompt_session is not None
                    else input("you > ")
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
                        renderer.line(f"  Started new session: {new_id}", "text.secondary")
                    else:
                        renderer.line("  Started new ephemeral session.", "text.secondary")
                elif cmd == "/resume":
                    if not arg:
                        try:
                            with renderer.activity("Checking workspace"):
                                session.run_round("", resume=True)
                        except (RuntimeError, ValueError) as exc:
                            renderer.line(f"  Cannot resume round: {exc}", "status.error")
                    else:
                        try:
                            session.resume_session(arg)
                            renderer.line(f"  Resumed session: {session.session_id}", "text.secondary")
                            if session.recovery_warning:
                                renderer.line(f"  Warning: {session.recovery_warning}", "status.warning")
                        except ChatSessionError as exc:
                            renderer.line(f"  Cannot resume session: {exc}", "status.error")
                elif cmd == "/rewind":
                    try:
                        child = session.rewind(int(arg))
                        renderer.line(f"  Rewound to round {arg} in session {child}.", "text.secondary")
                    except (ChatSessionError, ValueError, OSError, RuntimeError) as exc:
                        renderer.line(f"  Cannot rewind: {exc}", "status.error")
                elif cmd == "/rename":
                    if not arg:
                        renderer.line("  Usage: /rename NAME", "text.secondary")
                    else:
                        try:
                            session.rename(arg)
                            renderer.line(f"  Session renamed: {arg}", "text.secondary")
                        except (ChatSessionError, ValueError) as exc:
                            renderer.line(f"  Cannot rename session: {exc}", "status.error")
                elif cmd in {"/archive", "/cache", "/recovery"}:
                    if cmd == "/recovery":
                        if not renderer.recoveries:
                            renderer.line("No recovery events recorded for this round.")
                        for event in renderer.recoveries:
                            renderer.line(event, "action.recovery")
                    session.show_runtime(cmd, arg or None)
                elif cmd in ("/status", "/diff", "/tokens", "/plan", "/skills", "/mcp", "/compact"):
                    session.show_ui_command(cmd)
                elif cmd == "/approval":
                    mode = arg.lower()
                    if not mode:
                        renderer.panel("APPROVAL", {"Current": session.approval_mode, **APPROVAL_MODES})
                        renderer.line("  Usage: /approval ask|auto|full", "text.secondary")
                    elif mode not in APPROVAL_MODES:
                        renderer.line("  Usage: /approval ask|auto|full", "status.warning")
                    else:
                        if (session.approval_mode == "full") != (mode == "full"):
                            tools_for_mode = build_chat_registry(mode)
                            for name in ("shell", "git_add", "git_commit"):
                                tool = tools_for_mode.get_tool(name)
                                if tool is None:
                                    registry.unregister(name)
                                else:
                                    registry.register(tool, replace=True)
                        session.set_approval_mode(mode, _approval_confirm_callback(mode, confirm_cb))
                        renderer.line(f"  Approval: {mode} · {APPROVAL_MODES[mode]}", "status.success")
                elif cmd == "/clear":
                    session.clear_history()
                    renderer.line("  Conversation history cleared.", "text.secondary")
                elif cmd == "/model":
                    save_default = bool(arg)
                    can_save_default = ctx.obj.get("config_path") is None
                    if not arg:
                        if config.model_profiles and prompt_session is not None:
                            renderer.close()
                            selected = choose_model(config.model_profiles, config.llm, allow_default=can_save_default)
                            if selected is None:
                                continue
                            arg, save_default = selected
                        else:
                            renderer.line(f"  Current: {config.llm.model} ({config.llm.provider})")
                            for name, profile in config.model_profiles.items():
                                renderer.line(f"  {name} · {profile.model} ({profile.provider})")
                            renderer.line("  Usage: /model NAME_OR_MODEL_ID", "text.secondary")
                    if arg:
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
                            renderer.line(f"  Model: {config.llm.model} ({config.llm.provider})", "status.success")
                            if save_default and can_save_default:
                                try:
                                    save_selected_model(arg, profile=arg in config.model_profiles)
                                except OSError as exc:
                                    renderer.line(f"  Could not save model choice: {exc}", "status.warning")
                        except ValueError as exc:
                            renderer.line(f"  Cannot switch model: {exc}", "status.error")
                elif cmd == "/help":
                    renderer.line(
                        "  Commands:\n"
                        "    /exit   — quit\n"
                        "    /stats  — show session statistics\n"
                        "    /session — show current session id and state path\n"
                        "    /new    — start a new session\n"
                        "    /resume [SESSION_ID] — resume a compatible interrupted round or switch sessions\n"
                        "    /rewind ROUND — continue from a finished round in a new session\n"
                        "    /rename NAME — name the current session\n"
                        "    /clear  — clear effective conversation history\n"
                        "    /model [NAME_OR_MODEL_ID] — list or switch models\n"
                        "    /approval [ask|auto|full] — show or change tool approval\n"
                        "    /status — model, repository and session\n"
                        "    /diff   — last round patch\n"
                        "    /tokens — session usage and latest model input\n"
                        "    /plan /skills /mcp /compact — inspect execution details\n"
                        "    /recovery /archive /cache — inspect recovery and durable records\n"
                        "    /help   — show this help\n"
                        "  Anything else is sent to the agent.",
                        "text.secondary",
                    )
                else:
                    renderer.line(f"  Unknown command: {user_input}. Type /help for help.", "text.secondary")
                continue

            # 运行一轮 agent

            try:
                with renderer.activity("Checking workspace"):
                    session.run_round(user_input)
            except KeyboardInterrupt:
                renderer.line("\n  Interrupted. Type /exit to quit or continue with a new task.", "status.warning")
            except ChatSessionConflict:
                # 冲突时保持 fail-closed，不覆盖另一进程已经写入的新状态。
                renderer.line("\n  Session conflict: another process saved a newer state; this round was not saved.", "status.error")
                renderer.line(f"  Tool side effects may be partial. Use /resume {session.session_id} to reload, or /new to start separately.", "status.warning")
            except Exception as e:
                renderer.line(f"\n  Error: {e}", "status.error")
                if verbose:
                    import traceback
                    traceback.print_exc()
    finally:
        session.close()
        if runtime is not None:
            runtime.cleanup()

    if verbose:
        session.print_stats()


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
        click.echo(f"File not found: {path}", err=True)
        sys.exit(1)

    with EventLog.open_existing(path) as elog:
        events = elog.replay()
        stats = summarize_run(elog)

    click.echo(f"\nEvent Log: {path.name}")
    click.echo(f"  Total events : {stats['total_events']}")
    click.echo(f"  Actions      : {stats['actions']}")
    click.echo(f"  Reflections  : {stats['reflections']}")
    click.echo(f"  Tool calls   : {stats['tool_calls']}")
    click.echo(f"  Final status : {stats['final_status']}\n")

    click.echo("Events:")
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

    click.echo(f"\nLog files in {log_path}:\n")
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
