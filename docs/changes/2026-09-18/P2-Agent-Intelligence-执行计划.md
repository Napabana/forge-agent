# P2 Agent Intelligence 执行计划落盘

日期：2026-09-18  
计划入口：`docs/todo/P2-Agent-Intelligence-执行计划.md`

## 背景

P0/P1 主线与 Docker E2E 执行语义已完成并由用户本地验证。下一阶段不继续横向堆普通 Tool/UI，而是进入 **P2 Agent Intelligence**，目标是建立可评测的任务级规划、恢复、能力复用与经验演化闭环。

仓库历史已经存在 “P2 Repo Map” 证据与实验，因此本阶段统一使用 **P2 Agent Intelligence / P2-0 ～ P2-5** 命名，不重写旧 P2 Repo Map 的历史含义。

## 六个子任务

| 编号 | 任务 | 主要参考设计 | 状态 |
| --- | --- | --- | --- |
| P2-0 | Coding Agent Evaluation Harness | Anthropic Agent Evals；SWE-bench/SWE-agent eval 思路 | TODO |
| P2-1 | Structured Planning | Claude Code Plan Mode；Aider Architect Mode | TODO |
| P2-2 | Failure-aware Recovery + Replanning | SWE-agent RetryAgent/max_requeries；Aider lint/test fix loop | TODO |
| P2-3 | Agent Skills | Anthropic Agent Skills；OpenAI/Codex Skills progressive disclosure | TODO |
| P2-4 | MCP Client / Tool Adapter | MCP Host-Client-Server 架构；OpenAI Skills + MCP capability/workflow 分离 | TODO |
| P2-5 | Trajectory-driven Skill Evolution | SWE-agent trajectories/demonstrations；OpenAI Skill Evals | TODO |

## 固定执行顺序

```text
P2-0 Evaluation Harness
        ↓
P2-1 Structured Planning
        ↓
P2-2 Failure-aware Recovery
        ↓
P2-3 Agent Skills
        ↓
P2-4 MCP
        ↓
P2-5 Trajectory-driven Evolution
```

P2-0 必须先于其它能力完成，原因是后续每个 feature 都要有 baseline/A-B，而不是实现完之后再补 benchmark。

## 不变边界

- 不新增第二套 Agent loop；
- 不绕过 `ExecutionRunner → Agent → ToolExecutor`；
- MCP/Skill 继续服从 Permission / Hook / Cancel / Sandbox / Trace；
- infrastructure failure 不伪装成可恢复智能行为；
- Skills 不允许直接绕过 ToolExecutor 执行脚本；
- 自进化只生成 candidate，必须经过 Eval Gate + 人工确认后才能 promote；
- 默认先做 deterministic offline regression，真实模型结果单独冻结并标注 small sample；
- 不覆盖历史 fixture/result，不为了指标静默重跑。

## 每项完成要求

每个 P2 子任务必须同时留下：

1. 源码实现；
2. deterministic regression；
3. Eval Harness 可运行的 case/variant；
4. docs/changes 改动日志；
5. Evidence Pack claim 与边界；
6. 如有真实模型运行，保留完整 metadata/raw/report，不外推总体成功率。

详细范围、参考设计与验收条件见：

`docs/todo/P2-Agent-Intelligence-执行计划.md`
