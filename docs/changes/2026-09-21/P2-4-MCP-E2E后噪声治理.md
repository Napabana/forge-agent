# MCP E2E 后噪声治理

日期：2026-09-21

状态：**IMPLEMENTED / LOCAL VALIDATION PENDING**

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

当前 ChatGPT 执行环境无法解析 github.com，因此没有在此处实际运行 pytest；状态保持 **LOCAL VALIDATION PENDING**。

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
