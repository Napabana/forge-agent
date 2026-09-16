# Forge Agent 本地协作与交接

本文件是本机 Codex/Agent 的持续交接说明。每次开始任务先读，结束任务前更新
“最后交接”。这是面试项目：优先保证代码可验证、设计可解释、指标不夸大。

## 不可违反

- 保留用户已有修改；先看 `git status` 和 diff，禁止擅自 reset、clean、checkout 覆盖。
- 修改代码前必须先澄清需求，列出拟修改的代码文件及每个文件的改动理由，等待用户
  确认后再执行；用户可以指定部分文件由自己修改，未获确认不得提前改动。
- 不读取、打印、提交或复制真实 API Key。配置只引用 `${KRILL_API_KEY}`。
- `.env`、本文件和其他本地 Markdown 已被 `.gitignore` 忽略，不要强制加入 Git。
- 私有远程必须使用 SSH 别名：`git@Napabana:Napabana/forge-agent.git`。
  `ssh -T git@Napabana` 已验证成功；不要改成 `git@github.com`，当前 Key 不匹配它。
- 默认在 `dev` 开发；切分支、pull、rebase、push 前先确认工作区和跟踪分支。
- 修复后运行与风险相称的最小测试；不能运行时明确记录原因，不能假称通过。
- 新增或修改代码时补充对应的中文注释；能在一行内清晰写完的代码不要无故拆成多行。
- 每轮完成实际更新后，在仓库根目录新建一份 `YYYY-MM-DD-改动内容.md`；文件名必须
  简要说明本轮改了什么，同一天有多轮不同更新时分别使用不同的“改动内容”，不要
  覆盖旧日志。日志完整记录本轮目标、行为变化、修改文件、测试真实结果和已知边界。
- 每轮最终回复必须提示用户可通过对应更新日志查看完整改动，并给出日志路径。

## 每次开始

```text
1. git status --short --branch
2. git log -3 --oneline --decorate
3. git remote -v
4. git stash list
5. 阅读本文件“当前状态、下一步、最后交接”
```

## 环境

仓库位置：

- Windows：`E:\2806\forgeAgent\forge-agent`
- WSL：`/mnt/e/2806/forgeAgent/forge-agent`
- 可恢复旧文件备份：`E:\2806\forgeAgent\wsl-recovery-20260913`

