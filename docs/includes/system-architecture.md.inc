```mermaid
flowchart TB
  subgraph Entry[产品入口]
    CLI[CLI run]
    CHAT[Chat session]
    API[HTTP API]
    GH[GitHub Issue]
  end
  CLI --> RUNNER[ExecutionRunner]
  CHAT --> RUNNER
  API --> RUNNER
  GH --> RUNNER
  RUNNER -->|direct| AGENT[Agent.run 同步 ReAct loop]
  RUNNER -->|isolate| ORCH[orchestrate_run async composition root]
  ORCH --> ENGINE[(TaskEngine SQLite WAL)]
  ORCH --> WT[WorktreeSession]
  ORCH --> RT[LocalRuntime / DockerRuntime]
  ORCH --> AGENT
  AGENT --> PROMPT[Prompt + runtime contexts]
  AGENT --> LLM[LLMBackend]
  AGENT --> CTX[History / TokenBudget / Repo Map]
  AGENT --> PLAN[PlanningRuntime / RecoveryRuntime]
  AGENT --> SKILL[SkillCatalog / SkillRuntime]
  AGENT --> EXEC[ToolExecutor]
  EXEC --> HOOK[Validation / Hooks / Permission / Constraints]
  EXEC --> REG[ToolRegistry]
  REG --> NATIVE[File / Search / Shell / Test / Git]
  REG --> MCP[MCPToolAdapter]
  MCP --> MCPM[MCPClientManager / official SDK]
  NATIVE --> RT
  AGENT --> TRACE[(EventLog / Trace v2 JSONL)]
  CHAT --> SESSION[(Chat Session state.json)]
  CHAT --> COMPACT[TraceableCompaction]
  COMPACT -. model-facing override .-> AGENT
  RUNNER --> ACCEPT[Independent Acceptance]
  GH --> DELIVERY[commit / push / PR delivery]
  ACCEPT --> DELIVERY
  subgraph Offline[评测与运行后演化，不在主循环内]
    EVAL[EvaluationHarness] --> RUNNER
    TRACE --> EVOLVE[Trajectory mining]
    EVAL --> EVOLVE
    EVOLVE --> CAND[CandidateStore / PromotionGate]
    CAND -. 显式 promotion 后 .-> SKILL
  end
```
