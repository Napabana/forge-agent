# P1-6 Evidence Pack 改动内容

## 本轮目标

P1-6 不扩展 Forge Agent 功能面，只把当前 `dev` 已有的实现、确定性回归、冻结 benchmark、真实模型小样本和真实 GitHub 交付案例整理成可追溯 Evidence Pack。所有数字直接读取既有冻结结果，不重新运行付费模型、不修改 B1/B2 fixture、不覆盖 `evals/results`。

## Evidence Audit 结论

证据分为五层，禁止互相替代：

1. **Implementation Fact**：源码中确实存在的实现。
2. **Deterministic Offline Regression**：固定输入下验证 contract 的离线测试。
3. **Frozen Offline Benchmark**：固定 fixture/protocol 的离线 benchmark。
4. **Real-model Small Sample**：真实模型的小样本实验，只报告观察值。
5. **Real End-to-End Case**：真实外部链路案例，单案例不外推总体成功率。

当前正式证据：

- B1 Context Policy：7 cases × 3 variants = 21 rows，semantic mode=`fixture`；hybrid 7/7，hard-constraint recall=1.0，recent-raw recall=1.0。
- Repo Map：12-case frozen commit-history benchmark；MRR 0.096954→0.318750；budget target recall 0.364914→0.635251；reference-count median 35.1176s→0.4928s（71.26×），只适用于该子步骤与冻结协议。
- B2 v3：`deepseek-v4.1-flash`，3 cases × 3 variants × 1 run=9 real-model runs；非 deterministic、无 repeat/seed，只能称小样本观察。
- GitHub delivery：当前正式可引用真实案例为 1 个，即 `Napabana/pr-test` Issue #4 → merged PR #5；同一案例中的 4 次尝试不是 4 个独立样本。

## 新增文件

- `docs/evidence/README.md`
  - Evidence Index：Claim / Type / Evidence / Reproduce / Result / Limitation。
  - 正式 benchmark / experiment / case 的数字与边界。
  - 核心能力的“面试可说 / 不可说”。
  - Resume Claim → Evidence Mapping。
  - 对总体 success rate、production ready、100% 自动 PR 等主张明确标记 `INSUFFICIENT EVIDENCE`。
- `evals/verify_evidence_pack.py`
  - 只读离线校验入口。
  - 检查核心证据文件存在、冻结 JSON schema/范围可读取、关键数字未漂移、Evidence Index 的机器锚点与 report 一致。
  - 不访问网络，不调用 Provider/GitHub，不运行 benchmark，不写 fixture/result/repo。
- `tests/test_evidence_pack.py`
  - 将 Evidence Pack 一致性校验纳入 pytest。

默认入口：

```bash
python -m evals.verify_evidence_pack
```

## 明确降级 / 删除的主张

以下说法当前没有足够证据，不进入安全简历表述：

- Agent 总体成功率 X%。
- production-grade / production ready 可靠性。
- 自动 PR 成功率 100%。
- B2 已证明 Context Policy 稳定提升成功率。
- Git Worktree 是安全沙箱。
- Docker 提供“完全安全”隔离。
- Repo Map reference counting 的 71.26× 等于 Agent 端到端提速 71.26×。
- Trace v2 可以确定性重放 Agent 执行。

## 验证状态

当前执行环境无法通过容器直接访问 GitHub：尝试 clone `dev` 时 DNS 返回 `Could not resolve host: github.com`。因此无法在本轮容器中重建完整仓库并执行用户要求的既有全量回归；这不是测试失败，也不记录为“通过”。

已实际执行：

- `python -m py_compile evals/verify_evidence_pack.py tests/test_evidence_pack.py`：通过（临时实现目录）。
- `python -m evals.verify_evidence_pack`：在按当前冻结 report 字段构造的只读最小镜像中通过。
- `pytest -q tests/test_evidence_pack.py`：补齐真实仓库本来已有的 `evals/__init__.py` package 结构后，`1 passed in 0.05s`。

未执行：

- `tests/test_failure_harness.py` / `tests/test_failure_harness_isolate.py`
- `tests/test_trace_v2.py` / `tests/test_runner.py`
- Context benchmark reader 的仓库内现有测试
- Repo Map benchmark reader 的仓库内现有测试
- 全量 `pytest`

上一阶段交接 HEAD `103c3a44d0d018ae858ff462cb9e7a3b269f24d4` 在本轮审计开始与写入前均未发生漂移，且用户已说明该基线本地测试全通过；但本轮不会把这一事实替代为“本次完整回归已执行”。

## P1-6 状态

实现内容已完成，但由于本轮无法实际执行所要求的完整离线回归，**P1-6 暂不标记 DONE，保持 PARTIAL**。待在可 checkout 当前 `dev` 的环境执行 Evidence Pack 自测、Failure Harness、Trace/Runner、benchmark reader 与全量 pytest 后，再把 `TODO-P0-P1.md`、`Forge-Agent-P0-P1-实施计划.md`、`AGENTS.md` 的 P1-6 状态统一改为 DONE。
