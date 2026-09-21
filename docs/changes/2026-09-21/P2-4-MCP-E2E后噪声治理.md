# MCP E2E 后噪声治理

日期：2026-09-21

状态：**VERIFIED / REAL-MODEL R3 PASS**

## 背景

P2-4 MCP 的第二轮 real-model E2E 已成功完成，但轨迹中存在明显非 MCP 噪声：

- Windows/WSL checkout 造成无关 CRLF/LF Git diff；
- 只改一行却多次 `file_write`，模型反复检查 trailing newline；
- `python -c` 等表达能力强的命令被简单前缀白名单视为 read-only；
- 纯只读 pipeline 又因为出现 `|` 被 Planning 一律视为 may-mutate；
- 模型有专用 Tool 时仍频繁使用 shell / byte inspection；
- MCP 新 evidence 与完全重复 evidence 在 NO_PROGRESS 语义上没有区分。

本轮按上述顺序逐项修复。

## 1. pr-test 固定 LF

目标分支：

```text
Napabana/pr-test:forge-p2-mcp-demo
```

新增：

```text
.gitattributes
* text=auto eol=lf
```

提交：

```text
0de0c7cc8d947999900e65c2c25b30c9ebfa3af1
```

正式 benchmark 前仍需在本地 clone 执行 clean reset，使新的 attributes 真正作用于工作树：

```bash
git config core.autocrlf false
git reset --hard
git clean -fdx
git status --short
```

## 2. 新增 format-preserving file_edit

新增 `tools.file_tool.FileEditTool`。

接口：

```json
{
  "path": "src/app/config.py",
  "old_text": "POLICY_MODE = \"legacy\"",
  "new_text": "POLICY_MODE = \"strict\""
}
```

约束：

- `old_text` 不得为空；
- 必须恰好出现一次；
- 缺失或多次出现返回 `INVALID_ARGUMENTS`；
- workspace escape 继续拒绝；
- 只替换命中的 UTF-8 byte sequence；
- 未修改 bytes 原样保留。

因此局部 edit 不会因为 Python text serialization 改写整文件的 line endings / EOF newline。

提交：

```text
164513ee523a236b5ec63d897a7d5fb2585cb0a7
```

## 3. Shell classifier fail-safe

旧的 simple prefix whitelist 包含：

```text
python -c
python3 -c
find
awk
sed -n
```

这些命令都有足够表达能力产生副作用，不能仅凭 prefix 判定为 read-only。

修复后：

- 从 simple read-only prefix 集中移除；
- `xxd / od / strings` 作为明确 byte inspection 工具保留 read-only；
- Planning 与 PermissionManager 统一调用 `_is_repository_readonly`；
- 无法证明只读的 shell command 保守按 mutation-capable / CONFIRM。

提交：

```text
f658b1e35bd974461bf16c60f3d83f521c6bd3b4
```

## 4. 安全只读 pipeline

此前任何 `|` 都让 Planning fail-safe 为 mutation-capable。

现在支持一个有限子集：

```text
cat file | tail -1
git diff | cat
wc -c file | tail -1
```

仅当每个 stage 都独立属于明确只读集合时才 READ_ONLY。

以下继续 MAY_MUTATE：

```text
cat file | tee copy
python -c "..." | cat
cat file > copy
find . -delete
```

并继续拒绝/保守处理 command substitution、background、`||`、重定向等复杂 shell form。

提交：

```text
d1edd08401285ee981a5cf0b623b680cbb284411
```

## 5. Prompt 降噪

System Prompt 增加：

- 有等价专用 Tool 时优先专用 Tool；
- localized existing-file change 优先 `file_edit`；
- 新文件/整文件重写再用 `file_write`；
- 没有 test / repository policy / diff 证据时，不检查 CRLF/LF、EOF newline；
- minimal diff 已存在且 post-edit verification 通过后，不继续冗余 inspection。

提交：

```text
7ecbdc3b8cc7e3c6e8288c07460babbcc79998bd
```

## 6. MCP semantic progress

成功的 MCP observation 现在按：

```text
SHA256(tool_name + status + output + error)
```

加入 bounded `SemanticProgressTracker`：

- 第一次新的 MCP evidence → semantic progress；
- 相同 tool + 相同 output 的重复结果 → 不重复推进；
- 新的不同 MCP evidence → 再次推进；
- failed MCP call 不当作成功 evidence。

提交：

```text
c1d3ed24b388e90b034be51f61baf66928ce4326
```

## 7. 测试补充

已补 deterministic tests，覆盖：

