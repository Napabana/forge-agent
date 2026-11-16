"""
scripts/m1_demo.py

M1 阶段基线链路验收（Task 1.3）。

把 Task 1.1（SQLite TaskEngine）与 Task 1.2（WorktreeSession）拼成端到端流程，
验证「数据库事务 + 文件系统隔离」的接口契约：

    初始化 DB → 插任务 → async with WorktreeSession（隔离区）→ 模拟 Agent 写入
    → 主动抛异常（模拟崩溃 / 测试失败）→ 退出上下文 → 断言：
        1. 隔离区被强制删除（文件系统干净回滚）
        2. 数据库任务状态置为 failed
        3. 主代码库工作区不受影响

纯本地运行，不调 LLM、不需要真实 Issue。

用法（项目根目录）：
    source /root/learn-claude-code/venv/bin/activate
    python scripts/m1_demo.py

退出码：0 全部断言通过；1 失败。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 项目根加入 sys.path（脚本在 scripts/ 下，需回到项目根才能 import 包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.worktree import WorktreeSession          # noqa: E402
from task.engine import STATUS_FAILED, TaskEngine     # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
)
logger = logging.getLogger("m1_demo")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m"


def _ok(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def _fail(s: str) -> str:
    return f"\033[31m{s}\033[0m"


def _step(n: int, msg: str) -> None:
    print(_bold(f"\n[{n}] {msg}"))


def _make_temp_repo(base: Path) -> Path:
    """在 base 下建一个带初始提交的临时 git 仓库，返回其路径。"""
    repo = base / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "demo@forge.agent"],
        ["config", "user.name", "forge-demo"],
    ):
        subprocess.run(["git"] + args, cwd=repo, check=True,
                       capture_output=True, text=True)
    (repo / "README.md").write_text("# demo repo\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo,
                   check=True, capture_output=True, text=True)
    return repo


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

async def run_demo() -> int:
    print(_bold("=" * 64))
    print(_bold("  M1 基线链路 demo：TaskEngine + WorktreeSession 端到端"))
    print(_bold("=" * 64))

    # 用一个临时根目录，隔离 db 与 repo，跑完整体删除
    root = Path(tempfile.mkdtemp(prefix="forge-m1-"))
    db_path = root / "tasks.db"
    repo_path = _make_temp_repo(root)
    print(f"临时工作根目录: {root}")
    print(f"SQLite DB:       {db_path}")
    print(f"git 仓库:        {repo_path}")

    assertions: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        assertions.append((name, cond, detail))
        mark = _ok("✓") if cond else _fail("✗")
        print(f"  {mark} {name}" + (f"  ({detail})" if detail and not cond else ""))

    # ------------------------------------------------------------------
    # Step 1: 初始化 TaskEngine
    # ------------------------------------------------------------------
    _step(1, "初始化 TaskEngine（SQLite WAL）")
    engine = TaskEngine(db_path)
    wal = engine._conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("WAL 已开启", wal.lower() == "wal", f"journal_mode={wal}")

    # ------------------------------------------------------------------
    # Step 2: 插入任务
    # ------------------------------------------------------------------
    _step(2, "插入测试任务")
    task_id = engine.create_task("修复 parser bug", description="M1 demo 任务")
    print(f"  task_id = {task_id}")
    check("任务初始为 pending", engine.get_task(task_id).status == "pending")

    worktree_path_ref: dict[str, Path] = {}

    # ------------------------------------------------------------------
    # Step 3: 进入隔离工作区，模拟 Agent 写入，然后崩溃
    # ------------------------------------------------------------------
    _step(3, "进入 WorktreeSession 隔离区，模拟 Agent 写入后抛异常")
    crashed = False
    try:
        async with WorktreeSession(repo_path, "m1-demo", engine, task_id) as wt:
            worktree_path_ref["path"] = wt.path
            print(f"  隔离区路径: {wt.path}")
            check("隔离区已创建", wt.path.exists())
            check("任务已绑定 worktree",
                  engine.get_task(task_id).worktree == "m1-demo")
            check("任务认领后应为 pending（本 demo 不 claim，仅绑定）",
                  engine.get_task(task_id).status == "pending")

            # 模拟 Agent 在隔离区内改代码
            (wt.path / "parser.py").write_text("# buggy fix\n")
            print(f"  在隔离区写入: parser.py")
            check("隔离区文件可见", (wt.path / "parser.py").exists())

            # 主动认领任务（进入 in_progress），再模拟崩溃
            engine.claim_task(task_id, owner="agent-demo")
            check("任务认领为 in_progress",
                  engine.get_task(task_id).status == "in_progress")

            raise RuntimeError("模拟 Agent 崩溃 / 测试失败")
    except RuntimeError:
        crashed = True
    check("异常被抛出", crashed)

    # ------------------------------------------------------------------
    # Step 4: 断言回滚 —— 文件系统 + 数据库
    # ------------------------------------------------------------------
    _step(4, "断言：异常后隔离区被强制清理、任务状态置 failed")
    wt_path = worktree_path_ref["path"]
    check("隔离区目录已删除", not wt_path.exists())
    check("wt/ 分支已删除",
          subprocess.run(["git", "branch", "--list", "wt/m1-demo"],
                         cwd=repo_path, capture_output=True, text=True).stdout.strip() == "")
    check("主仓库工作区无隔离区残留文件",
          not (repo_path / "parser.py").exists())

    # 业务约定：崩溃后任务标记为 failed（demo 主动 fail_task）
    engine.fail_task(task_id)
    check("任务状态置为 failed",
          engine.get_task(task_id).status == STATUS_FAILED)

    engine.close()

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    print(_bold("\n" + "=" * 64))
    passed = sum(1 for _, c, _ in assertions if c)
    total = len(assertions)
    if passed == total:
        print(_ok(f"  全部通过：{passed}/{total}  —— M1 基线链路 OK"))
        print(_bold("=" * 64))
        return 0
    print(_fail(f"  失败：{total - passed}/{total}  —— 见上方 ✗ 项"))
    print(_bold("=" * 64))
    return 1


def main() -> None:
    try:
        rc = asyncio.run(run_demo())
    finally:
        # 清理临时根目录（保留 db/repo 供排查的话可注释掉）
        # 这里 demo 性质，直接清理
        pass
    sys.exit(rc)


if __name__ == "__main__":
    main()
