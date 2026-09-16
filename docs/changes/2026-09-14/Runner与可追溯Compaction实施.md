# Runner 与可追溯 Compaction 实施

## 本轮目标

校验并记录 A-H 批次定义，执行 D（P1-1 Runner）和 E（P1-2 Compaction）的第一版
最小闭环。保持 `Agent.run()` 同步，不引入新依赖，不触碰 `config/default.yaml`。

## 批次表校验

原表依赖顺序正确，实施计划已写入修正版。两处修正：C 实际因测试暴露问题而最小修改
了 executor/base/core；E 的可追溯 checkpoint 实际涉及 Session 模型与测试，不能只写
History/EventLog。

## D：ExecutionRunner

- 新增 `agent/runner.py`：`RunRequest` 描述 task、history/session、isolate、sandbox、
  hooks、permission、cancel、prepare strategy 和 result policy；`AcceptanceContract`
  在进入 Agent 前冻结 require_changes/require_tests。
- Runner 统一 direct/isolate 的 AgentConfig、ToolExecutor、EventLog、TaskEngine 和
  `orchestrate_run()` 选择，返回结果带 `trace_path` 与 delivery status 基础字段。
- `entry/cli.py`、`entry/chat.py`、`entry/api.py`、`entry/github_issue.py` 都改为调用
  Runner；它们继续只负责终端、API 队列、Session 和 GitHub 交付等产品差异。
- Chat 的 Runner/Agent 实例跨轮复用，保留 Repo Map cache；shared History 仍显式通过
  `RunRequest` 传入。

## E：TraceableCompaction

- 新增 `context/compaction.py`，第一版为确定性 `extractive-v1`，不额外调用 LLM。
- 触发条件基于 history 实际估算 token 相对 history budget 的压力，而不是消息条数。
- 压缩保留首条原始任务约束和最近 tail；早期内容生成有方法版本和 SHA-256 hash 的
  摘要，完整 Action/Observation 仍保留在原 JSONL。
- `LLMMessage.event_ref` 将真实 Event ID 从 EventLog 带入 History；History/Session
  序列化保留该引用；Compaction checkpoint 只引用真正被压缩消息的 event IDs，
  不再错误抓取当前轮无关事件。
- checkpoint 保存 before/after token、Repo revision、summary method/hash、source
  event IDs 和 retained tail，并写 `context_compacted` Trace；Chat Session 同步保存。
- Compaction 通过 `prepare_next_turn` 注入，默认不启用，不改变现有用户的上下文行为。

## 测试真实结果

- 新增 Runner/Compaction 首轮：**3 passed（2.48s）**。
- Chat/Session/CLI/API 定向批次：**23 passed、3 failed、1 skipped（22.23s）**。
  - Chat 失败：旧 RepoMap mock 不接受已有 `force_refresh` 参数。
  - CLI 两项失败：旧测试 patch 的是迁移前 `agent.orchestrate` 接缝，并触及真实 SQLite。
- 修正测试接缝后，Chat 节点先通过；CLI 两节点随后 **2 passed（1.55s）**。
- 加入真实跨轮 `event_ref` 与 Session checkpoint 后，Runner/Compaction：
  **4 passed（4.29s）**。
- Session Store 与 usage 回归：**12 passed（6.68s）**。
- 11 个 D/E 主要 Python 文件 AST 语法检查通过；CLI、Chat、GitHub Issue 和新增模块
  导入通过。API 导入因当前 Windows 测试环境未安装 FastAPI 而未执行，对应 API 测试
  模块也是上述 `1 skipped`，不能声称 API 运行时已验证。
- 遵照用户要求未重跑全量测试，也没有为了得到整齐数字重复整个入口批次。

## 当前边界

- Runner 是同步组合根，isolate 内部使用 `asyncio.run()`；当前 CLI/API worker 适用，
  若未来从已有 event loop 直接调用，应补独立 async API。
- `AcceptanceContract` 第一版只冻结 require_changes/require_tests；必改/禁改路径、
  隐藏 verifier、资源预算和交付验收尚未实现。
- 自动 PR 仍缺独立验收、无 diff 门禁和 push/PR 幂等恢复，不能称可靠交付闭环。
- Compaction 是确定性抽取基线，不是模型语义摘要；尚缺 20～30 轮硬约束、取消/失败
  原子性、Repo revision 失效和三组消融测试。
- 旧 Session 没有 event_ref 的历史仍可读取，但这些旧消息无法回查到原 EventLog；
  新产生的 Action/Observation 才具备真实引用。
