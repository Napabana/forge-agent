"""
runtime/worktree.py

Git Worktree 异步事务上下文（M1 Task 1.2）。

把 s20_comprehensive/code.py 的同步函数式 worktree 系统（171-283 行）迁移成
async 上下文管理器 WorktreeSession。完整保留 s20 的命令与约定：
- git worktree add <path> -b wt/<name> <base>
- git worktree remove <path> --force  +  git branch -D wt/<name>
- 名字校验规则（validate_worktree_name，code.py:181）
- 分支命名约定 wt/<name>

核心语义（M1 任务规划 Task 1.2）：
- __aenter__ 创建隔离工作区（可选绑定 TaskEngine）
- __aexit__ 无论是否抛异常，强制 git worktree remove --force + 清理残余
  → 智能体崩溃 / 测试失败时文件系统能安全、干净地回滚

并发模型：每个会话用 asyncio.create_subprocess_shell 自己跑 git，
不依赖同步的 Runtime 抽象（那是给同步工具链用的）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from task.engine import TaskEngine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 名字校验（移植自 s20 code.py:178-189）
# ---------------------------------------------------------------------------

VALID_WT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def validate_worktree_name(name: str) -> str | None:
    """
    校验 worktree 名字（安全边界：在 git 看到名字之前先拦）。
    返回 None 表示合法，否则返回错误消息。
    """
    if not name:
        return "Worktree name cannot be empty"
    if name in (".", ".."):
        return f"'{name}' is not a valid worktree name"
    if not VALID_WT_NAME.match(name):
        return (f"Invalid worktree name '{name}': "
                "only letters, digits, dots, underscores, dashes (1-64 chars)")
    return None


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class WorktreeError(Exception):
    """WorktreeSession 操作失败。"""


# ---------------------------------------------------------------------------
# WorktreeSession
# ---------------------------------------------------------------------------

class WorktreeSession:
    """
    Git Worktree 异步事务上下文。

    用法：
        async with WorktreeSession(repo_path, "feat-x", task_engine, tid) as wt:
            # wt.path 是隔离工作区路径，在里面改代码 / 跑测试
            ...
        # 退出时（含异常）自动 git worktree remove --force + 删分支

    Args:
        repo_path:    宿主 git 仓库根目录（必须有至少一次提交）
        name:         worktree 名字（须通过 validate_worktree_name）
        task_engine:  可选，提供后 __aenter__ 会 bind_worktree
        task_id:      配合 task_engine，要绑定的任务 id
        base:         worktree 基点，默认 "HEAD"
        worktrees_dir: worktree 存放目录，默认 repo_path/.worktrees
    """

    def __init__(
        self,
        repo_path: str | Path,
        name: str,
        task_engine: "TaskEngine | None" = None,
        task_id: str | None = None,
        base: str = "HEAD",
        worktrees_dir: str | Path | None = None,
    ) -> None:
        err = validate_worktree_name(name)
        if err:
            raise WorktreeError(err)

        self._repo_path = Path(repo_path).resolve()
        self._name = name
        self._base = base
        self._task_engine = task_engine
        self._task_id = task_id
        self._worktrees_dir = (
            Path(worktrees_dir).resolve() if worktrees_dir
            else self._repo_path / ".worktrees"
        )
        self.path: Path = self._worktrees_dir / name   # 隔离工作区路径
        self._created = False   # __aenter__ 是否成功创建了 worktree

    # ------------------------------------------------------------------
    # 上下文协议
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "WorktreeSession":
        self._worktrees_dir.mkdir(parents=True, exist_ok=True)

        if self.path.exists():
            raise WorktreeError(f"Worktree '{self._name}' already exists at {self.path}")

        # git worktree add <path> -b wt/<name> <base>
        ok, out = await self._run_git(
            ["worktree", "add", str(self.path), "-b", f"wt/{self._name}", self._base]
        )
        if not ok:
            raise WorktreeError(f"git worktree add failed: {out}")

        self._created = True

        # 可选：绑定到任务
        if self._task_engine is not None and self._task_id is not None:
            self._task_engine.bind_worktree(self._task_id, self._name)

        logger.info("[worktree] created: %s at %s", self._name, self.path)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        无论是否异常，强制清理：git worktree remove --force → 删分支 → 兜底 rmtree。
        清理本身的错误只记日志，不抛出（避免掩盖原始异常）。
        """
        await self._cleanup()
        # exc_val 不处理 → 原异常正常传播

    # ------------------------------------------------------------------
    # 显式清理（也可手动调用）
    # ------------------------------------------------------------------

    async def _cleanup(self) -> None:
        if not self._created:
            # __aenter__ 没成功创建，无需 git 清理（但兜底删可能残留的目录）
            if self.path.exists():
                shutil.rmtree(self.path, ignore_errors=True)
            return

        # 1. 强制删除 worktree
        ok, out = await self._run_git(
            ["worktree", "remove", str(self.path), "--force"]
        )
        if not ok:
            logger.warning("[worktree] remove failed for %s: %s", self._name, out)

        # 2. 删除分支（即使上一步失败也尝试）
        ok2, out2 = await self._run_git(["branch", "-D", f"wt/{self._name}"])
        if not ok2:
            logger.debug("[worktree] branch delete %s: %s", self._name, out2)

        # 3. 兜底：物理删除残留目录
        if self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)

        self._created = False
        logger.info("[worktree] removed: %s", self._name)

    async def close(self) -> None:
        """显式清理（不依赖 with 时用）。"""
        await self._cleanup()

    # ------------------------------------------------------------------
    # 内部：async git 执行
    # ------------------------------------------------------------------

    async def _run_git(self, args: list[str]) -> tuple[bool, str]:
        """跑 git 命令，返回 (success, merged_output)。不抛异常。"""
        cmd = "git " + " ".join(_shell_quote(a) for a in args)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(self._repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            out = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()
            return proc.returncode == 0, (out[:5000] if out else "(no output)")
        except asyncio.TimeoutError:
            return False, "Error: git timeout (60s)"
        except Exception as e:  # noqa: BLE001
            return False, f"Error: {e}"


def _shell_quote(arg: str) -> str:
    """简易 shell 引用：含空格等特殊字符时加双引号。"""
    if arg and re.fullmatch(r"[A-Za-z0-9._/=@:-]+", arg):
        return arg
    return '"' + arg.replace('"', r'\"') + '"'
