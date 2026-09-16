# P2 Repo Map：增量索引与真实任务消融改动内容

日期：2026-09-16

## 1. 本轮目标

P2 不是继续扩展 Agent 功能面，而是解决 Repo Map 在大仓库和长会话中的两个结构性问题：

1. 仓库结构解析结果只能进程内复用，重启后需要重新扫描；
2. Agent 或用户只修改少量文件时，旧实现会在下一次 Repo Map 构建时按仓库级重新扫描。

同时补齐两类证据：

- 持久化 / 增量实现是否保持旧 Query-aware Repo Map 的排序与渲染语义；
- No Repo Map / Static / Query-aware / Incremental Query-aware 四组真实 Coding Agent 消融是否具备可复现 harness。

本轮明确不把 retrieval benchmark、索引阶段耗时或 deterministic regression 包装成 Coding Agent 成功率。

---

## 2. 最终架构

生产默认路径现在是：

```text
Repository State Detector
        ↓
Changed Files
        ↓
Persistent Structural Index (SQLite)
        ↓
Query-aware Ranking
        ↓
Token Budget Rendering
        ↓
Model-visible Repo Map
```

核心边界：

```text
仓库变化
→ changed-file detection
→ persistent index update

用户需求变化
→ query rerank
→ Token Budget render
```

即：

> 任务变化不再等价于仓库变化；Query 改变只重排视图，不应触发源码重新 parse。

---

## 3. 主要代码改动

### 3.1 `context/repo_index.py`

新增版本化 SQLite 结构索引，持久化：

- file path / content hash / mtime / size / language；
- symbols；
- imports；
- identifier/reference counts；
- repository state metadata。

索引默认存储在仓库外部 cache 目录，不污染目标仓库。

schema 不兼容或 SQLite 损坏时，优先安全重建，而不是继续读取不可信索引。

### 3.2 `context/incremental_repo_map.py`

新增 `PersistentRepoMap`：

- 首次使用：full scan → parse → persist；
- 后续进程：load index → detect changes；
- 仓库未变化：warm load，不重新 parse；
- Query 改变：只重新 ranking/render；
- 已知 changed path：只更新目标文件；
- 外部仓库变化：通过 Repository State Detector 找 changed paths；
- 检测不可靠、索引异常或显式 refresh：fallback full rebuild。

暴露阶段 telemetry：

- full rebuild；
- warm load；
- incremental update；
- parsed paths；
- fallback reason。

### 3.3 `context/repository_state.py`

将 P2 change detection 与 P1 已冻结的 `repository_fingerprint()` 契约分离。

P2 状态覆盖：

- Git HEAD；
- staged；
- unstaged；
- untracked；
- add / modify / delete / rename；
- dirty file 内容变化；
- 非 Git 目录安全 fallback。

这次曾出现过一次跨模块兼容性回归：如果直接扩展原 `repository_fingerprint()` 语义，会影响 Context Compaction 的既有测试。最终实现恢复旧 fingerprint 契约，把 P2 所需状态放到独立 `RepositoryState` / change detector 中。

### 3.4 `agent/core.py`

`AgentConfig.repo_map_mode` 支持：

- `none`
- `static`
- `query_aware`
- `incremental`

生产默认使用 `incremental`。

成功执行 `file_write` / `file_edit` / `edit` 后：

- 如果工具参数能提供目标 path，直接调用 changed-file incremental update；
- 否则标记下一轮做 repository sync；
- 不再把“成功写一个文件”等价为“下一 step 强制全仓重建”。

同时增加 Repo Map telemetry，供 benchmark / Agent ablation 读取。

### 3.5 `agent/runner.py`

共享 Chat history 边界复用同一个 `PersistentRepoMap` 实例，避免 `getattr(..., default)` 的 eager default 意外创建额外 SQLite index 实例。

### 3.6 `agent/prompt.py`

system prompt 调整为稳定内容优先：

```text
stable system rules
stable tool schema
...
dynamic repository context
```

这只证明 prompt layout 已为 prefix reuse 做结构优化。

当前没有真实 provider cached-token 对照数据，因此不能宣称缓存命中率提升。

---

