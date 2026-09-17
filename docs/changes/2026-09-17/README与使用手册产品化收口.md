# README 与使用手册产品化收口

日期：2026-09-17

## 1. 本轮目标

在 P0/P1、Persistent Repo Map 与 Model-aware Token Budget 已收口的当前 `dev` 基线上，重写项目级 README 与使用手册，使文档能够直接支持后续人工验收：

- `agent run`；
- `agent chat`；
- GitHub Issue → Agent → Acceptance → PR；
- Git Worktree isolate；
- Docker sandbox；
- Worktree + Docker 组合路径；
- EventLog / Trace v2 / Session / Worktree 产物检查。

本轮只更新文档，不修改 runtime、配置、fixture、benchmark result 或测试代码。

## 2. README 改动

`README.md` 从功能清单式说明改为基于生产调用链的整体架构文档，按以下六层展开：

1. Product Entrypoints：CLI / Chat / API / GitHub Issue；
2. Execution Control：`ExecutionRunner → Agent.run`；
3. Context Plane：Model-aware Token Budget、Persistent Query-aware Repo Map、History / Compaction；
4. Tool Safety Plane：validation → pre-hook → permission → tool → post-hook；
5. Isolation / State：SQLite WAL TaskEngine、WorktreeSession、DockerRuntime；
6. Audit / Delivery：Trace v2、Independent Acceptance、GitHub deterministic delivery。

同时将每层设计直接回链到当前实现文件与关键类，明确 direct/isolate、Worktree/Docker、canonical history/model view/Session/EventLog 之间的职责边界。

## 3. 使用手册改动

`USAGE.md` 改为可执行的端到端验收手册，给出建议测试仓库、命令、内部调用链、预期产物与检查方式。

推荐验收顺序固定为：

```text
安装/配置
 → direct run
 → chat 多轮 + resume
 → isolate keep-if-changed
 → isolate discard
 → direct sandbox
 → isolate + sandbox
 → GitHub Issue --no-pr
 → GitHub Issue + verifier + PR
 → Trace / Session / Worktree / PR 核对
```

新增/补齐：

- Session `/session`、`/new`、`/resume`、`/rename` 与 `--continue/--resume/--no-session`；
- `logs/chat/<repo_key>/<session_id>/state.json` 与 per-round EventLog 的职责区分；
- Model-aware Token Budget 新字段及 legacy alias；
- Persistent Repo Map 与 Chat Context Compaction 的当前生产接线；
- Worktree `keep-if-changed` / `discard` 的真实成果语义；
- Docker preflight、默认资源限制、network none、readonly root 与挂载边界；
- GitHub Issue 自动 PR 的 clean baseline、hidden verifier、deterministic delivery 与 delivery status；
- Trace v2 / token usage / Repo Map / Session 并发恢复检查方法；
- 一份完整手工验收 checklist。

## 4. 修正的旧文档误导

### 4.1 Docker 与 Worktree

旧文档容易把 `--sandbox` 描述成“宿主机环境完全隔离”。当前真实实现中：

- direct `--sandbox` 会把目标 repo bind mount 到容器 `/workspace`，容器中的文件/Git 修改会反映到宿主目标 repo；
- `--isolate` 才负责创建独立 Git checkout/index/branch；
- `--isolate --sandbox` 才是“独立 Worktree + 容器进程/网络/资源边界”的组合路径。

新文档已按源码修正。

### 4.2 `--confirm`

产品路径默认经过 PermissionManager。没有 confirm callback 时，CONFIRM 决策不是“自动放行”，而是拒绝。新手册已删除旧版“run 不加 --confirm 会直接执行需要确认操作”的错误描述。

### 4.3 GitHub Issue 自动 PR

当前自动 PR 强制要求独立 `--verify-command`，并要求启动前 local repo clean。自动 PR registry 还会移除 Agent 的 `git_add` / `git_commit`，只有 Acceptance 通过后才由 deterministic delivery 执行 add / commit / push / PR。

旧文档缺少这层门禁，新手册已补齐。

### 4.4 Context Budget

旧 `budget_tokens=80000` 不再描述成模型 Context Window。README/USAGE 已按当前实现拆分：

- model context window；
- model max output；
- request max output；
- Forge context budget cap；
- safety margin；
- semantic packet token cap。

未知代理只使用 Forge cap fallback，不伪造模型能力。

## 5. 证据边界

文档保留 `docs/evidence/README.md` 的既有约束：

- benchmark 数字只在对应 frozen protocol 下成立；
- B2 为 9 个 real-model small-sample runs；
- GitHub delivery 正式真实案例仍只有 1 个 merged PR；
- Worktree / Docker / Failure Harness / Trace 的 implementation/regression 不能外推为总体 Coding Agent success rate 或线上 SLA；
- Docker 不描述为“完全安全”；
- cooperative cancellation 不描述为任意同步调用的强制终止。

## 6. 修改文件

- `README.md`
- `USAGE.md`
- `docs/changes/2026-09-17/README与使用手册产品化收口.md`

未修改：

- `config/default.yaml`
- `agent/`
- `context/`
- `llm/`
- `runtime/`
- `tools/`
- `tests/`
- B1/B2 fixture
- `evals/results`

## 7. 验证说明

本轮是纯文档变更，没有声称重新运行 pytest、真实 Provider、Docker、GitHub PR 或 benchmark。文档内容通过逐项对照当前 `dev` 的以下生产实现进行核对：

- `agent/core.py`
- `agent/runner.py`
- `agent/orchestrate.py`
- `entry/cli.py`
- `entry/chat.py`
- `entry/api.py`
- `entry/github_issue.py`
- `harness/executor.py`
- `harness/permission.py`
- `context/token_budget.py`
- `context/compaction.py`
- `llm/router.py`
- `runtime/worktree.py`
- `tools/runtime.py`
- `task/engine.py`
- `agent/session_store.py`
- `docs/evidence/README.md`

后续应按照新版 `USAGE.md` 在用户本地逐项跑通真实产品流程，再把任何暴露出的实现缺口单独记录和修复。