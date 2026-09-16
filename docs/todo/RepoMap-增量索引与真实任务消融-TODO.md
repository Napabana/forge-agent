# Repo Map 增量索引与真实任务消融 TODO

状态更新时间：2026-09-16

当前基线：`dev`

## 1. 当前结论

P2 的生产代码主链已经完成：

```text
Repository State Detector
        ↓
Changed Files
        ↓
Persistent Structural Index
        ↓
Query-aware Ranking
        ↓
Token Budget Rendering
        ↓
Model-visible Repo Map
```

现在的核心语义是：

```text
仓库变化
→ changed-file detection
→ persistent index update

用户需求变化
→ query rerank
→ Token Budget render
```

即：

> 任务变化不再等价于仓库变化。

剩余工作主要是 Real-model Agent ablation 和真实 provider cache evidence，不是继续扩 Repo Map 功能面。

---

## 2. P2 状态总表

| Milestone | 状态 | 当前结果 |
| --- | --- | --- |
| P2-RM1：No / Static / Query-aware Agent baseline | **PARTIAL** | 4-case production-path harness 已完成；CI 无 provider credential，因此真实模型 rows=`0` |
| P2-RM2：Persistent per-file index | **DONE** | SQLite file/symbol/import/reference index、schema/version、warm reuse、repo 外 cache、corruption fallback 已完成 |
| P2-RM3：Changed-file incremental update | **DONE** | write/edit 已知 path 直接更新；外部 Git change 支持 add/modify/delete/rename/staged/unstaged/untracked/HEAD；reference 语义有 strict regression |
| P2-RM4：Prompt Cache layout | **PARTIAL** | stable rules/tool schema 已移到 dynamic Repo Map 前；尚无真实 provider cached-token 对照 |
| P2-RM5：最终消融 | **PARTIAL** | Incremental variant、strict retrieval equivalence、phase benchmark 已完成；真实模型 E2E 尚未执行 |

因此：

- **实现层：RM2 / RM3 已完成。**
- **离线正确性与性能证据：已完成。**
- **Real-model Agent success / token / latency 结论：未完成。**
- **真实 Provider cache-hit 结论：未完成。**

---

## 3. 当前真实实现

### 3.1 Query-aware View

`context/repo_map.py` 保持原 Query-aware ranking 语义。

任务 Query 会影响 path / symbol / source text / import-reference structural signals 和 Token Budget 内最终可见文件。

Query 改变时，不应重新 parse 仓库。

### 3.2 Persistent Structural Index

`context/repo_index.py` 使用 SQLite 持久化 file、symbol、import、reference 以及 repository state metadata。

索引默认位于仓库外部 cache 目录。

### 3.3 Incremental Repo Map

`context/incremental_repo_map.py` 提供 `PersistentRepoMap`。

首次：

```text
full scan
→ tree-sitter parse
→ build index
→ persist
```

后续进程：

```text
load index
→ detect repository changes
→ unchanged: warm load
→ changed: update changed files
```

Query change：

```text
index unchanged
→ rerank
→ render
```

### 3.4 Repository State Detector

`context/repository_state.py` 中 P2 change detection 与原 P1 `repository_fingerprint()` 保持职责分离。

Git 场景覆盖 HEAD、staged、unstaged、untracked、add、modify、delete、rename，以及 dirty file 在相同 status code 下继续变化。

非 Git 目录发生变化时使用安全 full-rebuild fallback。

### 3.5 Agent 接线

`agent/core.py` 的 Repo Map mode：

```text
none
static
query_aware
incremental
```

生产默认 `incremental`。

成功 `file_write` / `file_edit` / `edit`：

```text
known changed path
→ update_paths([path])
→ rerank/render
```

如果不能可靠得到 changed path，则标记下一 render boundary 做 repository sync。

### 3.6 Chat 跨轮

同一 Agent / repo 下，Query 改变会让 rendered Repo Map 失效，但 `PersistentRepoMap` 结构索引实例继续复用，不重新 parse 未变化文件。

