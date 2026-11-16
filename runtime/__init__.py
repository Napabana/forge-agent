"""runtime/ — 执行隔离运行时（M1 Task 1.2 起）。

WorktreeSession：基于 Git Worktree 的异步生命周期，
既支持事务式自动回滚，也支持 orchestrator 显式保留有修改的工作区。
"""

from runtime.worktree import (
    WorktreeArtifact,
    WorktreeChanges,
    WorktreeDisposition,
    WorktreeError,
    WorktreeFinalizeAction,
    WorktreeResultPolicy,
    WorktreeSession,
    validate_worktree_name,
)

__all__ = [
    "WorktreeArtifact",
    "WorktreeChanges",
    "WorktreeDisposition",
    "WorktreeError",
    "WorktreeFinalizeAction",
    "WorktreeResultPolicy",
    "WorktreeSession",
    "validate_worktree_name",
]
