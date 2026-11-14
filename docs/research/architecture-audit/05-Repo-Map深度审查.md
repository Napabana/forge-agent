# 阶段 05：Repo Map、Tree-sitter、SQLite 与成本横向审查

> 2026-11-14｜分支 `docs/qwen38-27b-pilot-r1`；生产基线 `ca396ce6f14b88dd439bebdcb3b9634645017034`。覆盖 D10–D16，共 7 项。**仅静态源码审查，未做付费/离线 RepoMap 动态消融**。

## 1. 当前实现流

`RepoMap._discover_files` 枚举文件，`_scan` 预加载语言解析器、生成 Symbol/FileInfo、计算引用与路径重要性；`RepoMap.build(budget,query)` 按加权重要性排序，将格式化内容附到模型 system prompt。持久化路径由 `PersistentRepoMap.sync` 从 SQLite warm load + diff，必要时调用 `update_paths` 或 full rebuild，`PersistentRepoIndex` 管理五表/WAL/事务。

## 2. 逐项证据与评分

| ID | 功能 | 文件/函数 | 当前实现事实 | 风险或验证缺口 | 静态暂评分 |
|---|---|---|---|---|---|
| D10 | 文件发现/解析 | `context/repo_map.py::_discover_files/_extract_symbols` | 多语言 Tree-sitter + 正则/Python fallback，读取失败计入 report | 需验证大仓符号准确率与非法/软链路径 | 7/10（中） |
| D11 | 排序/渲染 | `context/repo_map.py::_query_relevance/RepoMap.build` | 综合 importance、reference、query 分数排序；预算以 `budget*4` 字符截断 | P1：实际 TokenCounter 可偏离设定的 token cap，需离线算 | 6/10（高） |
| D12 | Persistent index schema | `context/repo_index.py::_create_schema` | metadata/files/symbols/imports/references 五张表，保留源码 content | 存储空间、隐私及重建成本待量化 | 7/10（中） |
| D13 | SQLite transaction | `context/repo_index.py::replace_all/apply_changes/_reset` | WAL、foreign keys、事务原子更新，schema/reset 检查 | 并发 writer busy/retry 和损坏恢复须测 | 7/10（中） |
| D14 | Git/文件指纹 | `context/repository_state.py::capture_repository_state/repository_content_fingerprint` | 比较 git metadata 与 checkout-visible 内容 | 大仓哈希成本、未跟踪文件/symlink 与忽略规则 | 7.5/10（中） |
| D15 | 增量同步 | `context/incremental_repo_map.py::PersistentRepoMap.sync/update_paths` | warm load+detect_changes，失败 fallback full rebuild | 在 symlink/rename/大量修改/跨 worktree 的正确性与成本 | 7.5/10（中） |
| D16 | 工具修改感知 | `agent/core.py::_prepare_next_turn; context/incremental_repo_map.py` | 每步准备下一轮，对变更路径更新 index/repo-map 查询 | 需查 refresh 次数与每轮实际 prompt 注入长度 | 6/10（中） |

采用设计2/正确性3/可靠性2/证据2/扩展1，以上均为静态暂评分。

## 3. 直接发现：RepoMap 预算不是严格 Provider Token 限额

`context/repo_map.py::RepoMap.build`（约 222–254 行）在渲染时使用 `max_chars = budget * 4`，并以字符串字符数决定截断。`context/token_budget.py::TokenBudget.default_plan` 中的 `repo_map_cap` 则以 Token 预算表达目标，`Agent._trace_token_breakdown` 使用另一套 TokenCounter 统计。因此配置 `repo_map_budget=12000` 并不证明 Provider 实际编码后的 map 恰好 ≤12K Token。

Typer Pilot 的 Trace 每次本地估算约 15.9–16.0K Repo Map tokens，29 次累计约 463K，是需要解释的**计数与截断语义差异**，不是已证明的 Provider“多计费 4K”的结论。