`agent/runner.py` 已修复 shared-history boundary 中 eager default 创建额外 PersistentRepoMap 的问题。

### 3.7 Prompt layout

`agent/prompt.py` 当前将 stable system rules / tool schemas 放在 dynamic repository context 之前。

这只是结构优化；没有真实 provider `cached_input_tokens` 对照前，不宣称 cache hit 提升。

---

## 4. 正确性 contract

Persistent / Incremental 实现必须保持原 Query-aware 语义。

当前 strict contract：

1. structural semantic hash 等价；
2. full ranking 等价；
3. Token Budget visible file set 等价；
4. rendered Repo Map 等价；
5. 正式 retrieval 指标与冻结 Query-aware report delta 为 0。

第一次 strict benchmark 曾发现一个真实 reference semantic bug：同名 symbol 在多个文件定义、且 source file 自身也定义该 symbol 时，SQLite 初版只排除 self-edge，而 legacy 逻辑会跳过该 source 对此 symbol 的全部跨文件计数。

最终修改 `context/repo_index.py`，使用 owner-aware `NOT EXISTS` 语义恢复旧行为，并加入 regression。

不能通过调整 tolerance 或修改 ground truth 绕过 equivalence failure。

---

## 5. 正式冻结结果

### 5.1 Static → Query-aware Retrieval

来源：`evals/results/repo_map_ablation/report.json`

12 个真实 commit-history case：

```text
MRR:
0.096954 → 0.318750

Token Budget 内 target recall:
0.364914 → 0.635251
```

reference-count hotspot：

```text
median:
35.1176s → 0.4928s

speedup:
71.26×
```

注意：该 `71.26×` 只属于旧 reference-count 子步骤，不是完整 Repo Map，也不是 Agent E2E。

### 5.2 Persistent Strict Equivalence

来源：`evals/results/repo_map_persistent_benchmark/report.json`

12/12：semantic、ranking、visible-set、rendering 全部 equivalent。

与冻结 Query-aware 指标相比：MRR、budget recall、recall@1、recall@3、recall@5、mean target rank 的 delta 全部为 `0`。

### 5.3 Phase Benchmark

5-run GitHub Actions 中位数：

| Phase | Median |
| --- | ---: |
| legacy build | `0.6097s` |
| persistent cold build | `0.9605s` |
| warm load | `0.2967s` |
| query rerank | `0.2080s` |
| single-file incremental update | `0.1700s` |
| two-file incremental update | `0.1766s` |
| explicit full rebuild | `0.8045s` |

额外 contract：warm start reparsed files=`0`，single-file update parsed files=`1`，two-file update parsed files=`2`，benchmark 后 working tree clean。

重要结论：当前收益主要是 warm reuse / changed-file update，不是 cold start。Persistent cold build 当前比 legacy build 更慢，因为需要建立 SQLite 持久化状态。

---

## 6. Real Coding Agent Ablation

脚本：

- `evals/repo_map_agent_ablation.py`
- `evals/fixtures/repo_map_agent_cases.json`

Variants：

```text
A. no_repo_map
B. static_repo_map
C. query_aware_repo_map
D. incremental_query_aware_repo_map
```

走生产路径 `ExecutionRunner → Agent → Tool lifecycle → Completion Guard → Independent Acceptance → Trace v2`。

采集 solved、hidden verifier、first target read step、files read、search/find calls、input/output/total/cached tokens、latency 和 Repo Map phase telemetry。

当前冻结状态：

```text
execution_status = not_executed
real_model_executed = false
rows = 0
reason = provider_credentials_not_available_in_ci
```

因此 P2 当前不能给出 Coding Agent success-rate 对比。

---

## 7. 已完成测试覆盖

关键测试：

- `tests/test_repo_map_improvements.py`
- `tests/test_repo_map_ablation.py`
- `tests/test_repo_map_persistent.py`
- `tests/test_repo_map_product_behavior.py`
- `tests/test_repo_map_prompt_layout.py`
- `tests/test_repo_map_agent_ablation.py`
- `tests/test_repo_map_persistent_benchmark.py`

