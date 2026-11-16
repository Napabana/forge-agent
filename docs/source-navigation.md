# 源码导航

完整的功能→源码→测试、源码→功能→测试关系见[源码双向索引](design/源码索引.md)。本页只提供常用
入口的可点击链接；路径文字本身在离线环境中仍可复制。

固定核查基线：`fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb`。`dev` 链接会随分支更新，
`baseline` 链接固定到阶段一核查版本。

| 模块 | 主要符号 | 源码 |
|---|---|---|
| 执行入口 | `ExecutionRunner.run` | `agent/runner.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/agent/runner.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/agent/runner.py) |
| isolate 编排 | `orchestrate_run` | `agent/orchestrate.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/agent/orchestrate.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/agent/orchestrate.py) |
| ReAct 主循环 | `Agent.run` | `agent/core.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/agent/core.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/agent/core.py) |
| Tool 生命周期 | `ToolExecutor.execute` | `harness/executor.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/harness/executor.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/harness/executor.py) |
| 权限与只读 | `PermissionManager.check` | `harness/permission.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/harness/permission.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/harness/permission.py) |
| Repo Map | `RepoMap.build` | `context/repo_map.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/context/repo_map.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/context/repo_map.py) |
| 持久化索引 | `PersistentRepoIndex` | `context/repo_index.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/context/repo_index.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/context/repo_index.py) |
| Planning | `PlanningRuntime` | `agent/planning.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/agent/planning.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/agent/planning.py) |
| Recovery | `RecoveryPolicy` | `agent/recovery.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/agent/recovery.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/agent/recovery.py) |
| Skills | `SkillCatalog.discover` | `skills/catalog.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/skills/catalog.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/skills/catalog.py) |
| MCP | `MCPClientManager` | `mcp_integration/manager.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/mcp_integration/manager.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/mcp_integration/manager.py) |
| Worktree | `WorktreeSession` | `runtime/worktree.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/runtime/worktree.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/runtime/worktree.py) |
| Evaluation | `EvaluationHarness` | `evals/coding_agent/runner.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/evals/coding_agent/runner.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/evals/coding_agent/runner.py) |
| Promotion | `PromotionGate` | `experience/promotion.py` · [dev](https://github.com/Napabana/forge-agent/blob/dev/experience/promotion.py) · [baseline](https://github.com/Napabana/forge-agent/blob/fd700bdf48ace5f03bb9feab49d72f7c12e5ddcb/experience/promotion.py) |

GitHub 外链不是技术解释的唯一载体；离线时请用本页路径、专题“源码映射”和双向索引定位代码。