Windows 使用 Windows venv：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest -q
```

WSL 必须使用独立 Linux venv，不能把 Windows venv 当作 Linux 环境：

本机默认 WSL 发行版固定为 `Ubuntu-22.04-Recovered`；后续 WSL 命令直接使用该发行版，
无需再次向用户确认。仓库 Git 状态与历史检查在 Windows 端执行，代码修改与测试在该
WSL 发行版中执行。

```bash
cd /mnt/e/2806/forgeAgent/forge-agent
source ~/.venvs/forge-agent/bin/activate
python -m pytest -q
```

若 Linux venv 尚未创建，用 `python3 -m venv ~/.venvs/forge-agent`，再安装
`-e ".[dev]"`。WSL 使用 `python3`/`python` 和 `./path`，不要使用 PowerShell 的
`.\path`。

`KRILL_API_KEY` 应保存在 Windows 用户环境变量，或 WSL 的
`~/.config/forge-agent/env`（权限 `600`），绝不写真实值到 YAML。当前
`config/default.yaml` 使用 OpenAI-compatible Krill 地址和
`deepseek-v4-flash-0731`。

## 项目认知

- `agent/core.py`：同步 ReAct 主循环、完成性守卫、Reflection、循环检测。
- `llm/`：Anthropic、OpenAI-compatible Chat Completions、OpenAI Responses。
- `context/`：ConversationHistory、Repo Map、Token Budget。
- `entry/cli.py`、`entry/chat.py`、`entry/api.py`、`entry/github_issue.py`：四个入口。
- `task/engine.py`：SQLite WAL 任务状态与条件认领。
- `runtime/worktree.py`、`agent/orchestrate.py`：隔离 worktree、成果检测和保留策略。
- `tools/`、`harness/`：工具执行、权限与本地/Docker runtime。

面试表述边界：同步的是 `Agent.run` 内核，asyncio 主要用于编排；Worktree 隔离
checkout/index/branch，Docker 隔离进程、网络、资源和根文件系统；保留 worktree
不等于已经 commit、merge、push 或创建 PR；EventLog 是审计记录，不是确定性重放。

## 当前状态（2026-09-16）

- B2 termination 收口已完成：Completion Guard 可恢复拒绝、`INCOMPLETE`、结构化
  termination/resource reason、中性资源 warning、Loop Detector 独立语义均已实现。
- B2 v3 已按冻结参数完成 3×3×1 共 9 个真实 run；报告位于
  `evals/results/context_policy_agent_ablation_v3/`。本轮不自动进入后续 P0/P1。
- 本机默认 WSL 发行版为 `Ubuntu-22.04-Recovered`；Git 检查使用 Windows，代码修改与
  测试使用该 WSL，不再重复确认发行版。

- 分支：`dev`，跟踪 `origin/dev`。
- HEAD：`ba0720d 完善B2`；本地 `dev` 与 `origin/dev` 一致。
- 私有远程：`git@Napabana:Napabana/forge-agent.git`。
- P0-1、P1-1、P1-2、P1-3 和 P1-5 核心已完成；P0-2、P0-3、P1-4、P1-6 为
  `PARTIAL`；C6 与完整 Resource Manager 等扩展明确延期。当前唯一状态清单为
  `TODO-P0-P1.md`，执行顺序见 `Forge-Agent-P0-P1-实施计划.md`。
- 文档导航见 `docs/README.md`；日期日志位于 `docs/changes/YYYY-MM-DD/`，当前架构文档位于
  `docs/design/`，历史计划位于 `docs/plans/archive/`。历史日志中的“下一步”不自动恢复为待办。
- 当前 working tree 含本轮 Markdown 归档和状态对齐，以及此前真实 eval fixture 仓库状态；
  后者必须原样保留，不得作为文档清理的一部分处理。
  `config/default.yaml` 当前不在 `git status` 中，但仍按用户配置文件保护，未经确认
  不要修改、提交或还原。
- 本地 stash 中有 `windows bootstrap before switching to dev`，包含 README 启动说明、
  `.env.example` 和 `scripts/start.cmd`；未确认前不要 drop，恢复时按文件选择。
- 旧 WSL 仓库的 Git 元数据及部分文件损坏；可读候选文件已备份到上述 recovery 目录。

已经完成并进入 `dev`：

- OpenAI Responses 协议与基础设施错误判断修复。
- isolate 的 `discard` / `keep-if-changed`、base commit 改动检测和 WorktreeArtifact。
- Chat 持久化：版本化 `state.json`、原子 checkpoint、`--continue`、`--resume`、
  `--no-session`、pending round 中断识别、仓库隔离和测试。

## 已知问题与下一步

按当前证据和工程价值排序：

1. P0-3：统一四入口 Trace schema/version、prompt 分区 token 统计与 schema 级脱敏。
2. P0-2：补齐四入口 hook/cancel 行为矩阵，只修复可复现的不一致。
3. P1-4：用 deterministic fake provider 补 provider、permission、hook、cancel 失败注入。
4. P1-5 小尾项：仅在测试能稳定复现时处理 Git/working-tree cache identity，以及 shell/git
   写入、删除和重命名导致的 Repo Map 陈旧问题。
5. P1-6：整理默认离线、真实模型显式 opt-in 的证据入口，并持续标注样本和外推限制。

不自动开始完整 Resource Manager、hidden-verifier feedback、C6、MCP 或多 Agent。

不要为了简历堆功能。每个新增主张必须能指向实现、测试或可复现实验；测试覆盖率
不等于 Agent 真实任务成功率。

## 每次结束：更新最后交接

必须在本文件末尾覆盖更新以下内容：

- 本轮目标和结论。
- 实际修改的文件。
- 测试命令与真实结果。
- 未提交修改、stash、分支和 remote 状态。
- 明确的下一步或阻塞原因。

### 最后交接（2026-09-16）

- 本轮只做 TODO/实施计划状态对齐和 Markdown 日志整理；未修改生产代码、测试代码、
  `config/default.yaml`、remote 或 stash，未运行付费模型，未 commit/push/rebase。
- 基线为 `dev@ba0720d3ca909afd0146510bd5bad7cf8131d2d6`，与 `origin/dev` 一致；Git 检查均在
  Windows 端执行，文档操作使用 `Ubuntu-22.04-Recovered`。该发行版已固定为默认，不再询问。
- `TODO-P0-P1.md` 和 `Forge-Agent-P0-P1-实施计划.md` 已改为当前版；旧版本保存在
  `docs/plans/archive/`。P0-1、P1-1、P1-2、P1-3、P1-5 核心标记为 `DONE`，并将其边界、
  可选证据和延期项分开记录。
- 根目录日期日志已按日期迁入 `docs/changes/`；架构文档迁入 `docs/design/`；新增
  `docs/README.md` 作为 source-of-truth、设计、历史日志和评测证据导航。
- 本轮未运行 Python 测试，因为没有代码变更；只执行 Markdown 引用、目录与 Git diff 检查。
- 完整记录见 `docs/changes/2026-09-16/文档与TODO状态整理改动内容.md`。下一批建议从
  P0-3 Trace schema/脱敏收口开始，但本轮不实施。

### 历史交接（2026-09-15）

- G、P0-1、F、H 的既有未提交成果全部保留；没有覆盖用户修改。
- 本轮以 `Napabana/pr-test` Issue #4 完成首个真实自动 PR 案例。前三次运行分别暴露测试
  工具 cwd 错误、provider 空响应、Agent 提前 commit 后 Completion Guard 误判，工作区和
  Trace 均保留；第四次为 Agent success、Acceptance passed、Delivery delivered。
- `entry/github_issue.py` 现在把 shell/test/git 与文件工具绑定到目标仓库，并从自动 PR 的
  Agent registry 移除 `git_add`/`git_commit`，确保独立验收后才由交付层统一提交。
- `tests/test_github_issue_delivery.py::test_issue_registry_uses_target_repo` 同时固定目标 cwd 与
  提交权契约，WSL 最终结果 **1 passed in 2.25s**；此前 4 个交付测试与 3 个 GitHub 逻辑
  回归结果继续有效，未重复运行已通过节点，也未跑全量。
- 新增仓库外隐藏验收器 `evals/pr_test_issue_4_verifier.py`，先跑目标完整 pytest，再检查
  clamp 边界、异常文本、既有函数和中文 docstring；成功 Trace 中目标测试为 **15 passed**。
- 真实成功运行：run `297cb818_20260915_040600`，8 steps、47,454 tokens、40.3s；远端
  commit `0c5b108`，PR `https://github.com/Napabana/pr-test/pull/5`。用户已合并；核验得到
  Merge Commit 与 `main` HEAD 均为 `f5ad77c`，Issue #4 已关闭。
