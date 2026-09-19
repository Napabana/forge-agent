"""
entry/github_issue.py

GitHub Issue 自动修复入口。

流程：
1. 拉取 Issue 标题 + body 作为任务描述
2. Clone 或使用已有的本地 repo
3. 在新分支上运行 agent
4. agent 完成后创建 PR（可选）

用法：
    python -m entry.github_issue \
        --repo owner/repo \
        --issue 42 \
        --local-path /tmp/myrepo

依赖：
    pip install PyGithub gitpython
"""

from __future__ import annotations

import base64
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import click

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GitHub 操作
# ---------------------------------------------------------------------------

def _get_github_client():
    """初始化 PyGithub 客户端，从环境变量读 token。"""
    try:
        from github import Github
    except ImportError:
        raise ImportError("PyGithub not installed. Run: pip install PyGithub")

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise ValueError(
            "GITHUB_TOKEN environment variable is not set.\n"
            "Create a token at https://github.com/settings/tokens"
        )
    return Github(token)


def fetch_issue(repo_name: str, issue_number: int) -> tuple[str, str, str]:
    """
    拉取 GitHub Issue 内容。

    Returns:
        (title, body, html_url)
    """
    gh = _get_github_client()
    repo = gh.get_repo(repo_name)
    issue = repo.get_issue(issue_number)
    return issue.title, issue.body or "", issue.html_url


def create_pull_request(
    repo_name: str,
    branch: str,
    title: str,
    body: str,
    base: str = "main",
) -> str:
    """
    创建 PR，返回 PR URL。

    Args:
        repo_name: "owner/repo" 格式
        branch:    源分支（agent 在这个分支上做了修改）
        title:     PR 标题
        body:      PR 描述
        base:      目标分支，默认 main
    """
    gh = _get_github_client()
    repo = gh.get_repo(repo_name)

    # 检查 base 分支是否存在，不存在时尝试 master
    try:
        repo.get_branch(base)
    except Exception:
        base = "master"

    pr = repo.create_pull(
        title=title,
        body=body,
        head=branch,
        base=base,
    )
    return pr.html_url


# ---------------------------------------------------------------------------
# Git 操作
# ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: str, env: dict[str, str] | None = None) -> tuple[bool, str]:
    """运行 git 命令，返回 (success, output)。"""
    try:
        proc = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=cwd,
            env=env,
        )
        output = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, output
    except Exception as e:
        return False, str(e)


def _github_git_env() -> dict[str, str] | None:
    """通过临时 Git 配置传递 GitHub 凭据，不把 Token 写入参数或 remote。"""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return None
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = os.environ.copy()
    env.pop("GITHUB_TOKEN", None)
    # 禁用可能打印 HTTP 头的 Git 调试开关，认证配置仅对子进程生效。
    for key in tuple(env):
        if key.startswith("GIT_TRACE") or key == "GIT_CURL_VERBOSE":
            env.pop(key)
    env.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
    })
    return env


def clone_repo(repo_name: str, local_path: str) -> None:
    """Clone repo 到本地路径（如果已存在则跳过）。"""
    path = Path(local_path)
    if path.exists() and (path / ".git").exists():
        logger.info("Repo already exists at %s, skipping clone", local_path)
        return

    url = f"https://github.com/{repo_name}.git"
    click.echo(f"Cloning {repo_name} → {local_path} ...")
    ok, out = _run_git(["clone", url, local_path], cwd="/tmp", env=_github_git_env())
    if not ok:
        raise RuntimeError(f"git clone failed: {out}")


def create_branch(local_path: str, branch: str) -> None:
    """创建并切换到新分支。"""
    ok, out = _run_git(["checkout", "-b", branch], cwd=local_path)
    if not ok:
        # 分支已存在，切换过去
        switched, switch_out = _run_git(["checkout", branch], cwd=local_path)
        if not switched:
            raise RuntimeError(f"git branch checkout failed: {out}; {switch_out}")