覆盖 initial full build、warm restart、Query-only rerank、single/multi-file change、add/modify/delete/rename、staged/unstaged/untracked、HEAD、dirty content、non-Git fallback、corrupt/schema mismatch、symbol cleanup、reference graph、duplicate symbol definitions、index outside repo、Chat cross-round query change、prompt layout、A/B/C/D harness protocol 和 phase benchmark contract。

最新正式 CI 成功轮次包含：

```text
801 passed, 1 skipped
```

随后新增的 duplicate-symbol regression 也在最终 strict run 中通过。

---

## 8. 当前简历 / 面试证据边界

可以说：

> Query-aware Repo Map 在 12-case frozen commit-history benchmark 上将 MRR 从 0.097 提升到 0.319、Token Budget 内 target recall 从 0.365 提升到 0.635；随后将 Repo Map 拆为持久化 SQLite 结构索引与 Query-aware 任务视图，Query 改变只 rerank、代码修改按 changed file 增量更新，并用 12-case strict equivalence regression 保证排序/渲染语义不变。

技术追问时可补充：

> 当前 CI snapshot 中 warm load、单文件和双文件增量更新中位约 0.297s、0.170s、0.177s，explicit full rebuild 约 0.805s；首次 persistent cold build 约 0.961s，比 legacy 0.610s 更慢，因此优化目标是复用和增量更新，而不是 cold-start 加速。

不可以说：

- Repo Map 让 Coding Agent 成功率提升 X%；
- 首次 persistent indexing 更快；
- 整个 Agent 快 71×；
- 只扫描任务相关模块；
- Prompt Cache 命中率提升 X%；
- A/B/C/D real-model ablation 已跑完。

---

## 9. 剩余 TODO

### P2-RM1 / P2-RM5：真实模型实验

- [x] 冻结 4-case fixture；
- [x] A/B/C/D variant；
- [x] production-path harness；
- [x] hidden verifier；
- [x] exploration / token / latency telemetry；
- [ ] 使用同一 model/provider/config 执行真实模型；
- [ ] 最好每个 cell 多次重复；
- [ ] 冻结 report 后再决定是否新增 success/token/latency 简历结论。

### P2-RM4：Provider Prompt Cache

- [x] stable tool schema 前置；
- [x] dynamic Repo Map 后置；
- [ ] 使用真实 provider usage 记录 cached input tokens；
- [ ] 对比旧 / 新 layout；
- [ ] 只有真实 usage 显示差异后再写 cache 结论。

### Optional：Cold Build

当前不是 correctness blocker。未来进入超大仓库场景时，再评估 lazy persistence、parser batching、hash/stat 快路径或 prebuilt index。

---

## 10. 本地验收

优先：

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

完整：

```bash
pytest -q
```

重新跑 phase benchmark 时不要覆盖冻结目录：

```bash
python -m evals.repo_map_persistent_benchmark \
  --repo . \
  --output /tmp/forge-repo-map-benchmark \
  --repetitions 5 \
  --strict
```

无 provider credential 时，只验证 Agent harness：

```bash
python -m evals.repo_map_agent_ablation \
  --validate-only \
  --output /tmp/forge-repo-map-agent-ablation
```

有 provider credential 时，再执行真实模型实验：

```bash
python -m evals.repo_map_agent_ablation \
  --output /tmp/forge-repo-map-agent-ablation \
  --repetitions 1
```

---

## 11. 完成定义

P2 代码主链的 Done 条件已经满足：persistent index 可跨进程 reuse、changed-file incremental update 可用、Query-only rerank 不重新 parse、external Git changes 可检测、未知状态有 full rebuild fallback、frozen Query-aware retrieval/ranking/rendering 语义不退化、production Agent 默认可使用 incremental Repo Map、deterministic regression 覆盖主要 correctness contract。

P2 作为“带真实 Agent 效果结论的完整实验项目”尚未完全 Done，原因仅剩：

1. Real-model A/B/C/D 尚未执行；
2. Provider cache-hit 对照尚未执行。

这两个未完成项必须继续保留为明确边界，不能用离线 benchmark 代替。
