# Forge Agent Real-model Benchmark V1

日期：2026-09-22  
基线 HEAD：3962241d67566c752418eb0f24778e51b5fd6636  
Benchmark ID：forge-agent-real-model-benchmark-v1  
Suite：evals/fixtures/coding_agent/benchmark_v1.json  
Canonical suite SHA-256：ad69fc2fd905b501db9ae5f4c6d37363d4b67f6b1cdecace90ddc64173f4c7ec

## 1. 目标与归因边界

本轮只评估同一模型、同一 Provider、同一任务、同一 Repo Map、同一 native Tool registry、同一 Context / execution budget 下，两种 Agent architecture 的差异：

~~~text
baseline_react
  planning=off
  recovery=off
  skills=false
  MCP=false

vs

planning_recovery_skills
  planning=always
  recovery=structured
  skills=true
  MCP=false
~~~

主实验不引入 MCP，不修改生产 Agent prompt，不增加 Full P2 的 step/token budget，不使用 LLM-as-judge。

Primary metric 固定为 Independent Acceptance Pass Rate：

~~~text
Run / Acceptance contract success
+
all required deterministic outcome graders pass
~~~

报告只写 observed successes / observed success rate，不把 24 次 trial/variant 外推成稳定总体 pass@1。

## 2. Stage 1 Audit

基于 dev@3962241d67566c752418eb0f24778e51b5fd6636：

- evals/coding_agent/__main__.py 已有 architecture variant 映射。baseline_react 对应 planning/recovery/skills 全关；planning_recovery_skills 对应 always/structured/true；两者 MCP 均关闭。
- evals/coding_agent/runner.py 已有 isolated trial repo、reference solution self-check、独立 AcceptanceContract、trial artifact、output no-overwrite 和 default not-executed 路径。
- evals/coding_agent/graders.py 已支持 command / file / repository_state / run_trace / skill_selection / recovery_motif，并可提取 steps、tokens、test attempts、completion rejection、plan revision、recovery、Skill 等指标。
- evals/coding_agent/report.py 适合单 variant small-sample aggregate，但不负责本次跨 variant frozen benchmark。
- evals/fixtures/coding_agent/suite.json 是既有 P2-0 8-task suite，本轮不覆盖。
- agent/planning.py、agent/recovery.py、skills/runtime.py 已提供本轮需要的能力。

因此本轮只修改 Eval / fixture / report / regression / docs，不修改生产 Agent 行为。

## 3. Frozen 12-task suite

| Task ID | 类型 | Required outcome | Recovery | Skill |
| --- | --- | --- | --- | --- |
| email-normalization-bug | 单文件 bug | email trim + case-insensitive | 否 | should-trigger bug-fix |
| clamp-feature | 单文件 feature/control | clamp 三段行为 | 否 | should-not-trigger |
| canonical-retry-config | repository navigation | canonical retry=4，无 caller hardcode | 否 | should-trigger navigation |
| multifile-display-name | 多文件 feature | normalize helper + caller wiring | 否 | neutral |
| slug-regression-recovery | test failure/recovery | slug whitespace behavior + retest | 是 | should-trigger bug/test |
| completion-guard-increment | completion guard | increment + required tests | 否 | should-trigger test |
| runtime-config-navigation | navigation/regression | runtime setting override reflected | 是 | should-trigger |
| generated-copy-change-approach | source-of-truth navigation | runtime source=v2，generated unchanged | 否 | should-trigger navigation |
| tag-parser-recovery | test failure/second repair | lowercase + skip empty + dedupe | 是 | should-trigger bug/test |
| registry-text-encoder | 多文件 feature/replan candidate | encoder + registry + regression test | 否 | should-trigger test/navigation |
| warning-color-feature | 单文件 feature/control | exact mapping | 否 | should-not-trigger |
| id-contract-migration | 多文件 contract migration/replan candidate | string ID + numeric caller preserved | 否 | should-trigger test/navigation |

约束：

- task prompt 只描述用户可见目标，不包含 reference implementation；
- functional outcome 由 deterministic grader 与独立 Acceptance 判断；
- skill_selection 等 process grader 默认不决定主要功能成功；
- recovery / replan 是否真实发生由 Trace 统计，不通过改 Agent 强制制造；
- should-not-trigger task 只用于测量 false-trigger；
- 一旦开始真实运行，suite 语义内容和 canonical SHA-256 一起冻结，不能根据结果修改 fixture。Canonical hash 对 JSON key 顺序、缩进和 CRLF/LF 不敏感，避免 Windows/WSL checkout 差异被误判成 suite 漂移。

## 4. Frozen protocol

~~~text
12 tasks
x 2 repetitions
x 2 variants
= 48 real-model Agent trials
~~~

Suite defaults：

~~~yaml
max_steps: 20
budget_tokens: 40000
repo_map_mode: incremental
MCP: false
~~~

