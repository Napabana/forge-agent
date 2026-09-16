# Repo Map 引用计数热点修复与实施计划

日期：2026-09-14

## 本轮目标

在不启动 P0-1、不中途重构 History/TokenBudget/Agent 主循环的前提下，修复 Repo Map 首次构建的引用计数热点，并把后续 Global Map、Focused Map、增量索引与评测工作写入实施计划。

## 实际修改

- `context/repo_map.py`：保留现有词边界、定义文件跳过自身引用和 `reference_count` 契约；将每文件对每个符号执行全文正则，改成每个文件一次 identifier 扫描，再按命中的 owner 名称回写引用数。
- `tests/test_repo_map_improvements.py`：增加精确标识符边界回归（`Engine` 不匹配 `EngineFactory` 或 `not_Engine`）。
- `Forge-Agent-P0-P1-实施计划.md`：增加分阶段 Repo Map 路线，先修本地扫描热点，再做 query-aware 排序、Focused Map、一致性/增量索引，最后接预算与 Prompt Cache 评测；明确暂不引入向量/图数据库等大组件。
- `AGENTS.md`：更新交接状态和本轮证据。

## 验证

```text
tests/test_repo_map_improvements.py  -> 8 passed in 0.80s
RepoMap(force_refresh=True)           -> 约 3.028s（只读基准）
```

测试使用备用 Windows Python 3.12 和项目 site-packages，未调用真实模型；用户已确认 WSL smoke 通过。未运行全量测试。

## 已知边界

本次只优化引用计数算法，不改变评分公式、map 输出格式或缓存失效策略。3.028s 是当前机器和仓库规模下的相对基准，不能外推为所有仓库的绝对耗时。

