# 长时运行与恢复

## 为什么实现

进程中断后复用已提交的动作和结果，并为无法确认的外部副作用保留核查路径。

## 怎么使用

在用户配置设置 `durable_runtime.enabled: true`。用 `runtime status`、`runs`、`inspect RUN_ID` 查看状态。用 `runtime pause RUN_ID` 请求暂停单次 CLI 任务，用 `runtime resume RUN_ID` 或 `run --resume-id RUN_ID` 续接。Chat 使用 `/resume`。

## 怎样实现

### 状态与接管

Durable Runtime Store 是按项目划分的 SQLite WAL 数据库。Scope Owner 用 OS 锁、主机、启动标识、进程创建身份和 Epoch 隔离并发，Revision 让持久写入按序推进。Schema 升级前保留数据库及所引用的 Blob 备份。`runtime verify` 检查完整性，`runtime backup PATH` 输出备份，`runtime owners` 与 `runtime takeover` 处理显式接管。

### 检查点与操作日志

`DurableRun` 保存 Run 请求、模型动作、History、Planning、Recovery、Skills、Usage 和仓库签名。Tool Journal 提交 Intent、Started 和 Result。Unknown Outcome 会在接管后等待人工确认。`runtime recover list --run-id RUN_ID` 查看指定运行的未决操作。用 `inspect` 核对证据、`resolve` 记录决定、`retry` 执行受控重试、`delivery` 查看远端交付回执。

### 归档与请求计量

`ContextArchive` 保存原文、摘要、裁剪和来源。`ArchiveRead` 按 Scope 分页读取。`runtime archive-export` 与 `archive-import` 在同一项目内复制归档根及来源闭包，保持目标会话现有视图。`runtime import-chat` 导入旧 Chat 历史，`/rewind ROUND` 创建新会话分支。Prefix Cache Audit 记录最终请求指纹、SDK 可观察的物理尝试和供应商回传用量。`runtime cache` 查询，`--prune-expired` 清理过期记录。

### 遇到未决操作时

| 已看到的状态 | 下一步 |
| --- | --- |
| 已提交模型动作 | 续接时使用该动作 |
| 已提交工具结果 | 校验摘要后返回已保存的结果 |
| 已开始执行，效果未知 | `recover inspect` 核对本地证据。可显式用 `--online` 核对 GitHub 或用 `--mcp-query` 查询已配置回执 |
| 查明操作已生效或未生效 | `recover resolve OPERATION_ID --decision resolve_applied\|resolve_not_applied --reason TEXT --evidence TEXT` |
| 已核实未生效的文件写入或 Git push | 用 `recover retry OPERATION_ID --reason TEXT` 记录一次新的受控尝试 |

恢复时重新绑定原任务、Scope 和工作区。路径验收沿用已保存的初始文件基线与工作区指纹。独立验收已提交时复用验收结果。

### 请求与使用量

审计记录请求协议、指纹、变更组件和可观察的尝试序号，生成、Worker 与压缩调用分别归因。报告区分供应商所报缓存命中、本地估算与缺失回执。

## 预期效果

对已完成的模型动作和工具结果提高复用机会，对未确认的副作用提供可审计的恢复证据。

## 使用边界

续接要求仓库、配置、工具注册表与保存状态相符。无法核对的工具效果留待核查。真实供应商用量、外部 MCP、用户 Token 直连和实机跨启动或跨主机接管按各自环境验收。

Windows 下可将 `durable_runtime.root` 指向 NTFS 卷上的私有目录，使运行时数据库和归档能应用用户级 ACL。exFAT 卷无法提供这一访问控制。
