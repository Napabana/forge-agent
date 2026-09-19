# P2-3 Agent Skills 本地回归验证收口

日期：2026-09-19  
实现提交：`ae4a25f77a2bd32c4e4e34bb13903616db34624c`  
状态：**DONE**

## 验证事实

用户在本地完成 P2-3 新增专项、关键兼容、package discovery、not-executed Eval 接线、Evidence Pack 校验与全量 pytest，并明确确认全部通过。

用户没有提供最终通过轮次的完整 stdout、passed 数量或耗时，因此不补造统计数字。

## 状态收口

```text
P2-3
IMPLEMENTED / LOCAL VALIDATION PENDING
        ↓
DONE
```

## Deterministic evidence

当前可证明：

- Skill filesystem discovery 与 frontmatter contract 已实现；
- project-local Skill 可以覆盖 global 同名 Skill；
- malformed Skill 被隔离，不阻断整个 Agent；
- initial context 只暴露 Skill metadata；
- `skill_load` 后才进入完整 instructions；
- references 继续二次按需 disclosure；
- loaded Skill/reference 作为 per-run runtime state 跨 history trimming / compaction 保留；
- Skill subsystem 不自动执行 script，真实执行能力仍必须走正常 ToolExecutor lifecycle；
- Trace v2 可观察 discovery / selection / load / reference / rejection；
- P2-0 `planning_recovery_skills` variant 与 non-blocking skill-selection process grader 已接线；
- `skills*` 已进入 setuptools package discovery。

## Evidence boundary

本地 regression 通过不能支持 Skills 提升真实 coding success rate、提高 pass@1、降低 token / latency / steps、Skill trigger accuracy X% 或真实任务效果百分比。这些都需要显式 real-model A/B 或更大规模独立任务证据。

## 下一步

进入 **P2-4 MCP Client / Tool Adapter**。

P2-4 必须把 MCP 作为外部 capability source 接入现有 Tool abstraction，而不是新增一条绕过 ToolExecutor 的调用路径；MCP tool invocation 必须继续服从 validation / Hook / Permission / Cancel / Trace。
