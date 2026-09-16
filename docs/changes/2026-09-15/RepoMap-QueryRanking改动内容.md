# 2026-09-15 Repo Map Query Ranking 改动内容

## 目标

基于真实 commit-history Repo Map 消融结果，修复当前 Query-aware 排序对自然语言任务提升有限的问题；保持无 query 时的 Static Repo Map 行为不变，并保持现有 benchmark case、ground truth 与指标定义不变。

## 修改文件

- `context/repo_map.py`
  - 新增 snake_case / camelCase 拆词与轻量词形归一。
  - Query relevance 同时使用路径、符号和扫描阶段已缓存的源码正文，不增加额外文件读取。
  - 使用文件频率对仓库中普遍出现的通用词降权；路径与符号命中权重高于普通正文命中。
  - 无 query 或空 query 时返回空 relevance，继续使用原 Static importance 排序。
- `tests/test_repo_map_improvements.py`
  - 增加 identifier 拆词、词形归一、正文 relevance 以及空 query Static fallback 的定向测试。
- `tests/test_repo_map_ablation.py`
  - 增加只依赖源码正文相关性的 commit-history 集成 case，验证 Query-aware 排序能够改变目标文件排名。

## 保持不变

- 未修改 `evals/fixtures/repo_map_ablation.json`，避免根据结果调整 benchmark case。
- 未修改 Repo Map 扫描、tree-sitter 解析、引用计数和预算裁剪逻辑。
- 未引入 embedding、向量数据库、LLM reranker 或新的外部依赖。

## 验证状态

当前环境无法直接 clone GitHub 仓库执行 pytest，因此本轮没有宣称新的 Recall/MRR 或端到端收益。需要在本地 `dev` 工作树运行定向测试，并用原 12 个 commit-history case 重新执行 `evals.repo_map_ablation`。只有新报告生成后，才根据真实数据决定是否保留新的 Query-aware 简历 Claim。
