"""task/ — 任务状态机与 DAG 依赖引擎（M1 Task 1.1）。

基于 SQLite(WAL) 的 TaskEngine，替代 s20 里基于 JSON 文件的任务系统，
为多智能体并发认领提供 ACID 事务与原子性。
"""

from task.engine import (
    Task,
    TaskEngine,
    TaskNotFound,
    TaskNotClaimable,
    TaskNotCompletable,
)

__all__ = [
    "Task",
    "TaskEngine",
    "TaskNotFound",
    "TaskNotClaimable",
    "TaskNotCompletable",
]
