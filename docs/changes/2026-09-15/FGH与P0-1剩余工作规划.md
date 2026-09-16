# F/G/H 与 P0-1 剩余工作规划

## 本轮目标

把代码审计得出的真实缺口写入实施计划，并固定后续执行顺序；本轮不修改生产代码。

## 更新内容

- 在 `Forge-Agent-P0-P1-实施计划.md` 新增第 12 节，区分 P0-1、F、G、H 的已完成
  能力和剩余契约。
- 明确当前 Query-aware 尚未覆盖普通源码内容关键词，六个 fixture 尚未被真实 Agent
  批量执行，不能声称 Recall/MRR、pass@1 或性能收益。
- 后续拆为七步：G 同一 Run 一致性、G EvalRunner、P0-1 收口、F 冲突体验、H 独立
  验收、H 交付闭环、H 真实证据包。
- 每一步列出预计修改文件，并保持 MCP、多 Agent、multi-tool call、树形 Session、
  向量检索后置。

## Git 与测试

- 规划前状态：`dev...origin/dev`，HEAD `1b65f84`，tracked working tree clean。
- 本轮只有被 `.gitignore` 忽略的 Markdown 更新，没有运行测试。
- `stash@{0}`、SSH remote 和 `config/default.yaml` 均未修改。

## 下一步确认点

第一批拟修改：

- `agent/core.py`：成功的文件写工具执行后失效当前 Repo Map 摘要，使下一 step 全量刷新。
- `tests/test_repo_map_improvements.py`：增加同一 Run“写文件 -> 下一轮看到新文件”的最小测试。

用户确认后只执行这一批和对应定向测试，再汇报结果后进入第二批。
