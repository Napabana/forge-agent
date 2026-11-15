# Harness P0/P1 加固与离线验证

分支：`fix/harness-audit-remediation-20261115`；基线：`35c94c3d96dd75a241913cf38ac243c208b6357d`。

## 实际改动

- P0 Security：LocalRuntime、command grader、GitHub 独立 verifier 使用最小环境；MCP 默认相同，显式 server.env 仍为可信扩展授权。
- P0 Evaluation：Trial schema v2，固定评分 policy；分开 Agent/Verifier/failure domain/scoreability/usage completeness；阶段异常保留 Trial，cleanup 不覆盖结果；v1 历史口径单列。
- P1 Completion：研究算法迁入 agent/completion，旧 eval 模块兼容导出；宿主冻结测试来源与内容快照，生成有限收尾反馈，保持全部 FINISH 与独立验收门禁。
- P1 Permission：解析 symlink 防路径越界；显式 unattended profile 仅在可信入口声明受限环境时可用，固定诊断可执行、危险或未知命令继续拒绝。
- P1 Token/Repo Map：生产地图用同一 TokenCounter 限制最终渲染；旧无 counter 调用保持字符近似；增加可选 Trial 逻辑调用/累计 token 限额，预算耗尽返回 INCOMPLETE。
- P1 Config parity：CLI/API/Chat/GitHub/Eval 共用配置构造；保留预算携带的模型信息；Eval 持有独立 context policy，并记录工具、权限、解释器与预算 metadata。

## 已获得的验证证据

全部 Python 执行位于 Ubuntu-22.04-Recovered 的既有 forge-agent venv；原生 Linux 验证副本由 E 盘 WSL VHD 承载。Windows 原 worktree 的用户 config/default.yaml 修改与 stash 均保留。

- Phase0 原型/归档：53 passed；Completion/Eval/Runner/Harbor：54 passed。
- 固定 HEAD 原生 WSL 全量基线：1190 passed、14 skipped、31 warnings，378.31 秒。
- 凭据/Runtime/MCP：92 passed、14 skipped；假凭据/权限/验收：79 passed；最终新增安全合同：16 passed。
- Completion/Planning/Recovery/Failure：143 passed；更新后 Completion/Guard：56 passed；新增收尾案例：12 passed。
- Context/配置：80 passed；补充入口及 policy 隔离：34 passed。
- Evidence Pack：passed。
- 文档一致性：PASS；严格 Zensical 构建：No issues found，6.54 秒。
- 修改后原生 WSL 完整 pytest：1247 passed、14 skipped、31 warnings，382.09 秒；相对基线新增 57 项测试，0 失败。
- `git diff --check` 通过；新增代码 Secret 模式扫描唯一匹配为测试路径 task-id 的误报，无新增真实凭据。

## 保留的限制

环境白名单阻断直接继承，不是 OS 机密隔离；LocalRuntime/Harbor 同 UID 代码仍可能访问宿主/同容器进程及凭据文件。故未自动为 LocalRuntime/Harbor 开启无人值守放宽，通用恶意代码执行仍需独立 UID/代理/进程隔离。Docker daemon 不可用，动态 Docker 14 项跳过；不能声称 Docker 安全验收通过。

Completion 的测试来源是宿主冻结的既有内容，不证明测试覆盖充分；未知测试命令保持保守。逻辑调用限额不包括每次 HTTP retry；累计 token 限额在同步请求返回后的边界检查，可能有单请求超调。缺失/估算/retry usage 标记不完整，账单未知不能当零。严格 map cap 是本地 Counter 口径，不是 provider 账单保证。

未调用真实 Provider、未运行付费 Harbor、未改变默认 Provider/Key 配置、未改历史审计或冻结 benchmark。没有真实成功率/成本改善声明。

## 后续固定 A/B 条件

固定模型参数、任务 SHA、依赖、工具 manifest、权限 profile、context/map/累计预算、timeout、评分 policy 指纹与重复次数；每次只改一个模块，同时报告自然结束率、独立功能评分、不可评分故障比例及 usage 完整性。
