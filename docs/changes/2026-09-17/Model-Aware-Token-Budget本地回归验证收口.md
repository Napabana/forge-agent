# Model-Aware Token Budget 本地回归验证收口

日期：2026-09-17

## 1. 验证对象

本记录只补充 `436947b943db35acfafd79b78aff8dcb8be16778`（`context: make token budgeting model-aware`）之后的用户本地回归结果，不修改 Model-aware Token Budget / Context Compaction 的实现语义。

原实现日志 `docs/changes/2026-09-17/Model-Aware-Token-Budget收口.md` 保留其当时事实：ChatGPT 执行环境无法运行仓库级 pytest，只完成了离线纯函数行为校验。该历史记录不回写、不覆盖。

## 2. 用户本地验证

用户已在本地仓库环境依次完成以下验证，并明确反馈“全部通过”：

```bash
python -m pytest -q tests/test_model_aware_token_budget.py

python -m pytest -q \
  tests/test_token_budget_init.py \
  tests/test_token_budget_improvements.py \
  tests/test_compaction.py \
  tests/test_structured_compaction.py \
  tests/test_tool_pruning.py \
  tests/test_prepare_next_turn.py

python -m pytest -q
```

验证覆盖三层：

1. 本轮新增的 Model-aware Token Budget / Semantic Packet 行为；
2. 既有 TokenBudget / Context Compaction 相关回归；
3. 仓库全量 pytest。

用户未提供 passed 数量、执行耗时或完整 pytest stdout，因此本记录只写“全部通过”，不补造具体数字。

## 3. 结论

- `436947b943db35acfafd79b78aff8dcb8be16778` 的 Model-aware Token Budget / Context Compaction 变更已完成用户本地仓库级回归验证；
- 当前没有来自上述三层测试的已知 regression；
- 本轮仅补验证证据，不修改 runtime、Token Budget 公式、Compaction 策略、测试 fixture 或 benchmark 结果；
- 未运行真实 Provider、付费 API、B2 真实模型实验或新 benchmark，因此本记录不能扩展为线上可靠性或真实模型效果结论。

## 4. 本轮修改范围

仅文档：

- `AGENTS.md`
- `docs/changes/2026-09-17/Model-Aware-Token-Budget本地回归验证收口.md`

明确未修改：

- `config/default.yaml`
- `context/`
- `llm/`
- `tests/`
- B1/B2 fixture
- `evals/results`
