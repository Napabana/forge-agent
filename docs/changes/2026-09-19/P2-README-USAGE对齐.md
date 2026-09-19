# README / USAGE 对齐 P2 Agent Intelligence

日期：2026-09-19  
状态：**DOCUMENTATION UPDATE**

## 目标

P2-0 ～ P2-5 已全部 DONE，但根 `README.md` 与 `USAGE.md` 仍主要描述 P0/P1。本轮把当前 `dev` 的真实实现同步到对外架构说明与使用手册。

## README

新增 Agent Intelligence 章节，覆盖 P2-1 Structured Planning、P2-2 Failure-aware Recovery、P2-3 Agent Skills、P2-4 MCP capability integration、P2-0 Coding Agent Evaluation Harness 与 P2-5 Trajectory-driven Skill Evolution。

同步更新关键目录、四入口功能矩阵、推荐配置示例与当前明确未实现/不得夸大的能力边界，并修正旧 README 中“MCP 正式产品接入未实现”的过时描述。

## USAGE

新增 P2 使用章节，记录 Planning/Recovery/Skills/MCP 配置、Skill 文件格式、MCP transport 与 trust 边界、Evaluation CLI 的 not-executed/real-model 区分、五个 architecture variant，以及 P2-5 offline API / promotion / rollback 主链。

同时更新推荐验收顺序、P2 手工检查项与专项回归命令。

## 边界

- 没有新增 runtime 功能、CLI flag 或配置字段；
- 没有把 P2-5 伪装成现有 `agent evolve` CLI；
- 没有修改 frozen fixture 或历史 `evals/results`；
- 没有新增 real-model 指标；
- 文档继续区分 deterministic regression 与 real-model effectiveness。