def push_branch(local_path: str, branch: str) -> None:
    """推送分支到远端。"""
    ok, out = _run_git(
        ["push", "--set-upstream", "origin", branch],
        cwd=local_path,
        env=_github_git_env(),
    )
    if not ok:
        raise RuntimeError(f"git push failed: {out}")


def _command_verifier(command: str):
    """把用户显式提供的验收命令转换为不经过 shell 的隐藏 verifier。"""
    argv = shlex.split(command, posix=os.name != "nt")
    if not argv:
        raise ValueError("verification command cannot be empty")

    def verify(repo: Path) -> bool:
        proc = subprocess.run(
            argv,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        logger.info("Independent verifier exited with %d", proc.returncode)
        return proc.returncode == 0

    return verify


def _branch_is_pushed(local_path: str, branch: str) -> bool:
    """比较本地 HEAD 和远端分支 SHA，避免 PR 重试时重复 push。"""
    head_ok, head = _run_git(["rev-parse", "HEAD"], cwd=local_path)
    remote_ok, remote = _run_git(
        ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        cwd=local_path,
        env=_github_git_env(),
    )
    return head_ok and remote_ok and bool(remote) and remote.split()[0] == head.strip()


def deliver_pull_request(
    result,
    local_path: str,
    branch: str,
    commit_message: str,
    repo_name: str,
    pr_title: str,
    pr_body: str,
    base_branch: str = "main",
    pr_creator=create_pull_request,
) -> str | None:
    """通过验收后确定性执行 commit、push 和 PR；失败时保留本地成果。"""
    if not result.is_success():
        result.delivery_status = "blocked_agent"
        return None
    if result.acceptance_status != "passed":
        result.delivery_status = "blocked_acceptance"
        return None

    branch_ok, current_branch = _run_git(["branch", "--show-current"], cwd=local_path)
    if not branch_ok or current_branch.strip() != branch:
        result.delivery_status = "commit_failed"
        return None
    status_ok, status = _run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=local_path,
    )
    if not status_ok:
        result.delivery_status = "commit_failed"
        return None

    if status:
        added, _ = _run_git(["add", "--all"], cwd=local_path)
        committed, _ = (
            _run_git(["commit", "-m", commit_message], cwd=local_path)
            if added
            else (False, "")
        )
        if not committed:
            result.delivery_status = "commit_failed"
            return None
    else:
        subject_ok, subject = _run_git(
            ["log", "-1", "--format=%s"], cwd=local_path
        )
        if not subject_ok or subject.strip() != commit_message:
            result.delivery_status = "no_changes"
            return None

    if not _branch_is_pushed(local_path, branch):
        try:
            push_branch(local_path, branch)
        except RuntimeError as exc:
            logger.warning("Push failed; local commit retained: %s", exc)
            result.delivery_status = "push_failed"
            return None

    try:
        pr_url = pr_creator(
            repo_name=repo_name,
            branch=branch,
            title=pr_title,
            body=pr_body,
            base=base_branch,
        )
    except Exception as exc:
        logger.warning("PR creation failed; pushed branch retained: %s", exc)
        result.delivery_status = "pr_failed"
        return None
    result.delivery_status = "delivered"
    return pr_url


def _record_delivery_trace(
    result,
    *,
    requested: bool,
    repo_name: str,
    branch: str,
    issue_number: int,
    pr_url: str | None = None,
    on_event=None,
) -> None:
    """Append product delivery outcome without making delivery depend on Trace I/O."""
    if not result.trace_path:
        return
    from agent.event_log import EventLog

    try:
        with EventLog.open_existing(
            result.trace_path,
            task_id=result.task_id,
            entrypoint="github_issue",
        ) as log:
            if on_event is not None:
                log.on_append(on_event)
            log.log_delivery(
                steps=result.steps_taken,
                requested=requested,
                delivery_status=result.delivery_status,
                repo=repo_name,
                branch=branch,
                issue_number=issue_number,
                pr_url=pr_url,
            )
    except Exception as exc:  # noqa: BLE001 — observability must not alter delivery
        logger.warning("Failed to append GitHub delivery trace: %s", exc)


