# 隔离、验收与 GitHub 交付

## 为什么实现

隔离执行为变更提供独立工作目录，验收判断变更是否满足要求，交付把已验证成果发布到远端。

## 怎么使用

使用 `forgeAgent run --repo . --isolate --task '描述任务'` 在 Git worktree 运行。需要容器环境时加上 `--sandbox`。发布场景用 `python -m entry.github_issue --help` 查阅 Issue、工作区与交付参数。

## 怎样实现

`WorktreeSession` 创建、附着、检查和收尾工作树。容器 Runtime 管进程与挂载。独立 `AcceptanceContract` 检查路径与 verifier 的结果，保留 Agent 终态和工作区产物。GitHub Issue 入口由确定性代码在验收后执行 stage、commit、push 和 PR，并保存各阶段回执。

## 预期效果

用户可以在主 checkout 外检查成果，并在审阅时追溯通过了哪些验收和交付步骤。

## 使用边界

Git worktree 隔离文件视图，Docker 提供进程与网络边界。需要可重建身份的验收合同才能续接，工具注册表变化时会触发恢复校验。
