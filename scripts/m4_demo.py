"""
scripts/m4_demo.py

M4 主循环集成端到端验收（简历交付物）。

跑通完整闭环：
    输入 Issue 文本 → TaskEngine 记账 → 隔离 worktree → 完整 ReAct 循环
    （MockBackend 驱动，不烧 API）→ 产出 JSONL 审计日志（含 worktree/权限事件）
    → 自动回滚 worktree。

任务：scratch repo 里 calc.py 的 add() 写成了减法（bug），agent 读代码 →
修文件 → 完成。用 MockBackend 预编 action，完全可复现。

用法（项目根目录）：
    fa && python scripts/m4_demo.py
退出码：0 全部断言通过；1 失败。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

# 项目根加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.event_log import EventLog                            # noqa: E402
from agent.orchestrate import orchestrate_run                   # noqa: E402
from agent.task import ActionType, Action, Task, ToolCall       # noqa: E402
from agent.core import AgentConfig                              # noqa: E402
from ipc.bus import AgentBus                                    # noqa: E402
from llm.base import MockBackend                                # noqa: E402
from task.engine import STATUS_COMPLETED, TaskEngine            # noqa: E402
from tools.base import ToolRegistry                             # noqa: E402
from tools.file_tool import FileReadTool, FileWriteTool         # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
)
logger = logging.getLogger("m4_demo")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _c(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s

def green(t): return _c(t, "32")
def red(t):   return _c(t, "31")
def bold(t):  return _c(t, "1")


# scratch repo：含 bug 的 calc.py + 测试
CALC_BUG = "def add(a, b):\n    return a - b   # bug: 应是 a + b\n"
CALC_FIX = "def add(a, b):\n    return a + b\n"
TEST_CALC = "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"


def _make_temp_repo(root: Path) -> Path:
    """在 root 下建一个有初始提交的 scratch git 仓库，含 bug 的 calc.py。"""
    repo = root / "repo"
    repo.mkdir()

    def git(args):
        subprocess.run(["git"] + args, cwd=str(repo),
                       capture_output=True, check=True)

    git(["init", "-q"])
    git(["config", "user.email", "demo@forge-agent"])
    git(["config", "user.name", "demo"])
    (repo / "calc.py").write_text(CALC_BUG)
    (repo / "test_calc.py").write_text(TEST_CALC)
    git(["add", "."])
    git(["commit", "-q", "-m", "init: calc with add() bug"])
    return repo


def _build_registry(cfg, confirm_callback, runtime, worktree_path):
    """registry_builder：给 orchestrator 用的最小 registry（文件工具）。"""
    return (
        ToolRegistry()
        .register(FileReadTool(workspace=str(worktree_path)))
        .register(FileWriteTool(workspace=str(worktree_path)))
    )


async def _print_bus_events(bus: AgentBus) -> None:
    """订阅并打印 bus 上的生命周期事件（tasks.* / worktree.*）。"""
    # subscribe 一次一个 topic；用 messages() async generator（自动清理队列）。
    async def _drain(topic: str):
        async for msg in bus.messages(topic):
            print(f"  {bold('bus')} → {msg.topic}: {msg.content}")

    await asyncio.gather(_drain("tasks.*"), _drain("worktree.*"))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def main() -> int:
    print(bold("\n🤖 M4 主循环集成 — 端到端验收\n"))

    root = Path(tempfile.mkdtemp(prefix="m4_demo_"))
    repo = _make_temp_repo(root)
    print(f"Scratch repo : {repo}")
    print(f"Task         : 修复 calc.py 的 add()（a-b → a+b）\n")

    # MockBackend 脚本：读代码 → 改文件 → 完成
    script = [
        Action(ActionType.TOOL_CALL, "先读 calc.py 确认 bug",
               ToolCall("file_read", {"path": "calc.py"})),
        Action(ActionType.TOOL_CALL, "把减法改回加法",
               ToolCall("file_write", {"path": "calc.py",
                                        "content": CALC_FIX})),
        Action(ActionType.FINISH, "add() 已修复", message="calc.py fixed"),
    ]
    backend = MockBackend(script)

    engine = TaskEngine(root / "tasks.db")
    bus = AgentBus()
    printer = asyncio.create_task(_print_bus_events(bus))

    task = Task(
        description="修复 calc.py 的 add()：当前返回 a-b，应返回 a+b。",
        repo_path=str(repo),
        max_steps=10,
    )

    print(bold("启动 orchestrate_run（worktree 隔离 + TaskEngine + bus）...\n"))
    result = await orchestrate_run(
        backend=backend,
        task=task,
        engine=engine,
        registry_builder=_build_registry,
        bus=bus,
        log_dir=str(root / "logs"),
        config=AgentConfig(max_steps=10),
    )

    # 给 bus 打印任务一点时间
    await asyncio.sleep(0.1)
    printer.cancel()

    print(bold("\n──────── 结果 ────────"))
    print(f"  status      : {green(result.status.value) if result.is_success() else red(result.status.value)}")
    print(f"  steps       : {result.steps_taken}")
    print(f"  task_id     : {result.task_id}")
    print(f"  task status : {engine.get_task(result.task_id).status}")

    # 断言
    print(bold("\n──────── 断言 ────────"))
    failures = []

    def check(cond, msg):
        mark = green("✓") if cond else red("✗")
        print(f"  {mark} {msg}")
        if not cond:
            failures.append(msg)

    check(result.is_success(), "agent 运行成功")
    check(engine.get_task(result.task_id).status == STATUS_COMPLETED,
          "TaskEngine 标记 completed")
    # Agent 在 worktree 清理前抓取 git diff 写入 RunResult.patch。
    patch = result.patch or ""
    check("return a + b" in patch and "return a - b" in patch,
          "result.patch 记录了 worktree 内 a-b → a+b 的真实修改")
    # worktree 已清理（事务回滚）
    wt_dir = repo / ".worktrees"
    check(not wt_dir.exists() or not any(wt_dir.iterdir()),
          "worktree 已自动清理")

    # JSONL 事件齐备
    log_file = next((root / "logs").glob("*.jsonl"))
    log = EventLog.open_existing(log_file)
    types = [e.event_type.value for e in log.replay()]
    print(f"\n  JSONL: {log_file}")
    print(f"  events ({len(types)}): {' → '.join(types)}")
    for need in ("task_claimed", "worktree_created", "worktree_removed",
                 "permission_decision", "task_complete"):
        check(need in types, f"JSONL 含 {need}")

    # 关键：calc.py 在主 repo 仍是 bug 版（worktree 内的修改随回滚消失）
    # —— 这验证了隔离：agent 的改动不污染主库。
    main_calc = (repo / "calc.py").read_text()
    check("a - b" in main_calc, "主库 calc.py 未被污染（worktree 隔离生效）")

    print(bold("\n──────────────────────\n"))
    if failures:
        print(red(f"✗ {len(failures)} 项失败: {failures}\n"))
        return 1
    print(green("✓ M4 端到端闭环全部通过\n"))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
