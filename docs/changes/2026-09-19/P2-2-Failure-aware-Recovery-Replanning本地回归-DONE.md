# P2-2 Failure-aware Recovery + Replanning 本地回归验证收口

日期：2026-09-19  
实现提交：`cf019d380fc71c505ebd55fa2e10e2b68f4b80d4`  
状态：**DONE**

## 验证事实

P2-2 实现后，用户在本地完成专项、关键兼容、全量 pytest 与 Evidence Pack 校验，并明确确认全部通过。

用户没有提供最终通过轮次的完整 stdout、passed 数量或耗时，因此不补造统计数字。

## 状态收口

```text
P2-2
IMPLEMENTED / LOCAL VALIDATION PENDING
        ↓
DONE
```

## Deterministic evidence

当前可证明 typed `FailureContext / RecoveryDecision / RecoveryPolicy / RecoveryRuntime` 已进入生产 Agent 主循环；`recovery_mode=off|structured` 与 bounded recovery budget 可用；Tool/Test failure、Permission denied、Loop、No Progress、Completion rejection 可进入结构化 recovery；Provider retry、cancel 与 infrastructure fatal contract 保持独立权威语义；P2-1 plan revision 与 RecoveryPolicy 有 runtime enforcement；pending replan state 在 history trimming / compaction 后仍保留；Trace v2 与 P2-0 Eval 能观测 recovery lifecycle。

## Evidence boundary

本地 regression 通过不能支持 Recovery 提升真实 coding success rate、提高 pass@1、降低 token / latency / steps、故障恢复成功率 X% 或生产级 fault tolerance。这些都需要后续显式 real-model experiment 或更大规模独立任务证据。

## 下一步

进入 **P2-3 Agent Skills**。Skill discovery / selection / load 只提供 workflow/context capability；Skill 脚本不得绕过 ToolExecutor 执行，也不能把所有 Skill 一次性塞入 prompt。