建议至少用同一个 `token_budget.counter.count_text` 对最终 Repo Map 渲染结果计数并记录 `actual_map_tokens`，再与 budget、map 字符数、Provider request input 并列。必要时引入基于 counter 的二分截断/按块上限；是否修改默认 budget 必须等相关性质量证据。

## 4. Aider 横向对照（固定版本）

- [Aider `aider/repomap.py`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L103-L160)：`get_repo_map` 针对聊天文件与上下文决定 map 规模，可扩大 token 预算。
- [Aider 相关性排序与标签](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L576-L700)、[官方说明](https://aider.chat/docs/repomap.html)：基于引用/图关系选择相关符号。
- Forge 已有 query-aware 排序和 SQLite 增量，而 Aider 公开实现提供动态缩减上下文的参考。二者算法不同，**不能从 Aider 1K 默认值推出 Forge 应强改为 1K**。

| 场景 | Forge | 可研究方向 |
|---|---|---|
| 刚进入未知仓库 | 默认 RepoMap 帮助整体定位 | 使用当前候选预算或较宽预算 |
| 已定位目标 `typer/core.py` | 仍可能反复带入长 Repo Map | 按已定位文件/工具调用动态降 Map，保留恢复手段 |
| 新文件/重命名/修改 | PersistentRepoMap 增量追踪并必要时 full rebuild | 测量正确性、延迟、是否过度失效 |
| 每次请求节约 | 不等同于任务总节约 | 同时考虑停止协议、Prompt Cache、任务累计预算 |

## 5. 必需的离线消融与安全验证

| 用例 | 输入/方法 | 验收 |
|---|---|---|
| RM05-01 | Typer 固定源 commit + Step1 query；分别 build(2K/4K/8K/12K) | 同 Counter 记录实际 tokens/相关文件和目标函数覆盖 |
| RM05-02 | Step13 已定位目标函数，再生成相同预算 map | 对照是否可降 map 且保留目标引用上下文 |
| RM05-03 | 新增文件、删除、rename、git commit、非 git 工作区、修改文件内容 | Index 不漏更改；fallback 原因可解释 |
| RM05-04 | 并发读写、SQLite WAL 热路径、schema mismatch、数据库损坏 | 不返回过期错误 Symbol；恢复有日志 |
| RM05-05 | 含 symlink、超大文件、二进制、非 UTF-8、多语言解析失败 | 扫描受边界限制且不暴露仓库外文件 |
| RM05-06 | 同样模型输入/相同任务，开关动态地图 | 仅在效果不退化后才判断真实 token 成本与成功率 |

现有测试应复核 `tests/test_repo_map_improvements.py`、`test_repo_map_ablation.py`、`test_repo_map_persistent.py`、`test_repo_map_product_behavior.py`、`test_repo_map_prompt_cache_ablation.py`、`test_repo_map_reference_semantics.py`；**本轮没有运行**。

## 6. 决策与面试解释

- **P1**：统一 RepoMap 实际 Counter 与 cap 的可观测性（0.5–1.5 人日），避免配置与 Trace 指标混用。
- **P1**：在已定位相关文件后生成更小的 map / 只保留目标上下文的独立离线消融（1–3 人日）。
- **P2**：验证持续更新/SQLite 数据安全、跨 Worktree index、SQLite 失败恢复（1–3 人日）。
- **P3**：不在当前阶段重写 Tree-sitter/SQLite，也不无证据引入 Graph RAG。

问：你的 Repo Map 为何每轮可能很贵？答：模型请求每次把 Repo Map 作为 system context 的一部分发送；其预算在现实现按字符上限近似，而长期重复的固定内容随请求次数累加。索引可以减少 CPU 扫描但**不会自然消除 API 输入 Token 费用**。

**证据边界**：已完成 D10–D16 源码层横向比较与测试计划，不代表 RM05 用例已运行、输入成本已实际下降。下一阶段 06 审查 Planning/Recovery 对额外轮次的影响。