当前 config/default.yaml / config/schema.py 解析出的模型配置：

~~~yaml
provider: openai
protocol: auto
model: deepseek-v4.1-flash
~~~

CLI 会把 provider / protocol / model、canonical suite SHA-256、max_steps、budget_tokens、Repo Map mode、planning/recovery/skills/MCP 状态写入 metadata.json 的 run_metadata。最终汇总器会拒绝两组 artifact 的公平性字段发生漂移。

## 5. Metrics

Primary：

- observed successes；
- observed success rate；
- Full P2 - Baseline absolute delta（percentage points）。

Secondary：

- successful-trial mean/median steps；
- successful-trial mean/median total tokens；
- successful-trial mean/median wall time；
- all-trial metrics 仅供审计；
- recovery-tagged task success rate；
- Skill should-trigger selected / loaded rate；
- Skill should-not-trigger false-trigger rate；
- completion rejection / plan revision / recovery selected / test attempts；
- per-task comparison；
- paired Full-P2 wins / losses / ties。

失败 trial 可能提前结束，因此效率比较以 successful-only 为主。

## 6. Offline validation contract

真实 Provider 调用前固定验证：

1. suite schema；
2. 12/12 reference solution deterministic self-check；
3. coverage tags；
4. 两个 architecture variant mapping；
5. repetitions=2，24 planned trials/variant；
6. dry-run not_executed artifact；
7. metadata suite SHA / model / budgets；
8. aggregator synthetic regression；
9. fairness drift rejection；
10. output no-overwrite；
11. 0 Provider calls。

当前冻结前离线自检结果：

- 12/12 reference solutions 通过 deterministic outcome graders；
- Benchmark aggregator synthetic A/B regression 通过；
- canonical suite hash 复核为 `ad69fc2fd905b501db9ae5f4c6d37363d4b67f6b1cdecace90ddc64173f4c7ec`；
- 当前 ChatGPT 执行容器无法解析 github.com，因此没有在容器中运行完整仓库 pytest；不能把下面的 pytest 记为已通过，需由用户本地执行。

本地 regression：

~~~bash
python -m pytest -q \
  tests/test_coding_agent_eval.py \
  tests/test_coding_agent_benchmark_v1.py
~~~

## 7. 0-API freeze commands

Baseline：

~~~bash
python -m evals.coding_agent \
  --suite evals/fixtures/coding_agent/benchmark_v1.json \
  --variant baseline_react \
  --repetitions 2 \
  --output-dir evals/results/benchmark_v1_baseline_dry
~~~

Full P2：

~~~bash
python -m evals.coding_agent \
  --suite evals/fixtures/coding_agent/benchmark_v1.json \
  --variant planning_recovery_skills \
  --repetitions 2 \
  --output-dir evals/results/benchmark_v1_full_p2_dry
~~~

不加 --real-model 时，只做 reference validation 并生成 not_executed metadata，不创建 Provider backend。

## 8. Real-model commands

仅在 dry-run artifact 验证后由用户人工执行。

Baseline：

~~~bash
python -m evals.coding_agent \
  --suite evals/fixtures/coding_agent/benchmark_v1.json \
  --variant baseline_react \
  --repetitions 2 \
  --output-dir evals/results/benchmark_v1_baseline_real \
  --real-model
~~~

Full P2：

~~~bash
python -m evals.coding_agent \
  --suite evals/fixtures/coding_agent/benchmark_v1.json \
  --variant planning_recovery_skills \
  --repetitions 2 \
  --output-dir evals/results/benchmark_v1_full_p2_real \
  --real-model
~~~

发生 timeout / connection error 时，先保留并分析已有 trial artifact，不自动重跑整套 benchmark。

## 9. Offline final aggregation

~~~bash
python scripts/report_coding_agent_benchmark_v1.py \
  --baseline evals/results/benchmark_v1_baseline_real \
  --full-p2 evals/results/benchmark_v1_full_p2_real \
  --output-dir evals/results/benchmark_v1_summary
~~~

输出：

~~~text
benchmark_v1_summary.json
benchmark_v1_summary.md
~~~

汇总器固定检查：12 tasks、2 repetitions、24 trials/variant、suite SHA、Provider/model、max_steps、budget、Repo Map、MCP 关闭，以及 architecture mapping。

## 10. Evidence boundary

真实 48 trial 完成前不能写任何成功率提升数字。

真实结果完成后也只能表述为：在该冻结 12-task Benchmark V1、每种 architecture 24 次真实模型 trial 中观察到的结果。

不能表述为稳定总体 pass@1、SWE-bench 水平、模型训练收益、MCP 收益或对其他任务分布的泛化保证。

如果之后根据 Benchmark V1 bad case 修改 Agent，该 case 可以进入 future failure regression set，但修改后的重跑不能冒充同一份未经见过的 Benchmark V1 泛化证据。