## 4. strict benchmark 暴露并修复的真实语义 bug

第一次 strict 12-case benchmark 没有通过。

失败 case：

```text
token-history-trimming
```

根因不是 benchmark 本身，而是 SQLite reference aggregation 与旧 `_apply_reference_scores()` 在“同名 symbol 多文件定义”场景下语义不同。

旧逻辑：

> 如果引用来源文件本身也定义该 symbol，则该来源对这个 symbol 的跨文件 reference counting 整体跳过。

最初 SQLite 实现只过滤了 self-edge，仍可能把 occurrence 计给另一个 owner 文件，因此会改变 centrality，进而改变 ranking。

最终在 `context/repo_index.py` 中使用 `NOT EXISTS` 检查 source file 是否也是该 symbol owner，恢复 legacy 语义，并增加 duplicate-definition regression。

没有通过放宽 tolerance 或修改 benchmark ground truth 处理该问题。

---

## 5. 测试与回归

新增 / 扩展的关键测试：

- `tests/test_repo_map_persistent.py`
- `tests/test_repo_map_product_behavior.py`
- `tests/test_repo_map_prompt_layout.py`
- `tests/test_repo_map_agent_ablation.py`
- `tests/test_repo_map_persistent_benchmark.py`
- 原 `tests/test_repo_map_improvements.py`
- 原 `tests/test_repo_map_ablation.py`

覆盖：

- cold build；
- 第二实例 warm reuse；
- Query change 只 rerank；
- add / modify / delete / rename；
- staged / unstaged / untracked；
- HEAD change；
- dirty content change；
- non-Git fallback；
- schema mismatch / corrupted SQLite；
- old symbol cleanup；
- reference graph 更新；
- duplicate symbol owner 语义；
- index 位于 repo 外部；
- Chat 跨轮 Query 改变仍复用结构索引；
- prompt 稳定区位于动态 Repo Map 前；
- A/B/C/D Agent harness 配置；
- benchmark phase 分离。

GitHub Actions 最终成功轮次中：

```text
801 passed, 1 skipped
```

随后 duplicate-definition regression 也进入定向回归，并与 strict benchmark 一起通过。

---

## 6. 冻结 retrieval 语义

证据：

- `evals/results/repo_map_ablation/report.json`
- `evals/results/repo_map_persistent_benchmark/report.json`

旧 Static → Query-aware 冻结结果仍为：

```text
MRR:
0.096954 → 0.318750

Token Budget 内 target recall:
0.364914 → 0.635251
```

P2 strict 12-case 检查：

```text
semantic equivalent: 12/12
full ranking equivalent: 12/12
visible-set equivalent: 12/12
rendering equivalent: 12/12
```

与冻结 Query-aware 指标对比：

```text
MRR delta = 0
budget target recall delta = 0
recall@1 delta = 0
recall@3 delta = 0
recall@5 delta = 0
mean target rank delta = 0
```

因此可以说：

> 持久化增量实现保持了冻结 12-case Query-aware retrieval/ranking/rendering 语义。

不能从这里推出 Coding Agent 成功率提升。

---

## 7. P2 phase benchmark

环境与完整原始数据见：

`evals/results/repo_map_persistent_benchmark/report.json`

5-run CI 中位数：

| Phase | Median |
| --- | ---: |
| legacy build | `0.6097s` |
| persistent cold build | `0.9605s` |
| warm load | `0.2967s` |
| query-only rerank | `0.2080s` |
| single-file incremental update | `0.1700s` |
| two-file incremental update | `0.1766s` |
| explicit full rebuild | `0.8045s` |

同时确认：

- warm load 重新 parse `0` 文件；
- single-file update 只 parse `1` 文件；
- two-file update 只 parse `2` 文件；
- benchmark 后 working tree clean；
- semantic equivalence 为 true。

重要边界：

1. **cold build 没有变快。** 首次 persistent build 比 legacy build 更慢，因为增加 SQLite 持久化成本。
2. 收益主要发生在 warm reuse 和 changed-file update。
3. 上述数字是 Repo Map 阶段耗时，不是 Agent E2E latency。
4. 这些时间来自对应 CI 机器和当前 Forge Agent snapshot，不应外推为所有仓库的固定倍率。

