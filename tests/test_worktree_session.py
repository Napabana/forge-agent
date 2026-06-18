"""
tests/test_worktree_session.py

M1 Task 1.2：WorktreeSession 异步事务上下文测试。

用 tmp_path + git init 造隔离的临时 git 仓库，覆盖：
- 名字校验、正常创建/清理、异常退出强制回滚、与主仓物理隔离、
  TaskEngine 绑定、显式 close()。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runtime.worktree import (
    VALID_WT_NAME,
    WorktreeError,
    WorktreeSession,
    validate_worktree_name,
)
from task.engine import TaskEngine


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _git(args, cwd):
    """同步跑 git，测试 fixture 用。"""
    return subprocess.run(
        ["git"] + args, cwd=str(cwd),
        capture_output=True, text=True, check=False,
    )


@pytest.fixture
def repo(tmp_path):
    """一个有初始提交的临时 git 仓库。"""
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init", "-q"], r)
    _git(["config", "user.email", "t@t.com"], r)
    _git(["config", "user.name", "Test"], r)
    (r / "README.md").write_text("# repo")
    _git(["add", "."], r)
    _git(["commit", "-q", "-m", "init"], r)
    return r


@pytest.fixture
def engine(tmp_path):
    e = TaskEngine(tmp_path / "tasks.db")
    yield e
    e.close()


def _worktrees(repo):
    """列出当前仓库所有 worktree 路径。"""
    out = _git(["worktree", "list", "--porcelain"], repo).stdout
    return [l.split(" ", 1)[1] for l in out.splitlines() if l.startswith("worktree ")]


def _branches(repo):
    out = _git(["branch", "--list", "wt/*"], repo).stdout
    return [l.strip().replace("wt/", "", 1) for l in out.splitlines() if l.strip()]


# ===========================================================================
# 名字校验（移植自 s20）
# ===========================================================================

class TestValidateName:
    def test_valid(self):
        assert validate_worktree_name("feat-x_1.2") is None

    def test_empty(self):
        assert validate_worktree_name("") is not None

    def test_dot_and_dotdot(self):
        assert validate_worktree_name(".") is not None
        assert validate_worktree_name("..") is not None

    def test_bad_chars(self):
        assert validate_worktree_name("bad name") is not None      # 空格
        assert validate_worktree_name("bad/name") is not None      # 斜杠
        assert validate_worktree_name("rm -rf") is not None        # 命令注入尝试

    def test_too_long(self):
        assert validate_worktree_name("x" * 65) is not None


# ===========================================================================
# 构造校验
# ===========================================================================

class TestConstruct:
    def test_bad_name_raises(self, repo):
        with pytest.raises(WorktreeError):
            WorktreeSession(repo, "bad name")

    def test_path_attribute(self, repo):
        wt = WorktreeSession(repo, "feat")
        assert wt.path == (repo / ".worktrees" / "feat").resolve() or \
               wt.path.name == "feat"


# ===========================================================================
# 正常生命周期
# ===========================================================================

class TestLifecycle:
    async def test_create_and_cleanup(self, repo):
        async with WorktreeSession(repo, "feat-a") as wt:
            assert wt.path.exists()
            assert wt.path.is_dir()
            # worktree 已注册到 git
            registered = [Path(w) for w in _worktrees(repo)]
            assert wt.path in registered
            # 在隔离区写文件
            (wt.path / "new.txt").write_text("hi")
            assert (wt.path / "new.txt").exists()
        # 退出后：worktree 目录与 wt/ 分支都被清理
        assert not wt.path.exists()
        assert _branches(repo) == []
        assert len(_worktrees(repo)) == 1   # 只剩主 worktree

    async def test_isolation_from_main_repo(self, repo):
        """隔离区的改动不影响主仓库工作区。"""
        async with WorktreeSession(repo, "iso") as wt:
            (wt.path / "isolated.txt").write_text("only here")
        # 主仓库看不到这个文件
        assert not (repo / "isolated.txt").exists()

    async def test_explicit_close(self, repo):
        wt = WorktreeSession(repo, "feat-close")
        async with wt as session:
            assert session.path.exists()
        # 再次显式 close 幂等，不报错
        await wt.close()
        assert not wt.path.exists()


# ===========================================================================
# 异常退出强制回滚（核心：Task 1.2 的强一致性要求）
# ===========================================================================

class TestExceptionRollback:
    async def test_cleanup_on_exception(self, repo):
        wt_ref = {}
        with pytest.raises(RuntimeError, match="boom"):
            async with WorktreeSession(repo, "fail") as wt:
                wt_ref["path"] = wt.path
                (wt.path / "dirty.txt").write_text("dirty")
                raise RuntimeError("boom")
        # 即使异常，worktree + 分支也被清理
        assert not wt_ref["path"].exists()
        assert _branches(repo) == []

    async def test_cleanup_on_keyboard_interrupt(self, repo):
        """模拟 KeyboardInterrupt（崩溃）后的目录清理断言。"""
        path_ref = {}
        with pytest.raises(KeyboardInterrupt):
            async with WorktreeSession(repo, "ki") as wt:
                path_ref["path"] = wt.path
                raise KeyboardInterrupt
        assert not path_ref["path"].exists()
        assert _branches(repo) == []

    async def test_failed_enter_still_cleans_residue(self, repo):
        """__aenter__ 失败（同名已存在）时不留残留。"""
        # 先占一个同名 worktree
        async with WorktreeSession(repo, "dup") as wt1:
            assert wt1.path.exists()
            # 第二个同名应失败
            with pytest.raises(WorktreeError):
                async with WorktreeSession(repo, "dup"):
                    pass
        # 第一个正常清理
        assert not wt1.path.exists()


# ===========================================================================
# TaskEngine 集成
# ===========================================================================

class TestTaskBinding:
    async def test_bind_on_enter(self, repo, engine):
        tid = engine.create_task("do thing")
        async with WorktreeSession(repo, "task-1", engine, tid) as wt:
            assert engine.get_task(tid).worktree == "task-1"
        # 退出后绑定仍在（worktree 名字记录在任务上，便于追溯）
        assert engine.get_task(tid).worktree == "task-1"


# ===========================================================================
# M3 安全清理强化（参考 s20 code.py:240-281）
# ===========================================================================

class TestSafeCleanup:
    """discard_changes 安全门 + count_changes 改动计数 + keep() 保留。"""

    async def test_count_changes_clean_worktree(self, repo):
        """干净的 worktree：count_changes 返回 0。"""
        async with WorktreeSession(repo, "clean") as wt:
            files, commits = await wt.count_changes()
        assert files == 0

    async def test_count_changes_detects_dirty(self, repo):
        """有未提交文件：count_changes 检测到。"""
        async with WorktreeSession(repo, "dirty") as wt:
            (wt.path / "new.txt").write_text("hi")
            files, commits = await wt.count_changes()
            assert files >= 1

    async def test_close_refuses_dirty_when_discard_false(self, repo):
        """有改动 + discard_changes=False → close refuse，worktree 仍在。"""
        wt = WorktreeSession(repo, "keep1", discard_changes=False)
        await wt.__aenter__()
        (wt.path / "work.txt").write_text("work")
        await wt.close()   # discard_changes=False → refuse 清理
        assert wt.path.exists(), "有改动时应 refuse 清理，worktree 保留"
        # 手动清理避免污染后续测试
        await wt.close(discard_changes=True)

    async def test_close_forces_when_discard_true(self, repo):
        """有改动 + discard_changes=True → 清理成功。"""
        wt = WorktreeSession(repo, "keep2", discard_changes=True)
        await wt.__aenter__()
        (wt.path / "work.txt").write_text("work")
        await wt.close()
        assert not wt.path.exists()

    async def test_keep_skips_cleanup(self, repo):
        """keep() 标记后，显式 close 跳过清理，worktree 保留。"""
        wt = WorktreeSession(repo, "review")
        await wt.__aenter__()
        wt.keep()
        await wt.close()
        assert wt.path.exists(), "keep() 后 worktree 应保留供 review"
        # 手动清理
        await wt._cleanup(force=True)

    async def test_aexit_always_forces_regardless_of_discard(self, repo):
        """__aexit__ 是事务边界，无视 discard_changes，强制回滚。"""
        wt_ref = {}
        with pytest.raises(RuntimeError, match="boom"):
            async with WorktreeSession(repo, "tx", discard_changes=False) as wt:
                wt_ref["path"] = wt.path
                (wt.path / "dirty.txt").write_text("dirty")
                raise RuntimeError("boom")
        # 即使 discard_changes=False，__aexit__ 仍强制清理（事务一致性）
        assert not wt_ref["path"].exists()
