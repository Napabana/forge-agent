"""
runtime/worktree.py

Git Worktree 异步事务上下文（M1 Task 1.2）。

把 s20_comprehensive/code.py 的同步函数式 worktree 系统（171-283 行）迁移成
异步 WorktreeSession。完整保留 s20 的命令与约定：
- git worktree add <path> -b wt/<name> <base>
- git worktree remove <path> --force  +  git branch -D wt/<name>
- 名字校验规则（validate_worktree_name，code.py:181）
- 分支命名约定 wt/<name>

核心语义（M1 任务规划 Task 1.2）：
- __aenter__ 创建隔离工作区（可选绑定 TaskEngine）
- __aexit__ 无论是否抛异常，强制 git worktree remove --force + 清理残余
  → 智能体崩溃 / 测试失败时文件系统能安全、干净地回滚

并发模型：Git 命令使用 argv 形式的 subprocess 执行，并通过
asyncio.to_thread 移出事件循环；不依赖同步的 Runtime 抽象。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
from dataclasses import asdict, dataclass
from enum import Enum
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


class WorktreeResultPolicy(str, Enum):
    """一次隔离运行结束后如何处置 worktree 成果。"""

    DISCARD = "discard"
    KEEP_IF_CHANGED = "keep-if-changed"


class WorktreeFinalizeAction(str, Enum):
    """orchestrator 计算策略后交给 WorktreeSession 的具体动作。"""

    DISCARD = "discard"
    RETAIN = "retain"


class WorktreeDisposition(str, Enum):
    """worktree 最终处置结果。"""

    REMOVED = "removed"
    RETAINED = "retained"


@dataclass(frozen=True)
class WorktreeChanges:
    """相对于 worktree 创建基点的修改快照。"""

    changed_files: tuple[str, ...] = ()
    uncommitted_count: int = 0
    commit_count: int = 0
    inspection_error: str | None = None

    @property
    def has_changes(self) -> bool:
        # 无法确认时按“可能有成果”处理，避免误删。
        return bool(
            self.inspection_error
            or self.uncommitted_count > 0
            or self.commit_count > 0
        )


@dataclass(frozen=True)
class WorktreeArtifact:
    """返回给 CLI/API 的隔离工作区成果信息。"""

    disposition: WorktreeDisposition
    branch: str
    path: str | None
    base_commit: str
    head_commit: str | None
    changed_files: tuple[str, ...] = ()
    uncommitted_count: int = 0
    commit_count: int = 0
    partial: bool = False
    cleanup_required: bool = False
    warning: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


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

    生命周期：
    - 兼容的 async with 用法在 __aexit__ 始终强制回滚。
    - orchestrator 使用 create() / inspect_changes() / finalize()，可以显式保留
      有成果的 worktree。
    - close()/keep()/discard_changes 保留给旧调用，新的编排代码不再使用。

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
        self._base_commit: str | None = None

    @property
    def branch(self) -> str:
        return f"wt/{self._name}"

    @property
    def base_commit(self) -> str | None:
        return self._base_commit

    @property
    def created(self) -> bool:
        return self._created

    # ------------------------------------------------------------------
    # 上下文协议
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "WorktreeSession":
        return await self.create()

    async def create(self) -> "WorktreeSession":
        """创建 worktree，并记录稳定的基点 commit。"""
        self._worktrees_dir.mkdir(parents=True, exist_ok=True)

        if self.path.exists():
            raise WorktreeError(f"Worktree '{self._name}' already exists at {self.path}")

        ok, base_out = await self._run_git(
            ["rev-parse", "--verify", f"{self._base}^{{commit}}"]
        )
        if not ok:
            raise WorktreeError(
                f"Cannot resolve worktree base '{self._base}': {base_out}"
            )
        self._base_commit = base_out.splitlines()[0].strip()

        # git worktree add -b wt/<name> <path> <base commit>
        ok, out = await self._run_git(
            ["worktree", "add", "-b", self.branch, str(self.path), self._base_commit]
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
        正常退出时清理失败必须抛出；已有业务异常时保留原异常并记录清理失败。

        注意：__aexit__ 无视 discard_changes / keep。需要按运行结果保留成果时，
        orchestrator 应使用显式 create()/finalize() 生命周期。
        """
        try:
            await self._cleanup(force=True, honor_keep=False)
        except WorktreeError:
            if exc_val is None:
                raise
            logger.exception(
                "[worktree] cleanup also failed while propagating original error"
            )
        # exc_val 不处理 → 原异常正常传播

    # ------------------------------------------------------------------
    # 显式清理（也可手动调用）
    # ------------------------------------------------------------------

    async def _cleanup(self, force: bool = True, *, honor_keep: bool = False) -> None:
        """
        实际清理逻辑。force=True（__aexit__ 路径）始终强删；force=False
        （显式 close(discard_changes=False)）会先检查改动，有改动则 refuse。
        """
        if honor_keep and self._keep:
            logger.info("[worktree] kept for review: %s (branch wt/%s)",
                        self._name, self._name)
            return

        if not self._created:
            # __aenter__ 没成功创建，无需 git 清理（但兜底删可能残留的目录）
            if self.path.exists():
                try:
                    shutil.rmtree(self.path)
                except OSError as exc:
                    raise WorktreeError(
                        f"Failed to remove residual worktree directory "
                        f"{self.path}: {exc}"
                    ) from exc
            return

        # 安全门：非强制模式下，有改动则 refuse 清理（参考 s20 code.py:253-266）
        if not force:
            changes = await self.inspect_changes()
            if changes.inspection_error:
                logger.warning("[worktree] cannot verify status of %s; force=True "
                               "to clean anyway", self._name)
                return
            if changes.has_changes:
                logger.info(
                    "[worktree] refuse to remove '%s': %d file(s), %d commit(s) "
                    "uncommitted. Use discard_changes=True or keep().",
                    self._name, changes.uncommitted_count, changes.commit_count,
                )
                return

        # 1. 强制删除 worktree
        ok, out = await self._run_git(
            ["worktree", "remove", "--force", str(self.path)]
        )
        if not ok:
            logger.warning("[worktree] remove failed for %s: %s", self._name, out)

        # 2. 删除分支（即使上一步失败也尝试）
        ok2, out2 = await self._run_git(["branch", "-D", self.branch])
        if not ok2:
            logger.debug("[worktree] branch delete %s: %s", self._name, out2)

        # 3. 兜底：物理删除残留目录
        if self.path.exists():
            try:
                shutil.rmtree(self.path)
            except OSError as exc:
                logger.error(
                    "[worktree] cleanup failed for %s: %s", self._name, exc
                )
                raise WorktreeError(
                    f"Failed to remove worktree directory {self.path}: {exc}"
                ) from exc

        if self.path.exists():
            raise WorktreeError(f"Worktree directory still exists: {self.path}")

        self._created = False
        logger.info("[worktree] removed: %s", self._name)

    async def inspect_changes(self) -> WorktreeChanges:
        """检查相对创建基点的未提交文件和新增提交。失败时返回保守快照。"""
        if not self._created or not self.path.exists():
            return WorktreeChanges()
        if not self._base_commit:
            return WorktreeChanges(inspection_error="worktree base commit is unknown")

        try:
            ok_status, status_out = await self._run_git_result_in(
                str(self.path),
                ["status", "--porcelain=v1", "--untracked-files=all"],
            )
            if not ok_status:
                raise WorktreeError(f"git status failed: {status_out}")
            status_lines = _git_output_lines(status_out)

            ok_count, count_out = await self._run_git_result_in(
                str(self.path),
                ["rev-list", "--count", f"{self._base_commit}..HEAD"],
            )
            if not ok_count:
                raise WorktreeError(f"git rev-list failed: {count_out}")
            commit_count = int(count_out.strip())

            ok_diff, diff_out = await self._run_git_result_in(
                str(self.path),
                ["diff", "--name-only", self._base_commit],
            )
            if not ok_diff:
                raise WorktreeError(f"git diff failed: {diff_out}")

            status_files = [_porcelain_path(line) for line in status_lines]
            changed_files = tuple(sorted({
                path for path in status_files + _git_output_lines(diff_out) if path
            }))
            return WorktreeChanges(
                changed_files=changed_files,
                uncommitted_count=len(status_lines),
                commit_count=commit_count,
            )
        except (OSError, ValueError, WorktreeError) as exc:
            return WorktreeChanges(inspection_error=str(exc))

    async def count_changes(self) -> tuple[int, int]:
        """
        兼容旧调用：返回 (未提交文件数, 相对创建基点的提交数)。
        无法判定时返回 (-1, -1)。
        """
        changes = await self.inspect_changes()
        if changes.inspection_error:
            return -1, -1
        return changes.uncommitted_count, changes.commit_count

    async def finalize(
        self,
        action: WorktreeFinalizeAction,
        *,
        changes: WorktreeChanges | None = None,
        partial: bool = False,
    ) -> WorktreeArtifact:
        """显式完成 worktree 生命周期，并返回结构化成果信息。"""
        snapshot = changes or await self.inspect_changes()
        head_commit = await self._head_commit()

        if action == WorktreeFinalizeAction.RETAIN:
            return WorktreeArtifact(
                disposition=WorktreeDisposition.RETAINED,
                branch=self.branch,
                path=str(self.path),
                base_commit=self._base_commit or self._base,
                head_commit=head_commit,
                changed_files=snapshot.changed_files,
                uncommitted_count=snapshot.uncommitted_count,
                commit_count=snapshot.commit_count,
                partial=partial,
                cleanup_required=True,
                warning=snapshot.inspection_error,
            )

        await self._cleanup(force=True, honor_keep=False)
        return WorktreeArtifact(
            disposition=WorktreeDisposition.REMOVED,
            branch=self.branch,
            path=None,
            base_commit=self._base_commit or self._base,
            head_commit=head_commit,
            changed_files=snapshot.changed_files,
            uncommitted_count=snapshot.uncommitted_count,
            commit_count=snapshot.commit_count,
            partial=partial,
            cleanup_required=False,
            warning=snapshot.inspection_error,
        )

    async def _head_commit(self) -> str | None:
        if not self._created or not self.path.exists():
            return None
        ok, out = await self._run_git_result_in(str(self.path), ["rev-parse", "HEAD"])
        return out.splitlines()[0].strip() if ok and out.strip() else None

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
        await self._cleanup(force=force, honor_keep=True)

    # ------------------------------------------------------------------
    # 内部：async git 执行
    # ------------------------------------------------------------------

    async def _run_git(self, args: list[str]) -> tuple[bool, str]:
        """跑 git 命令，返回 (success, merged_output)。不抛异常。"""
        return await asyncio.to_thread(
            self._run_git_sync, str(self._repo_path), args, 60
        )

    async def _run_git_result_in(
        self, cwd: str, args: list[str], timeout: int = 30,
    ) -> tuple[bool, str]:
        return await asyncio.to_thread(self._run_git_sync, cwd, args, timeout)

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


def _git_output_lines(output: str) -> list[str]:
    if not output or output == "(no output)":
        return []
    return [line for line in output.splitlines() if line.strip()]


def _porcelain_path(line: str) -> str:
    path = line[3:] if len(line) > 3 else ""
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip().strip('"')
