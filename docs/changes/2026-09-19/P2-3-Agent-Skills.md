# P2-3 Agent Skills

日期：2026-09-19  
实现基线：P2-2 DONE 后当前 `dev`  
状态：**DONE**

## 目标

P2-3 给 Forge Agent 增加可复用 workflow/context capability，但不新增第二个 Agent、不把 Skill 变成绕过 ToolExecutor 的执行通道。

```text
filesystem Skill
  ↓
metadata catalog
  ↓
skill_load / skill_reference_load
  ↓
per-run SkillRuntime context
  ↓
existing Agent loop
  ↓
normal ToolExecutor for executable actions
```

## 1. Skill filesystem 与 catalog

新增 `skills/catalog.py`。

发现路径：

```text
<repo>/.agents/skills/
~/.forge-agent/skills/   # 可配置
```

最小结构：

```text
skill-name/
├── SKILL.md
├── references/   # optional
└── scripts/      # optional
```

`SKILL.md` 必须以 YAML frontmatter 开头，首版要求 `name` 与 `description`；name 必须和目录名一致。

global Skill 先发现，project Skill 后发现，因此 project 同名 Skill 覆盖 global。单个 malformed Skill 不会让整个 Agent 启动失败，而是形成 discovery issue，并在 Trace 中记录 `skill_rejected`。

Skill Trace 不暴露 global root 的绝对路径；catalog metadata 只包含 name / description / source / resource counts。

## 2. Progressive disclosure

新增 `skills/runtime.py`。

当 `skills_enabled=true` 时，首轮 system runtime context 只暴露：

- name
- description
- source=project/global
- references 数量
- scripts 数量

不会把所有 `SKILL.md` 正文一次性塞入 prompt。

模型需要某个 Skill 时调用内部 control：

```text
skill_load(name)
```

只有被选中的 Skill 才把完整 instructions 放入 SkillRuntime。

reference 继续二次按需：

```text
skill_reference_load(skill, reference)
```

reference 必须存在于该 Skill discovery manifest、必须是 UTF-8 regular file，并受独立字符上限约束；路径 traversal 会被拒绝。

## 3. 为什么 Skill control 不走 ToolExecutor

`skill_load` / `skill_reference_load` 只改变模型 runtime context，不执行外部能力、不修改仓库，也不运行脚本，其角色与 P2-1 的 `plan_create`/`plan_revise` 一样属于 Agent 内部状态控制。

真正执行能力仍保持：

```text
LLM ToolCall
  ↓
ToolExecutor
  ↓
Hook
  ↓
Permission
  ↓
Tool
  ↓
Observation / Trace
```

Skill scripts 只作为 manifest 告诉模型“存在什么资源”；Skill subsystem 没有 script execute API，不会自动运行。模型如果决定执行项目工作区内的脚本，必须显式调用现有 Shell Tool，因此继续受 Permission / Hook / Cancel / Sandbox / Trace 约束。

首版 global Skill script 在 sandbox 中不保证直接可执行，因为 global Skill root 不会被自动 mount 到 execution workspace；这是有意的安全边界，不为“能跑脚本”增加宿主路径旁路。

## 4. Context / Compaction

`SkillRuntime` 是 per-run 当前状态，和 current plan / recovery gate 一样不写入 canonical ConversationHistory。

每轮 `_render_request_parts()` 重新生成：

```text
Planning state
+ Recovery state
+ Skill metadata
+ loaded Skill instructions
+ loaded references
→ system runtime context
```

因此 HistoryWindow / Structured Compaction 可以裁旧对话，但不会遗忘当前已加载 Skill。

Skill context 本身进入 system message，因此已经参与 TokenBudget 的 request pressure 与 history trimming。首版没有另加 `skill_tokens` diagnostic 字段，避免把 provider billing truth 与本地 attribution 混淆；A/B 使用 provider total usage 与 Skill lifecycle metrics 观察 overhead。

## 5. Trace v2

新增：

- `skill_discovered`
- `skill_selected`
- `skill_loaded`
- `skill_reference_loaded`
- `skill_rejected`

统一 `span_type=skill`。

`skill_selected` 表示模型尝试选择，`skill_loaded` 表示 runtime 接受并加载；invalid / over-limit / invalid reference 使用 `skill_rejected`，便于区分 trigger 与 successful disclosure。

## 6. Config / 产品入口

```yaml
agent:
  skills_enabled: false
  skills_global_dir: "~/.forge-agent/skills"
  skills_max_loaded: 3
  skills_max_chars: 12000
  skills_reference_max_chars: 8000
```

默认关闭，保证既有 baseline 无 Skill schema/context overhead。

CLI / Chat / API / GitHub Issue 使用同一配置。

`pyproject.toml` 的 setuptools package discovery 已加入 `skills*`；否则源码 checkout 下 pytest 能 import，但安装包会漏掉新 package。

