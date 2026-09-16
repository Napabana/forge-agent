# Session 加固与 Query-aware 评测收口

## 本轮目标

收口批次 F（P1-3 Session 加固）和批次 G 的第一阶段（P1-4 固定编码任务、P1-5
Query-aware Repo Map），并把实施计划、TODO 和协作交接更新到真实状态。

## 行为变化

- Session schema 从 v1 升到 v2，读取时迁移旧数据；保存时通过跨平台文件锁串行化，
  再用 revision 拒绝旧副本覆盖新状态。
- Session 只对写盘副本脱敏，不改变运行中的 History；覆盖 Authorization Bearer、
  常见 key/token/secret/password 和 `sk-` 形式秘密。
- Repo Map 保留原静态重要性，并叠加 query 的路径、符号、关键词和匹配符号使用者分数；
  同仓库不同 query 重新排序，但复用扫描结果。
- `evals/fixtures/tasks.json` 固定六类编码任务；materializer 可重置任务目录，隐藏 verifier
  不进入任务文件。汇总器读取 JSONL，输出 JSON 和 Markdown。

## 主要文件

- `agent/session.py`、`agent/session_store.py`
- `context/repo_map.py`、`agent/core.py`
- `evals/__init__.py`、`evals/harness.py`、`evals/report.py`
- `evals/fixtures/tasks.json`
- `tests/test_session_store.py`、`tests/test_repo_map_improvements.py`、`tests/test_evals.py`
- `tests/test_chat.py`、`tests/test_day7.py`、`pyproject.toml`
- `Forge-Agent-P0-P1-实施计划.md`、`TODO-P0-P1.md`、`AGENTS.md`

## 测试证据

环境：WSL 默认发行版，`source ~/.venvs/forge-agent/bin/activate`，Python 3.11.0rc1。

定向命令：

```bash
pytest tests/test_session_store.py tests/test_repo_map_improvements.py tests/test_evals.py -q
```

首次结果：`20 passed, 1 failed in 5.26s`。失败仅在 fixture 测试：第一次 verifier 导入后
生成的 bytecode 可能在同一秒改写源码时仍被命中。`verify_case()` 子进程设置
`PYTHONDONTWRITEBYTECODE=1` 后只重跑失败节点：

```bash
pytest tests/test_evals.py::test_fixed_cases_are_resettable_and_use_hidden_verifier -q
```

结果：`1 passed in 0.95s`。按用户要求没有重复运行整批或全量测试。

## 已知边界

- 当前六个 fixture 只覆盖基础编码类别；长历史、超长输出、Hook/timeout/cancel、进程
  恢复、并发 worktree、bare remote/fake GitHub 尚未加入。
- 汇总器具备指标字段，但尚未运行真实模型/Agent 消融；没有 pass@1、Recall@K、MRR、
  Token 或耗时收益可对外声称。
- Repo Map 尚未用 Git HEAD、工作区变更集合和文件指纹定义完整缓存身份，也未实现
  增量索引、失败降级或 Focused Map。
- 没有实现 MCP、多 Agent、multi-tool call、树形 Session，也没有新增依赖。

## Git 与保护项

- 当前分支/远程：`dev...origin/dev`，HEAD `1b65f84`。
- remote：`git@Napabana:Napabana/forge-agent.git`。
- `stash@{0}` 继续保留；`config/default.yaml` 未修改。
- 本地 Markdown 被 `.gitignore` 忽略，未强制加入 Git。
