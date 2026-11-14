# 阶段 04：Context Engineering、History、Compaction 与 Session 源码横向审查

> 2026-11-14｜分支 `docs/qwen38-27b-pilot-r1`，以相同生产实现的 `ca396ce6f14b88dd439bebdcb3b9634645017034` 为审计基线。**静态代码/现有测试核查，不代表新运行 pytest**。本报告覆盖 D01–D09、D17–D19 共 12 项，D10–D16 在阶段 05。研究用途，不修改生产代码。

## 1. 执行链与来源

`entry/context_common.py::build_context_policy` 构造每运行实例的 `TraceableCompaction`；`agent/core.py::Agent.run` 构造 `TokenBudget`、`RepoMap` 和 canonical `ConversationHistory`，每步生成 model request；`context/token_budget.py` 计算单请求保留 token；`context/compaction.py` 检查压力后决定 Stage A 工具结果剪枝或 Stage B structured summary；`entry/chat.py` 通过 `agent/session_store.py` 保存会话并支持恢复。

## 2. 完整审计表

| ID | 功能 | 当前源码 | 确认的实现/边界 | 优先检查点 | 暂评分 |
|---|---|---|---|---|---|
| D01 | Canonical History | `context/history.py::ConversationHistory` | 序列化与首消息保留；model view 不是 canonical history | 需测 resume / tool-event 关联 | 7.5/10（中） |
| D02 | TokenCounter | `context/token_budget.py::token_counter_for_model` | 有 tiktoken/保守 fallback；对未知模型的计数非供应商账单 | 配置与 Trace counter 不一致需标注 | 6.5/10（中） |
| D03 | Model-aware budget | `context/token_budget.py::TokenBudget.__init__` | 区分 context window、request output reserve、安全余量；capability fallback | 未知 Qwen 模型不应把配置 cap 当真实能力 | 7.5/10（高） |
| D04 | 单次请求预算 | `context/token_budget.py::request_pressure / history_limit_for_request` | 限制单次请求 history，未见整次 Trial 累积费用硬上限 | P1: 增 total-call/token/cost 预算与终态合同 | 5.5/10（高） |
| D05 | Stage A 工具剪枝 | `context/tool_pruning.py::DeterministicToolPruner.prune` | 可按工具语义保留 marker / event_ref | 需证明被剪结果仍可回查且不影响必要诊断 | 7/10（中） |
| D06 | Deterministic evidence | `context/structured_compaction.py::build_deterministic_evidence` | 从消息构造 evidence；不能把模型摘要当原始事实 | 离线注入失败 observation/secret 并核对边界 | 7/10（中） |
| D07 | LLM semantic summary | `context/structured_compaction.py::LLMSemanticSummarizer` | 支持总结与 fallback，消耗额外模型调用 | 测错误摘要、未授权指令保留和 side usage | 6.5/10（中） |
| D08 | Checkpoint lineage | `context/compaction.py::TraceableCompaction` | source_event_ids、checkpoint、active view 可复用 | 验证摘要失效、源前缀哈希和中断恢复 | 7.5/10（中） |
| D09 | Compaction entrypoints | `entry/context_common.py::build_context_policy; entry/chat.py` | Chat/CLI 有 policy；eval factory 当前未完整复用 | P1: 比较 CLI/Chat/Eval Context 生效值 | 5.5/10（高） |
| D17 | Session resume | `agent/session.py; entry/chat.py::ChatSession.resume_session` | pending round 与 session/repo identity 分离 | 复现中断、冲突和重复轮次 | 7/10（中） |
| D18 | Session store | `agent/session_store.py::JsonChatSessionStore` | atomic write/版本冲突与脱敏 | 核查 symlink、敏感字段和 fsync 故障 | 7/10（中） |
| D19 | 四状态域 | `context/history.py; agent/event_log.py; agent/session_store.py` | History/Trace/Session/model request 并不等价 | 显式声明导出训练数据时的不可逆缺口；当前不做训练 | 7/10（中） |

评分采用设计 2、正确性 3、安全可靠性 2、证据 2、扩展性 1。分数仅为**源码证据暂评分**，不得当成运行基准或商业产品能力比较。

## 3. 与开源实现的机制对照