## 7. P2-0 Evaluation Harness

正式 architecture variant：

```text
baseline_react
  planning=off
  recovery=off
  skills=off

planning
  planning=always
  recovery=off
  skills=off

planning_recovery
  planning=always
  recovery=structured
  skills=off

planning_recovery_skills
  planning=always
  recovery=structured
  skills=on
```

Skill variant 不读取用户 `~/.forge-agent/skills`，而是固定使用：

```text
evals/fixtures/coding_agent/skills/
├── bug-fix/
├── test-and-verify/
└── repository-navigation/
```

TrialMetrics 新增 discovered / selected / loaded / reference-loaded counts。

共享 suite 增加 required=false 的 `skill_selection` process grader，部分 case 标记 should-trigger / should-not-trigger。这个 grader不会进入 independent outcome verifier，也不会改变 task success/acceptance；只作为 process evidence。

real-model report 如果未来执行，会单独记录 Skill lifecycle means 与 skill-selection process checks/passes；这些 process 数字不能包装成 coding success rate。

## 8. Deterministic regression

新增 `tests/test_agent_skills.py`，覆盖：

- metadata visible / body 未提前加载；
- explicit skill_load 后正文才进入 context；
- should-not-trigger 时正文不进入 context；
- reference 二次按需 disclosure；
- reference 必须先 load Skill；
- traversal / unknown reference 拒绝；
- loaded Skill 在 history override / compaction-like 路径后仍保留；
- project Skill 覆盖 global；
- malformed Skill 被隔离；
- script manifest 不会自动执行；
- max loaded bound；
- Eval architecture mapping。

扩展：

- `tests/test_coding_agent_eval.py`：Skill metrics + non-blocking skill_selection grader；
- `tests/test_day6.py`：Skill config validation；
- frozen suite：non-blocking trigger expectations。

## 明确未做

- 不做 Skill marketplace / remote install；
- 不新增 selector LLM；
- 不自动加载所有 Skill；
- 不自动执行 Skill scripts；
- 不把 global Skill directory mount 到 sandbox；
- 不让 Skill 绕开 ToolExecutor；
- 不实现 MCP；
- 不实现 Skill Evolution；
- 不执行或伪造 real-model Skill A/B。

## 本地验证

先运行 P2-3 新增/直接相关回归：

```bash
python -m pytest -q \
  tests/test_agent_skills.py \
  tests/test_coding_agent_eval.py \
  tests/test_day6.py
```

再运行 Agent/context/entry 关键兼容回归：

```bash
python -m pytest -q \
  tests/test_structured_planning.py \
  tests/test_structured_recovery.py \
  tests/test_trace_v2.py \
  tests/test_compaction.py \
  tests/test_structured_compaction.py \
  tests/test_token_budget_improvements.py \
  tests/test_runner.py \
  tests/test_chat.py \
  tests/test_api.py \
  tests/test_cli_isolate.py \
  tests/test_github_issue_delivery.py \
  tests/test_evidence_pack.py
```

验证 package discovery：

```bash
python -c "from setuptools import find_packages; assert 'skills' in find_packages('.'); print('skills package OK')"
```

只验证 Eval architecture 接线，不调用真实模型：

```bash
python -m evals.coding_agent \
  --variant planning_recovery_skills \
  --output-dir evals/results/local-p2-3-skills-not-executed
```

最后：

```bash
python -m evals.verify_evidence_pack
python -m pytest -q
```

当前 ChatGPT 环境未执行以上命令，因此状态保持 `IMPLEMENTED / LOCAL VALIDATION PENDING`。

## 本地回归补充（最终，2026-09-19）

用户按交接顺序完成 P2-3 新增专项、Agent/context/entry 关键兼容、package discovery、`planning_recovery_skills` not-executed Eval 接线、Evidence Pack 校验和全量 `python -m pytest -q`，并明确确认全部通过。

最终通过轮次没有提供完整 stdout、passed 数量或耗时，因此本文只记录“全部通过”，不推断或补造统计。

据此：

```text
P2-3 Agent Skills
IMPLEMENTED / LOCAL VALIDATION PENDING
                ↓
               DONE
```

本地 regression 能够证明：metadata-only discovery、显式 progressive disclosure、reference 二次按需加载、project-over-global、malformed Skill isolation、loaded Skill 跨 history override/compaction 保留、script 不自动执行、正式产品入口配置、package discovery、Trace lifecycle 与 P2-0 Skill variant/process grader 接线均可 deterministic 验证。

本阶段仍未执行正式 real-model `planning_recovery vs planning_recovery_skills` A/B，因此不能声明 Skills 提高 coding success rate、trigger accuracy、pass@1、降低 steps/tokens/latency，或证明某个 Skill 对真实任务有效。
