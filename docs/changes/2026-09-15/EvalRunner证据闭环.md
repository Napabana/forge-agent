# EvalRunner 证据闭环

## 本轮目标

复用现有 `ExecutionRunner` 批量执行六个固定 fixture，在 Agent History 外运行隐藏
verifier，并为每个任务写入可复查的原始 JSONL 证据。本批不调用真实付费模型。

## 行为变化

- `EvalRunner` 每次先重置独立 fixture 仓库，再由调用方按仓库创建 `ExecutionRunner`。
- Agent 完全返回后才执行隐藏 verifier，verifier 源码不会注入模型消息。
- 每个任务完成后立即向 `runs.jsonl` 写入一条记录，保存原始 prompt、Trace 路径、
  fixture 基线到最终状态的统一 diff、Agent 状态和 verifier 状态。
- `passed` 仅在 Agent 成功且 verifier 通过时成立；Agent 声称成功但 verifier 失败记为
  `false_finish`。
- 统一 diff 使用 Python 标准库生成，不要求 fixture 预先初始化 Git 仓库。

## 修改文件

- `evals/harness.py`：为 `EvalResult` 增加带默认值的证据字段，保持旧 JSONL/报告兼容。
- `evals/run.py`：新增最小 `EvalRunner`、Trace 工具调用计数和 fixture 统一 diff。
- `tests/test_evals.py`：新增一个批量契约测试，用 MockBackend 跑完六个 fixture。
- `AGENTS.md`：新增中文注释和单行代码风格约束，并更新最后交接。
- `Forge-Agent-P0-P1-实施计划.md`、`TODO-P0-P1.md`：同步第二批真实完成状态。

`evals/report.py` 无需修改：新增字段都有默认值，现有反序列化与指标汇总保持兼容。

## 测试

WSL 默认发行版，环境 `source ~/.venvs/forge-agent/bin/activate`：

```text
python -m pytest -q tests/test_evals.py
3 passed in 2.52s
```

没有失败节点需要重跑，未运行全量测试，测试全程使用 MockBackend。

## 已知边界

- 当前由调用方提供 `ExecutionRunner` 工厂，尚未接入真实模型配置或付费调用入口。
- `cost_usd` 暂记为 `0.0`；Provider 价格换算仍需独立、可审计的成本配置。
- diff 仅覆盖 UTF-8 文本 fixture；未来出现二进制任务时再扩展证据格式。
- 现有六个小任务只验证执行和证据管线，不能据此声称真实 Agent pass@1 或性能收益。
- 尚未补上下文、Hook/timeout/cancel、进程恢复、并发隔离和自动 PR fixture。

## 保护状态与下一步

`config/default.yaml` 未修改，`stash@{0}` 保留，Markdown 按约定保持忽略且不强制暂存。
下一批是实施计划第 12.2 节的 P0-1 收口，必须先列出拟修改文件和理由并等待确认。
