# Forge Agent 协作入口

新对话先读 [当前项目状态](docs/PROJECT_STATE.md)，再核对实际分支、源码与相关架构文档。旧对话不会自动成为新对话的上下文；本文件和项目状态页是可同步的交接入口。本机若有 `AGENTS.local.md`、`docs/changes/CURRENT.md`，也要阅读；本地文件只补充环境与未公开实验状态。

## 开发闭环

1. 开始前检查 `git status --short --branch`、最近提交、远端和 stash；保留用户已有修改、实验结果与配置，不擅自 reset、clean 或覆盖。
2. 先设计：明确目标、现状、受影响源码及调用方、方案、风险与验证方法。跨模块改动把设计记录在对应文档；小修复也要在变更记录说明理由。
3. 再实现：沿用现有结构，修改所需的源码、调用方和测试；运行与风险相称的验证，不把未运行写成通过。
4. 结果归档：记录测试命令与真实输出。Harbor 实验应保存 task/manifest、代码和配置版本、模型、预算、Trace 终态、独立 Verifier、token 与成本口径。失败、超时、未评分和自然完成分别记录。
5. 文档同步：架构事实变化时更新 `docs/design/`、源码索引与图；按 [维护协议](docs/design/MAINTENANCE.md)运行影响分析和一致性检查。每轮更新项目状态页中的已验证结论、未完成项与下一步；不把历史日志的“下一步”自动当作当前任务。

## 证据与发布边界

- Forge 的当前方向是 Coding Agent 与 Harness/Evaluation。Harbor 100 指 Repo2RLEnv `pr_runtime` 的公开任务池；实际试验范围和已完成数以 [项目状态](docs/PROJECT_STATE.md)及实验 manifest 为准。
- 独立 Verifier reward、Agent 自然 FINISH、Provider 错误、Harbor 超时、N/A 与未执行不能合并成一个成功率。
- 不提交真实密钥、用户配置、未经脱敏的 Trace、原始实验产物或本地交接历史；公开文档只写可核验的结论及证据限制。