- 新增代码继续配套中文注释，并保持能清晰单行表达的代码不无故拆行。
- Git 基线仍为 `dev...origin/dev`、HEAD `1b65f84`；当前 tracked 修改新增
  `entry/github_issue.py`；新增未跟踪 `tests/test_github_issue_delivery.py`、`evals/run.py`、
  `evals/pr_test_issue_4_verifier.py`，此前所有修改均保留。remote、`stash@{0}` 和
  `config/default.yaml` 未修改，GitHub Token 未写入仓库 remote 或日志。
- 用户确认后已完成 Token-safe clone：`entry/github_issue.py` 使用临时 Git
  `http.extraHeader` 认证 clone、push 和远端 SHA 查询，URL/remote/异常不含 Token；Git
  子进程移除明文 `GITHUB_TOKEN` 和 HTTP trace 环境，未增加依赖。
- `tests/test_github_issue_delivery.py` 新增 clone 成功/失败安全契约。WSL 首轮新增节点
  **2 passed in 1.34s**，受影响 push/retry 回归 **2 passed in 1.50s**；移除子进程明文
  Token 后最终受影响节点 **1 passed in 1.12s**，无失败节点，未跑全量。
- 使用当前 WSL 凭据对私有 `Napabana/pr-test` 做最终只读 clone 冒烟，成功且 origin 为
  `https://github.com/Napabana/pr-test.git`，`TOKEN_IN_REMOTE=False`；临时目录已自动清理。
- 第二优先级 `--no-pr` 兼容契约已完成：参数化现有 registry 测试，自动 PR 模式无
  `git_add`/`git_commit`，`--no-pr` 模式保留两者；WSL **2 passed in 2.34s**。生产行为
  已符合契约，因此本轮没有修改生产代码。
- 本轮记录见 `docs/changes/2026-09-15/no-pr兼容契约改动内容.md`。下一优先级先统计现有 JSONL 中
  provider 空响应和 `finish` 误调用的发生次数与任务影响，再决定是否需要恢复代码。
- 失败样本首轮统计已完成：现有 22 个 JSONL 中，provider 空响应命中 1 个 Run 并失败；
  `finish` 误调用命中 3 个 Run，其中 2 个恢复完成，另 1 个最终失败源于此前 test cwd。
  任务异质，不能表述为模型失败率；本轮未修改恢复代码。
- 统计记录见 `docs/changes/2026-09-15/失败样本统计改动内容.md`。下一优先级为 Repo Map 与 Context
  Compaction 真实消融，开始前先冻结 Commit、任务集、模式、重复次数和原始输出目录。

* 本轮新增 `docs/design/Forge-Agent-当前代码架构.md`，覆盖当前非测试源码的分层、调用链、逐文件职责
  注释与状态边界；按用户要求不分析 `tests/`，也未运行测试。重大设计或跨层改动必须先
  提供问题证据、拟修改文件及理由、最小替代方案、回滚和验证计划，等待用户确认后再改代码。
* 本轮记录见 `docs/changes/2026-09-15/当前代码架构文档改动内容.md`。用户要求后续新开一个任务继续执行；
  当前代码、`config/default.yaml`、stash、分支与 remote 均保持原状。
* 本轮按用户要求更新项目级 `.codex/config.toml`：关闭 documents、pdf、spreadsheets、
  presentations、template-creator 与 asu-skills 六个 Plugin，保留 ponytail；原有
  `sandbox_workspace_write.network_access = true` 未改动。
* 本轮仅修改 Codex 项目配置和本地交接文档，未修改业务代码、未运行代码测试；配置语法已
  通过 Python `tomllib` 解析检查。记录见 `docs/changes/2026-09-15/项目级Plugin精简改动内容.md`。
