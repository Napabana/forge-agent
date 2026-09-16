# P1-6 Evidence Pack 收口为 DONE

## 本轮目标

在 P1-6 Evidence Pack 实现已经落地的基础上，根据用户本地最终验证结果完成状态收口，不新增 Forge Agent 功能。

## 验证结论

用户已确认以下 P1-6 closure 要求全部通过：

```bash
python -m evals.verify_evidence_pack

pytest -q \
  tests/test_evidence_pack.py \
  tests/test_failure_harness.py \
  tests/test_failure_harness_isolate.py \
  tests/test_trace_v2.py \
  tests/test_runner.py \
  tests/test_context_policy_benchmark.py \
  tests/test_repo_map_ablation.py

pytest -q
```

用户未提供具体 passed 数量或耗时，因此本日志只记录“全部通过”，不补写未经证实的测试数字。

## 状态更新

以下三个状态/交接文件统一将 P1-6 改为 `DONE`：

- `TODO-P0-P1.md`
- `Forge-Agent-P0-P1-实施计划.md`
- `AGENTS.md`

同时删除或更新其中已经过时的“P1-6 PARTIAL”“远程未验证所以不能收口”等描述，明确：

- P0-1～P0-3 均为 DONE；
- P1-1～P1-6 均为 DONE；
- P0/P1 主线整体收口；
- 延期项仍保持延期，不因阶段收口自动升级为待开发功能。

## Evidence Pack 最终形态

- Evidence Index：`docs/evidence/README.md`
- 默认离线只读校验：`python -m evals.verify_evidence_pack`
- pytest 接线：`tests/test_evidence_pack.py`
- Evidence 分类：Implementation Fact / Deterministic Offline Regression / Frozen Offline Benchmark / Real-model Small Sample / Real End-to-End Case
- Resume Claim → Evidence Mapping 已形成；总体 success rate、production-ready、100% 自动 PR、B2 稳定提升等无充分证据主张继续保持 `INSUFFICIENT EVIDENCE`。

## 当前正式证据边界

- Context Policy B1：7 cases × 3 variants = 21 frozen rows；hybrid 7/7，只代表 fixture benchmark。
- Repo Map：12-case commit-history benchmark；MRR 0.097→0.319，预算内 target recall 0.365→0.635；reference-count 71.26× 仅代表该冻结子步骤。
- B2 v3：`deepseek-v4.1-flash`，3 cases × 3 variants × 1 run = 9 real-model runs；非 deterministic，无 repeat/seed。
- GitHub delivery：当前正式可引用真实 E2E 为 1 个 Issue→merged PR 案例；不能外推总体自动 PR 成功率。

## 修改边界

本轮只更新状态/交接文档和本 changelog：

- 不修改 Agent runtime；
- 不修改 `config/default.yaml`；
- 不修改 B1/B2 fixture；
- 不覆盖 `evals/results`；
- 不新增 MCP、Multi-Agent、parallel tools、Resource Manager 或新的 Context 策略。

## 阶段结论

P1-6 可以标记 `DONE`，Forge Agent 当前 P0/P1 阶段整体收口。后续默认工作重心转向简历、面试叙事和基于真实证据的项目深挖；只有真实使用或新 benchmark 暴露明确缺口时，再 reopen 对应工程条目。