- `file_edit` 保留 CRLF 与 EOF newline；
- missing/non-unique old_text 拒绝；
- workspace escape；
- registry 注入 file_edit workspace；
- `python -c` / `find -delete` / `awk system` fail-safe；
- byte inspection command read-only；
- safe read-only pipeline；
- pipeline 含 `tee` / `python -c` / redirection 时 may-mutate；
- Prompt targeted-tool 规则；
- unique MCP evidence 与 duplicate MCP evidence 的 NO_PROGRESS 语义。

建议本地先跑：

```bash
python -m pytest -q \
  tests/test_day3.py \
  tests/test_cli_isolate.py \
  tests/test_harness.py \
  tests/test_structured_planning.py \
  tests/test_structured_recovery.py \
  tests/test_repo_map_prompt_layout.py \
  tests/test_mcp_integration.py
```

随后：

```bash
python -m pytest -q
```

用户已在本地完成全量 pytest，并确认全部通过。此前测试过程中暴露的 Shell fake-fixture、find action 分类和正则转义回归均已修复；最终 real-model 复测基于 `dev@36d1150ea4958de6bd64e44c5c4713e0e9aa8ab6`。

## 8. 后续真实验收

本地测试通过后，应 reset/reclone clean `pr-test:forge-p2-mcp-demo`，重跑相同 MCP real-model task。

期望观察：

- 首次 MCP guidance 后直接进入 targeted edit；
- `file_edit` 取代多次 `file_write`；
- 不再为了 CRLF/trailing newline 做 xxd/od/python rewrite；
- safe read-only pipeline 不产生无意义 plan rejection；
- duplicate MCP output 不重复刷新 semantic progress；
- 最终 post-edit tests + FINISH。

只有新的真实 run 之后，才能讨论 step/token/time 是否下降；本轮不做性能提升 claim。


## 9. Real-model r3 验证结果

使用与上一轮成功 run 相同的：

```text
model: deepseek-v4.1-flash
config: config/eval-p2-mcp-pr-test.yaml
target: clean pr-test:forge-p2-mcp-demo
task: 同一 MCP policy-mode coding task
```

r3：

```text
Status  : SUCCESS
Steps   : 11
Tokens  : 73,197
Time    : 75.1s
Tests   : 16 passed
```

真实轨迹：

```text
Step 1-2  repository exploration
Step 3    mcp__eval_docs__lookup_project_guidance
          -> POLICY_MODE='strict'
          -> canonical file src/app/config.py
Step 4    plan_create
Step 5-6  plan_step_update
Step 7    file_edit legacy -> strict
Step 8    full tests/ -> 16 passed
Step 9    final status/diff verification
Step 10   plan_step_update
Step 11   FINISH -> SUCCESS
```

上一轮成功 run：

```text
29 steps
295,016 tokens
779.2s
SUCCESS
```

同 fixture 单次对比：

```text
steps:  29 -> 11      (-18, -62.07%)
tokens: 295,016 -> 73,197
        (-221,819, -75.19%)
time:   779.2s -> 75.1s
        (-704.1s, -90.36%)
```

### 可观察到的行为变化

本轮不是只看最终数字，还能在 trajectory 中直接看到对应治理项生效：

- `file_edit` 被真实模型直接采用；
- 没有重复 `file_write`；
- 没有 `xxd / od / trailing newline / CRLF` 调试链；
- 没有 permission denial；
- 没有 NO_PROGRESS recovery / replan；
- MCP guidance 只调用一次；
- 修改后立即执行 full tests；
- full tests 后只做一次最终 diff/status 检查并 FINISH。

因此可以说：

> 这轮噪声治理在同一真实 MCP fixture 的单次复测中显著收敛了轨迹，并且改进点与具体 tool trajectory 可对应。

不能说：

> 已证明 Forge 的 MCP token/latency 稳定提升 75%/90%。

原因：

- 只有一轮 before 与一轮 after；
- 模型本身存在随机性；
- 上一轮包含一次 provider timeout，本轮没有；
- 尚未做多 repetition benchmark / distribution comparison。

### 剩余噪声

r3 仍在 Step 1-2 和 Step 9 使用 shell 做 repo exploration / status / diff，而不是完全使用专用工具。这个问题已经通过 Prompt 做 soft guidance，但尚未成为强制 tool policy。

当前不继续扩大本轮 scope。后续若进入正式 benchmark，可以把：

```text
dedicated-tool adherence
shell fallback count
format-inspection count
duplicate edit count
```

作为 trajectory quality metrics 记录。
