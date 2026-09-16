# Runner 独立验收闭环

## 本轮目标

扩展现有 `AcceptanceContract`，支持必改/禁改路径和 Runner 持有的隐藏 verifier，并在
结果中区分 Agent、独立验收和交付状态。本批不执行 commit、push 或创建 PR。

## 行为变化

- `required_paths` 指定本轮必须发生最终内容变化的文件。
- `forbidden_paths` 指定本轮不得发生最终内容变化的文件。
- 普通运行只对契约指定文件保存运行前后 SHA-256 指纹，不依赖工作区原本干净。
- 隔离运行复用 `WorktreeArtifact.changed_files`，成果已被 discard 时独立验收失败。
- 隐藏 verifier 只接收仓库路径并在 Agent 返回后执行，不进入 prompt 或 History。
- Agent 未成功时独立验收标记 `skipped`；独立验收失败不会覆盖 Agent 原始状态。
- `RunResult.status`、`acceptance_status`、`delivery_status` 分别保留三层结果。

## 修改文件

- `agent/runner.py`：扩展契约、保存路径基线、执行独立验收并处理 verifier 异常。
- `agent/task.py`：新增独立验收状态和错误字段。
- `tests/test_runner.py`：新增路径、隐藏 verifier、状态分层和用户已有修改保护测试。
- `Forge-Agent-P0-P1-实施计划.md`、`TODO-P0-P1.md`、`AGENTS.md`：同步真实状态。

## 测试

WSL 默认发行版，环境 `source ~/.venvs/forge-agent/bin/activate`。初版 Git status 方案通过
测试后，经代码复核发现它会混入运行前已有修改，因此改为契约文件指纹并重新验证：

```text
python -m pytest -q tests/test_runner.py
4 passed in 1.82s

python -m pytest -q tests/test_evals.py
3 passed in 2.25s
```

没有失败节点需要重跑，未运行全量测试，也未调用真实模型。

## 已知边界

- 第一版路径约束只接受文件，不递归解释目录或 glob。
- 隐藏 verifier 当前返回布尔值；结构化命令、超时和资源预算尚未加入契约。
- 模型 FINISH 尚未拆成独立领域字段，目前可从 Trace 与 Agent 最终状态判断。
- 自动 PR 尚未消费 `acceptance_status`，下一批才接通确定性交付门禁。
- 没有实现 commit/push/PR、MCP、多 Agent、multi-tool call 或性能结论。

## 保护状态与下一步

`config/default.yaml` 未修改，`stash@{0}` 保留；本地 Markdown 不强制暂存。下一批为
实施计划第 12.2 节 H-交付闭环，需先确认两个拟修改文件及理由。