# ---------------------------------------------------------------------------
# 核心流程
# ---------------------------------------------------------------------------

def run_on_issue(
    repo_name: str,
    issue_number: int,
    local_path: str,
    config_path: str | None = None,
    create_pr: bool = True,
    base_branch: str = "main",
    verify_command: str | None = None,
    reasoning_stream: bool = False,
) -> int:
    """
    拉取 Issue，运行 agent，创建 PR。

    Returns:
        0 if success, 1 if failed
    """
    from config.schema import load_config
    from agent.core import AgentConfig
    from agent.runner import AcceptanceContract, ExecutionRunner, RunRequest
    from agent.task import Task, infer_completion_requirements
    from entry.event_renderer import RunEventRenderer
    from llm.router import create_backend_from_config

    config = load_config(config_path)
    if create_pr and not verify_command:
        click.echo(
            "Error: automatic PR requires --verify-command for independent acceptance.",
            err=True,
        )
        return 1
    try:
        verifier = _command_verifier(verify_command) if create_pr else None
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        return 1

    # 1. 拉取 Issue
    click.echo(f"\nFetching issue #{issue_number} from {repo_name} ...")
    try:
        title, body, issue_url = fetch_issue(repo_name, issue_number)
    except Exception as e:
        click.echo(f"Error fetching issue: {e}", err=True)
        return 1

    click.echo(f"  Title: {title}")
    description = f"Fix GitHub Issue #{issue_number}: {title}\n\n{body}"

    # 2. Clone（如果需要）
    try:
        clone_repo(repo_name, local_path)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        return 1

    if create_pr:
        # 自动交付只接受干净基线，防止把用户运行前的修改一并提交。
        clean_ok, dirty = _run_git(
            ["status", "--porcelain=v1", "--untracked-files=all"],
            cwd=local_path,
        )
        if not clean_ok or dirty:
            click.echo(
                "Error: repository must be clean before automatic issue delivery.",
                err=True,
            )
            return 1

    # 3. 创建工作分支
    branch = f"agent/fix-issue-{issue_number}-{int(time.time())}"
    try:
        create_branch(local_path, branch)
    except RuntimeError as exc:
        click.echo(f"Error: {exc}", err=True)
        return 1
    click.echo(f"  Branch: {branch}")

    # 4. 构建 agent
    try:
        backend = create_backend_from_config({
            "provider": config.llm.provider,
            "protocol": config.llm.protocol,
            "model": config.llm.model,
            "api_key": config.llm.api_key or None,
            "base_url": config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        return 1

    from entry.cli import _build_registry
    # GitHub Issue 的所有仓库工具默认在目标仓库执行，避免误用 Forge 进程目录。
    registry = _build_registry(
        config,
        default_cwd=local_path,
        workspace=local_path,
    )
    if create_pr:
        # 自动交付层统一负责提交，Agent 不得在独立验收前改变 Git 基线。
        registry._tools.pop("git_add", None)
        registry._tools.pop("git_commit", None)

    renderer = RunEventRenderer(compact=True)

    def _thought_cb(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    agent_config = AgentConfig(
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
        planning_mode=config.agent.planning_mode,
        recovery_mode=config.agent.recovery_mode,
        recovery_max_attempts=config.agent.recovery_max_attempts,
        skills_enabled=config.agent.skills_enabled,
        skills_global_dir=config.agent.skills_global_dir,
        skills_max_loaded=config.agent.skills_max_loaded,
        skills_max_chars=config.agent.skills_max_chars,
        skills_reference_max_chars=config.agent.skills_reference_max_chars,
        stream=reasoning_stream,
        thought_callback=_thought_cb if reasoning_stream else None,
    )
    require_changes, require_tests = infer_completion_requirements(description)
    task = Task(
        description=description,
        repo_path=local_path,
        issue_url=issue_url,
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
        require_changes=require_changes,
        require_tests=require_tests,
    )

    # 5. 运行 agent
    click.echo(f"\nRunning agent on issue #{issue_number} ...")
    t0 = time.time()
    acceptance = AcceptanceContract(
        require_changes=require_changes,
        require_tests=require_tests,
        verifier=verifier,
    )
    result = ExecutionRunner(
        backend=backend,
        registry=registry,
        config=agent_config,
        log_dir=config.agent.log_dir,
    ).run(
        RunRequest(
            task=task,
            acceptance=acceptance,
            entrypoint="github_issue",
        ),
        on_event=renderer,
    )

    elapsed = time.time() - t0
    click.echo(f"  Status : {result.status.value}")
    click.echo(f"  Acceptance: {result.acceptance_status}")
    click.echo(f"  Steps  : {result.steps_taken}")
    click.echo(f"  Tokens : {result.total_tokens:,}")
    click.echo(f"  Time   : {elapsed:.1f}s")

    if not result.is_success():
        if create_pr:
            result.delivery_status = "blocked_agent"
        _record_delivery_trace(
            result,
            requested=create_pr,
            repo_name=repo_name,
            branch=branch,
            issue_number=issue_number,
            on_event=renderer,
        )
        click.echo("  Agent did not complete the task.", err=True)
        return 1

    # 6. 独立验收通过后才进入确定性交付
    if create_pr:
        pr_title = f"[Agent] Fix issue #{issue_number}: {title}"
        pr_body = (
            f"Fixes #{issue_number}\n\n"
            f"This PR was automatically generated by the coding agent.\n\n"
            f"## Summary\n{result.summary}\n\n"
            f"## Task\n{description[:500]}"
        )
        pr_url = deliver_pull_request(
            result,
            local_path,
            branch,
            f"fix: resolve issue #{issue_number}",
            repo_name,
            pr_title,
            pr_body,
            base_branch,
        )
        _record_delivery_trace(
            result,
            requested=True,
            repo_name=repo_name,
            branch=branch,
            issue_number=issue_number,
            pr_url=pr_url,
            on_event=renderer,
        )
        if pr_url:
            click.echo(f"\n✓ PR created: {pr_url}\n")
        else:
            click.echo(
                f"Delivery stopped: {result.delivery_status}. Local artifact: {local_path}",
                err=True,
            )
            return 1
    else:
        _record_delivery_trace(
            result,
            requested=False,
            repo_name=repo_name,
            branch=branch,
            issue_number=issue_number,
            on_event=renderer,
        )

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--repo", "-r", required=True, help="GitHub repo (owner/repo)")
@click.option("--issue", "-i", required=True, type=int, help="Issue number")
@click.option(
    "--local-path", "-l", required=True,
    help="Local path to clone/use the repo",
)
@click.option("--config", "-c", default=None, help="Config YAML path")
@click.option("--no-pr", is_flag=True, help="Skip PR creation")
@click.option("--base-branch", default="main", help="Base branch for PR (default: main)")
@click.option(
    "--verify-command",
    default=None,
    help="Independent acceptance command required for automatic PR",
)
@click.option(
    "--reasoning-stream/--no-reasoning-stream",
    default=False,
    help="Stream model reasoning separately from lifecycle progress.",
)
@click.option("--verbose", "-v", is_flag=True)
def main(
    repo: str,
    issue: int,
    local_path: str,
    config: str | None,
    no_pr: bool,
    base_branch: str,
    verify_command: str | None,
    reasoning_stream: bool,
    verbose: bool,
) -> None:
    """Run the coding agent on a GitHub issue and create a PR."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    sys.exit(run_on_issue(
        repo_name=repo,
        issue_number=issue,
        local_path=local_path,
        config_path=config,
        create_pr=not no_pr,
        base_branch=base_branch,
        verify_command=verify_command,
        reasoning_stream=reasoning_stream,
    ))


if __name__ == "__main__":
    main()
