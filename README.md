# Forge Agent

Forge Agent 是一个支持模型切换、工具调用、独立验收与中断续接的命令行编码 Agent。使用 CLI 完成仓库任务、保留多轮 Chat 或交付 GitHub Issue，也可以用评测入口运行编码任务与可靠性 Benchmark。

## 快速开始

```bash
python -m pip install -e .
forgeAgent init
forgeAgent doctor
forgeAgent run --repo . --task "梳理模块入口并给出文件依据"
forgeAgent chat --repo .
```

开发测试使用可选依赖 `.[dev]`，系统凭据后端使用 `.[keyring]`。

## 核心能力

- Run 和 Chat 在终端中展示工具进度和结果，支持流式回答、Markdown、Diff 与纯文本重定向。[终端界面](docs/design/terminal-ui.md)
- 模型与工具共享执行链，支持仓库符号导航、上下文预算和可追溯压缩。
- Git worktree 与 Docker 提供隔离执行，独立验收为任务结果提供可检查依据。
- 运行时可保存检查点、恢复工具回执、导入归档包，并从历史 Chat 轮次分支继续。
- Skills、MCP、Worker 和项目记忆通过同一执行机制集成。
- 本地故障注入和编码评测保存独立结果、用量和 Trace。

[使用手册](USAGE.md) · [设计文档](docs/README.md) · [源码索引](docs/design/source-index.md)

## 运行边界

环境准备和外部连接取决于所选模型、仓库和执行方式。工具权限、续接条件及评测证据口径见对应设计页末尾。