| 机制 | Forge | 公开可核实参照 | 判断 |
|---|---|---|---|
| 请求组织 | 历史消息通过 `_build_messages` 与固定 system/tool schema 整合 | [Pi Agent 单轮执行协议](https://github.com/earendil-works/pi/blob/b729499585bd7dacf6e9bc52994c6f157e35f9c9/packages/agent/src/agent-loop.ts) | Pi 证明可分离 Agent turn 与宿主决策，不证明其内置 Forge 同等 Compaction |
| 上下文规模 | `TokenBudget.request_pressure` 按 context window 约束**单请求** | [Aider Repo Map](https://aider.chat/docs/repomap.html) | Aider 是相关性和规模选择参考，不是总 Trial 花费硬上限 |
| 调用后宿主扩展 | `prepare_next_turn` 可注入 Context Policy | [Codex turn loop](https://github.com/openai/codex/blob/1f1f6ebb6c156d5ab1d4745155f340b897b24f02/codex-rs/core/src/session/turn.rs#L642-L860) | 相似点是生命周期分层，不主张 Codex/Forge 使用同一 History 格式 |

## 4. 与真实 Typer Pilot 的直接关联

`context/token_budget.py:443–495` 的 `default_plan` 和 `request_pressure` 只约束每次请求，不含整个 Trial 已消耗 token；`context/compaction.py:145–210` 只在 request pressure `ratio>=threshold` 才触发，不能解决“每轮输入尚可，但 30 次请求累计昂贵”。

Pilot R1：29 次 completed 模型请求，Provider input 849,931；Trace Repo Map 本地计数合计约 463,080。这里**不能直接把 TokenCounter 和 Provider input 当同一精度的账单**，但重复前缀输入需要消融。Step 18 已有 focused PASS 后仍继续大量请求是停止策略和成本治理问题，不应全归罪 Compaction。

## 5. 最小离线反证矩阵

1. C04-01：`context_budget_cap` 大，但累计 100 次小请求超支；预期表明 TokenBudget 不限制总账单（**现状特征测试**）。
2. C04-02：未知模型/已知模型输出上限、不合法 safety margin、TokenCounter fallback；验证请求剩余 budget。
3. C04-03：恶意 Tool Result、失败 pytest 长输出、巨大 Shell Output 在 Stage A/B 压缩后仍可追溯失败原因与 event_ref。
4. C04-04：有未结束工具或恢复动作时做 Compaction，不得误报 PASS、丢失当前任务约束或伪造测试证据。
5. C04-05：同一会话 save→resume、临时文件/Repo Revision 改变、Version 冲突和重复 pending round；核查一致性。
6. C04-06：相同配置比较 CLI/Chat/Eval 实际 system、tool schema、Repo Map cap、History 和 Counter，差异作为实验变量纳入 metadata。

对应现有测试：`tests/test_model_aware_token_budget.py`、`test_tool_pruning.py`、`test_structured_compaction.py`、`test_context_policy_benchmark.py`、`test_session_store.py`、`test_chat.py`。

## 6. 决策、工作量与面试解释

**P1（0.5–2 人日设计+验证）**：加入每 Trial 调用数/累计 token/预估成本上限的**独立预算合同**，不修改 context window 语义；超预算返回可观察终态，避免覆盖 Verifier outcome。

**P1（1–3 人日）**：CLI/Eval/Chat 复用同一生效 Context 配置工厂或标注差异，保证 Benchmark 公平性。

**P2（1–3 人日）**：证明 Compaction 对诊断准确率、需要保留的失败证据无明显退化后，再调优阈值/内容。

**保留**：Canonical History 与 Trace 分离；Stage A 确定性剪枝；checkpoint lineage；模型不可用时 fallback。

面试追问：为什么不压缩就能省费？因为 Context Budget 限制**请求大小**，并非任务整体成本；Typer 反例中单次约 19–34K token，但反复发送导致累计 868K。正确手段是同时研究停止协议、Repo Map 相关性和 per-trial budget。

**证据边界**：以上 12 项通过静态阅读定位到源码与现有测试名；没有在当前运行环境执行 Forge 完整 pytest，也没有证明某种缩减策略下模型效果不降。下一阶段 05 承接 D10–D16 的 Repo Map 预算和数据库成本核查。
