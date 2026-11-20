# Forge Agent 使用手册

## 配置和启动

```bash
python -m pip install -e .
forgeAgent init
forgeAgent doctor --json
forgeAgent config models
forgeAgent run --repo /path/to/repo --task "实现改动并验证"
forgeAgent chat --repo /path/to/repo
```

`forgeAgent --config PATH` 选择用户配置。无人值守初始化使用 `init --non-interactive --provider PROVIDER --model MODEL --api-key-env ENV_NAME`。详见[配置、模型与用量](docs/design/configuration.md)。

## 执行与验收

```bash
forgeAgent run --repo /path/to/repo --isolate --sandbox --task "修复测试失败"
forgeAgent --multi-agent run --repo /path/to/repo --plan --task "审查实现，按需委派分析"
python -m entry.github_issue --help
```

`--isolate` 使用 Git worktree，`--sandbox` 启用 Docker，默认成果策略 `keep-if-changed` 保留有变更的工作树。GitHub Issue 命令可在独立验收后交付 PR。

## 终端显示与 Chat 输入

```bash
forgeAgent run --ui plain --no-stream --repo . --task "只读分析测试结构" > run.log
forgeAgent run --no-progress --verbose --repo . --task "检查失败测试"
```

`--no-stream` 关闭回答流，执行事件仍可见。默认隐藏模型推理，可用 `--reasoning-stream` 显式开启。交互终端使用内联 Rich，CI 和重定向自动使用纯文本。`NO_COLOR` 关闭颜色。

Chat 中按 Enter 提交，Meta+Enter（或先 Esc 再 Enter）换行，Ctrl+G 在 `VISUAL` 或 `EDITOR` 中编辑当前输入。用 `/status` 看会话与执行环境，`/tokens` 看用量，`/diff` 看最近一轮记录的 Git patch，`/plan`、`/recovery`、`/compact`、`/skills` 和 `/mcp` 查看执行细节。完整配置见 [CLI 终端界面](docs/design/terminal-ui.md)。

## 暂停、续接和归档

在用户 YAML 中设置：

```yaml
durable_runtime:
  enabled: true
```

```bash
forgeAgent runtime --repo . runs
forgeAgent runtime --repo . inspect RUN_ID
forgeAgent runtime --repo . pause RUN_ID
forgeAgent run --repo . --resume-id RUN_ID
forgeAgent runtime --repo . recover list --run-id RUN_ID
forgeAgent runtime --repo . recover inspect OPERATION_ID
forgeAgent runtime --repo . archive-export SOURCE_SCOPE bundle.json --id ARCHIVE_ID
forgeAgent runtime --repo . archive-import TARGET_SCOPE bundle.json
forgeAgent runtime --repo . cache --run-id RUN_ID
```

Chat 的 `/resume` 续接中断轮，`/resume SESSION_ID` 打开会话，`/rewind ROUND` 从已结束轮次建立新会话，`/archive` 查询归档记录。跨 Scope 导入返回目标中的新 ID。用 `runtime archive SCOPE_ID ARCHIVE_ID` 或模型工具 `archive_read` 读取。恢复命令与参数见[长时运行与恢复](docs/design/durable-runtime.md)。

## 扩展和记忆

```bash
forgeAgent workspace trust --repo . --scope project_skills
forgeAgent skill add /path/to/skill
forgeAgent skill list
forgeAgent mcp add docs --url https://example.com/mcp
forgeAgent mcp test docs
forgeAgent memory status --repo .
```

## 本地验证

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m evals.long_horizon --output evals/results/runtime-reliability.json
python scripts/check_docs.py --validate
```

测试输出和 Benchmark 报告写入机器上的本地路径。详细设计见[评测与经验迭代](docs/design/evaluation.md)。

## 使用边界

隔离执行、Provider、MCP 和 GitHub 需要对应环境与授权。恢复要求匹配原仓库、配置、工具及待核实状态。归档包保存正文和完整性摘要，应留在可信路径。Chat 回退保留工作区现状。
