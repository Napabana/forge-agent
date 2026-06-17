"""runtime/ — 执行隔离运行时（M1 Task 1.2 起）。

WorktreeSession：基于 Git Worktree 的异步事务上下文，
为每个任务提供物理隔离的代码工作区，退出时强制回滚清理。
"""

from runtime.worktree import WorktreeSession, WorktreeError, validate_worktree_name

__all__ = ["WorktreeSession", "WorktreeError", "validate_worktree_name"]
