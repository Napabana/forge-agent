# Harbor Qwen3.8-27B Pilot R1 归档

## 目标

在约 ¥5 预算内，只执行一次 `default/tiangolo__typer-1788` 真实模型 trial，使用 `Qwen/Qwen3.8-27B`、35 steps，并与 Qwen3.5-4B v7 做探索性比较。实验结束后把完整报告、轨迹和日志以可公开审计的脱敏附件归档到 `docs/research/`。

用户当前明确不开展 SFT 或 GRPO；本次轨迹只用于 Forge Harness/Evaluation、停止协议、timeout 配置和 Verifier provenance 审计。

## 结果

- 只启动一个付费 job：`typer-1788-qwen38-27b-pilot-r1`；`n_attempts=1`、`max_retries=0`，没有第二次运行。
- Step 13 正确修改 `typer/core.py::TyperOption.value_from_envvar()`；Step 18 focused test 为 `27 passed in 0.49s`。
- Harbor trial 内独立 Verifier：reward `1.0`、resolved `true`、F2P `1/1`、P2P `22/22`、无 regression；Verifier pytest 为 `27 passed, 1 skipped in 5.71s`。
- Agent 没有自然终止。Trace 有 30 次 `llm_call_started`、29 次 `llm_call_finished`、0 次 `llm_call_failed`、0 个 `run_terminated`；第 30 次调用期间被 Harbor 外层 1800 秒限制取消，Harbor 记录 `AgentTimeoutError`。
- 根因是双层 timeout 未对齐：adapter kwarg 为 5400 秒，但 task `[agent].timeout_sec=1800.0`，启动命令没有设置 Harbor `--agent-timeout-multiplier 3`。
- Trace 中 29 个已完成调用合计 input 849,931、output 18,792、reasoning 15,264、total 868,723；按 ¥3/M input、¥12/M output 估算约 ¥2.78。Provider 实际账单字段为空，且未完成的第 30 次请求是否计费未知，因此不把估值写成精确扣费。
- 发生 2 次 TPM 429，均由慢退避恢复，没有最终 Provider failure。
- Agent 创建了 `repro_issue.py`、`test_tmp_fix.py` 两个临时未跟踪文件，随后过度调试自建测试，没有在 timeout 前清理或 FINISH。

## 归档内容

目录：`docs/research/harbor-typer-1788-qwen38-27b-pilot-r1/`

- `报告.md`：完整结论、配置、轨迹、Verifier、tokens/cost、v7 对比和当前决策；
- `trace.sanitized.jsonl`：完整 Trace v2 事件；
- `console.sanitized.txt`：完整 Forge console；
- `harbor-result.sanitized.json`、`exception.sanitized.txt`、`harbor-trial.sanitized.log`、`harbor-config.sanitized.json`、`harbor-lock.sanitized.json`、`harbor-artifacts-manifest.sanitized.json`；
- `agent-install-check.sanitized.txt`；
- `verifier-reward-details.json`、`verifier-reward.txt`、`verifier-test-stdout.txt`、`verifier-test-output.log`；
- `runtime-config.sanitized.yaml`、`launch-command.sanitized.sh`；
- `manifest.json`：发布文件 hash、大小、身份和脱敏边界。

发布前替换本机用户名、宿主绝对路径和环境文件位置；扫描本机 env 文件中的实际 secret 值，并确认归档中零命中。没有上传 API key、wheel、完整 Harbor job、Docker layer 或 Provider 账户信息。原始本地 artifacts 未修改。

## 文档同步

- `docs/README.md` 增加 Pilot R1 入口；
- `docs/research/architecture-audit/README.md` 回填实际结果与当前不训练决策；
- `docs/research/architecture-audit/decisions/审计决策记录.md` 关闭 preflight 问题并记录功能通过/timeout 分层；
- `docs/changes/2026-11-13/Forge-Harness评测与Python-only试验实施规划.md` 回填结果表、归档路径和后续门槛；
- `docs/research/Harbor-Typer-1788实验报告.md` 将旧训练建议更新为当前 Harness/Evaluation-only 决策。

## 边界

本次是“功能结果成功、独立验收通过、rollout 因 Harness timeout 非自然终止”。不能写成完整 Forge SUCCESS，也不能写成模型任务失败。单次结果不能外推总体成功率或稳定 token/latency 优势。

本轮只更新文档和脱敏实验附件，不修改生产 Python、默认配置、Benchmark fixture 或历史结果目录。架构职责和执行链未变化，因此不更新 Mermaid 或源码索引。仓库测试不因纯文档/数据归档重复运行；执行文档一致性、JSON/JSONL 解析、链接、secret/path 扫描与 `git diff --check`。

补充：独立 Windows/WSL worktree 回归发现 `trace.sanitized.jsonl` 因 Git `text=auto` 被 checkout 为 CRLF，导致工作树 SHA-256 与 manifest 中仓库 blob 的 LF SHA-256 不同。`.gitattributes` 已增加 `*.jsonl text eol=lf`；实验内容和 manifest 不变，专项回归重新通过。

## 合并门禁收口

研究分支进入 PR 前补齐了既有 Evidence Pack 测试依赖的四份历史证据文档；它们此前受 `/docs/**` 忽略规则影响，导致干净 worktree 缺失。Zensical 明确排除 `changes/**`，因此两个对应引用改为 GitHub `dev` 链接，不改变站点内容源或生产架构。

WSL 最终验证：`python -m evals.verify_evidence_pack` 通过；`python -m pytest -q` 为 `1204 passed / 30 warnings`，耗时 461.55 秒；`zensical build --strict` 为 `No issues found`。warnings 均为既有 AnyIO/Python 3.11 cancellation deprecation。未调用真实模型 API，未修改生产 Agent Python、默认配置、Benchmark fixture 或历史结果。
