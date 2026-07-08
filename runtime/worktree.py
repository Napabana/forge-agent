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
import os
import re
import shutil
import signal
import subprocess
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

    安全清理（M3，参考 s20 code.py:240-281）：
    - __aexit__ 始终强制回滚（事务一致性，不可妥协），无视 discard_changes。
    - 显式 close(discard_changes=False) 可保护有改动的 worktree 不被误删。
    - keep() 标记保留，跳过清理供 review。

    Args:
        repo_path:    宿主 git 仓库根目录（必须有至少一次提交）
        name:         worktree 名字（须通过 validate_worktree_name）
        task_engine:  可选，提供后 __aenter__ 会 bind_worktree
        task_id:      配合 task_engine，要绑定的任务 id
        base:         worktree 基点，默认 "HEAD"
        worktrees_dir: worktree 存放目录，默认 repo_path/.worktrees
        discard_changes: close() 时的默认清理策略。True（默认）= 无脑 force 清理，
                       保持事务回滚语义；False = 有改动时 refuse 清理（保护用户劳动）。
                       仅影响显式 close()，不影响 __aexit__。
    """

    def __init__(
        self,
        repo_path: str | Path,
        name: str,
        task_engine: "TaskEngine | None" = None,
        task_id: str | None = None,
        base: str = "HEAD",
        worktrees_dir: str | Path | None = None,
        discard_changes: bool = True,
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
        self._discard_changes = discard_changes
        self._keep = False      # keep() 标记：跳过清理

    # ------------------------------------------------------------------
    # 上下文协议
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "WorktreeSession":
        self._worktrees_dir.mkdir(parents=True, exist_ok=True)

        if self.path.exists():
            raise WorktreeError(f"Worktree '{self._name}' already exists at {self.path}")

        # git worktree add -b wt/<name> <path> <base>
        ok, out = await self._run_git(
            ["worktree", "add", "-b", f"wt/{self._name}", str(self.path), self._base]
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

        注意：__aexit__ 无视 discard_changes / keep —— 事务回滚语义必须可靠。
        要保留 worktree，请在 with 块外用 keep()（但 with 退出时仍会清，因为
        __aexit__ 是事务边界）。
        """
        await self._cleanup(force=True)
        # exc_val 不处理 → 原异常正常传播

    # ------------------------------------------------------------------
    # 显式清理（也可手动调用）
    # ------------------------------------------------------------------

    async def _cleanup(self, force: bool = True) -> None:
        """
        实际清理逻辑。force=True（__aexit__ 路径）始终强删；force=False
        （显式 close(discard_changes=False)）会先检查改动，有改动则 refuse。
        """
        if self._keep:
            logger.info("[worktree] kept for review: %s (branch wt/%s)",
                        self._name, self._name)
            return

        if not self._created:
            # __aenter__ 没成功创建，无需 git 清理（但兜底删可能残留的目录）
            if self.path.exists():
                shutil.rmtree(self.path, ignore_errors=True)
            return

        # 安全门：非强制模式下，有改动则 refuse 清理（参考 s20 code.py:253-266）
        if not force:
            files, commits = await self.count_changes()
            if files < 0:
                logger.warning("[worktree] cannot verify status of %s; force=True "
                               "to clean anyway", self._name)
                return
            if files > 0 or commits > 0:
                logger.info(
                    "[worktree] refuse to remove '%s': %d file(s), %d commit(s) "
                    "uncommitted. Use discard_changes=True or keep().",
                    self._name, files, commits,
                )
                return

        # 1. 强制删除 worktree
        ok, out = await self._run_git(
            ["worktree", "remove", "--force", str(self.path)]
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

    async def count_changes(self) -> tuple[int, int]:
        """
        统计 worktree 内未提交的改动（参考 s20 code.py:240-250）。
        返回 (未暂存/已暂存文件数, 未推送提交数)。无法判定时返回 (-1, -1)。
        """
        if not self._created or not self.path.exists():
            return 0, 0
        try:
            # 未提交文件（porcelain 一行一个变更）
            r1 = await self._run_git_in(str(self.path), ["status", "--porcelain"])
            files = len([
                l for l in r1.splitlines()
                if l.strip() and not l[3:].startswith(".worktrees/")
            ]) if r1 else 0
            # 未推送提交（无上游分支时 git 报错 → 视为 0 或无法判定）
            r2 = await self._run_git_in(
                str(self.path), ["log", "@{push}..HEAD", "--oneline"]
            )
            commits = len([l for l in r2.splitlines() if l.strip()]) if r2 else 0
            return files, commits
        except Exception:  # noqa: BLE001
            return -1, -1

    def keep(self) -> None:
        """标记保留 worktree 供 review，后续 _cleanup 跳过清理（参考 s20 code.py:276）。"""
        self._keep = True

    async def close(self, discard_changes: bool | None = None) -> None:
        """
        显式清理（不依赖 with 时用）。

        Args:
            discard_changes: 覆盖构造时的策略。True = 强制清理；False = 有改动则
                           refuse（保护用户劳动）。None = 用构造时 discard_changes。
        """
        force = self._discard_changes if discard_changes is None else discard_changes
        await self._cleanup(force=force)

    # ------------------------------------------------------------------
    # 内部：async git 执行
    # ------------------------------------------------------------------

    async def _run_git(self, args: list[str]) -> tuple[bool, str]:
        """跑 git 命令，返回 (success, merged_output)。不抛异常。"""
        return self._run_git_sync(str(self._repo_path), args, 60)

    async def _run_git_in(self, cwd: str, args: list[str]) -> str:
        """在指定 cwd 跑 git，返回合并输出。失败返回空串（供 count_changes 容错）。"""
        ok, out = self._run_git_sync(cwd, args, 30)
        if not ok or out == "(no output)":
            return ""
        return out

    @staticmethod
    def _run_git_sync(cwd: str, args: list[str], timeout: int) -> tuple[bool, str]:
        """同步 git 执行函数，供 async wrapper 放进自管线程池。"""
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                ["git"] + args,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = proc.communicate(timeout=timeout)
            out = (stdout + stderr).strip()
            return proc.returncode == 0, (out[:5000] if out else "(no output)")
        except subprocess.TimeoutExpired:
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                proc.wait(timeout=5)
            return False, f"Error: git timeout ({timeout}s)"
        except Exception as exc:  # noqa: BLE001
            return False, f"Error: git execution failed: {exc}"