---

## 8. Real Coding Agent A/B/C/D harness

新增：

- `evals/repo_map_agent_ablation.py`
- `evals/fixtures/repo_map_agent_cases.json`
- `tests/test_repo_map_agent_ablation.py`

固定四组：

```text
A. no_repo_map
B. static_repo_map
C. query_aware_repo_map
D. incremental_query_aware_repo_map
```

harness 使用真实生产路径：

```text
ExecutionRunner
→ Agent
→ Tool lifecycle
→ Completion Guard
→ Independent Acceptance
→ Trace v2
```

采集：

- solved / hidden verifier；
- first target-file read step；
- unique files read；
- search_text / find_files / find_symbol；
- input / output / total token；
- cached input token；
- latency；
- Repo Map build / warm / incremental phase telemetry。

当前冻结结果：

`evals/results/repo_map_agent_ablation/report.json`

明确为：

```text
execution_status = not_executed
real_model_executed = false
rows = 0
reason = provider_credentials_not_available_in_ci
```

因此当前没有 Repo Map A/B/C/D 的真实模型成功率结论。

---

## 9. Evidence Pack 边界

当前可以安全表述：

> 将 Repo Map 拆为持久化 SQLite 结构索引与 Query-aware 任务视图；Query 改变只 rerank，已知文件修改只增量更新对应文件。冻结 12-case commit-history 协议中，增量版本与原 Query-aware ranking/rendering 完全一致；CI phase benchmark 中 warm load、单文件更新、双文件更新中位耗时分别为约 0.297s、0.170s、0.177s，而 explicit full rebuild 约 0.805s。

如果空间有限，可保留更稳定的 retrieval 主张：

> Query-aware Repo Map 在 12-case frozen commit-history benchmark 上将 MRR 从 0.097 提升到 0.319、Token Budget 内 target recall 从 0.365 提升到 0.635；随后实现持久化 changed-file 增量索引，并用 strict equivalence regression 保证排名语义不变。

当前不能说：

- “Repo Map 让 Coding Agent 成功率提升 X%”；
- “首次索引更快”；
- “整个 Agent 快 4×/5×/71×”；
- “只扫描任务相关模块”；
- “Prompt Cache 命中率提高 X%”；
- “P2 real-model ablation 已完成”。

---

## 10. 后续只剩的实验项

代码主链已经完成。剩余是证据，而不是继续扩功能：

1. 在有 provider credential 的环境运行固定 4-case × 4-variant Real Coding Agent ablation；
2. 最好对每个 cell 做多次重复，再讨论稳定 solved/token/latency 差异；
3. 单独做真实 provider cached input token 对照，验证 prompt layout 是否带来 cache 收益；
4. 如果后续目标转向超大仓库，再针对 cold build 做独立优化，不把 warm/incremental 收益误写为 cold-start 收益。

---

## 11. 本地验收建议

拉取 `dev` 后先跑：

```bash
pytest -q \
  tests/test_repo_map_improvements.py \
  tests/test_repo_map_ablation.py \
  tests/test_repo_map_persistent.py \
  tests/test_repo_map_product_behavior.py \
  tests/test_repo_map_prompt_layout.py \
  tests/test_repo_map_agent_ablation.py \
  tests/test_repo_map_persistent_benchmark.py

python -m evals.verify_evidence_pack
```

再跑全量：

```bash
pytest -q
```

如需自己重新采样 phase benchmark，请使用新的输出目录，避免覆盖冻结证据：

```bash
python -m evals.repo_map_persistent_benchmark \
  --repo . \
  --output /tmp/forge-repo-map-benchmark \
  --repetitions 5 \
  --strict
```

真实模型 A/B/C/D 需要有效 provider 配置：

```bash
python -m evals.repo_map_agent_ablation \
  --output /tmp/forge-repo-map-agent-ablation \
  --repetitions 1
```

若没有凭据，只验证 harness：

```bash
python -m evals.repo_map_agent_ablation \
  --validate-only \
  --output /tmp/forge-repo-map-agent-ablation
```
