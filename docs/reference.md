# 参考资料与证据边界

本页只提供稳定入口和结论边界。历史正文与冻结结果仍保留在原目录，不复制进网站正文，也不在左侧
导航和全文搜索中平铺。

## 当前事实

- [系统架构总览](design/Forge-Agent-当前代码架构.md)
- [功能 / 类 / 函数双向索引](design/源码索引.md)
- [常用源码 GitHub 链接](source-navigation.md)
- [架构知识库持续维护协议](design/MAINTENANCE.md)

## 测试与实验依据

- `docs/evidence/README.md`：本地保存的各阶段证据入口和可说/不可说边界，不进入公开仓库。
- `evals/results/benchmark_v1_r2_summary/benchmark_v1_summary.md`：冻结 Benchmark V1 R2 聚合结果。
- `tests/`：确定性单元、集成与回归测试；测试通过不等于真实 Provider 总体可靠性。

## 历史变更

`docs/changes/` 记录当时的施工过程与决策，`docs/todo/` 和 `docs/plans/` 记录计划；这些本地内容
不进入公开仓库，也不自动代表当前实现。需要追溯时，应回到当前源码与 `docs/design/` 交叉核查。

## 项目入口

- [README](https://github.com/Napabana/forge-agent/blob/dev/README.md)：能力概览与安装入口。
- [USAGE](https://github.com/Napabana/forge-agent/blob/dev/USAGE.md)：运行配置和命令。
- `AGENTS.md`：本机协作规则、Git 保护与最新交接；仅本地保存。
