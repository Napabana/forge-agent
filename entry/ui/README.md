# 终端界面

本模块把 Run 与 Chat 的事件、回答流和结果转换成适合终端读取的输出。可以用 `forgeAgent run` 或 `forgeAgent chat` 直接体验。在用户配置的 `ui.mode` 选择 `auto`、`rich` 或 `plain`。

- [`terminal.py`](terminal.py) 管理 Rich 刷新、流式回答和交互提示。
- [`presenter.py`](presenter.py) 从 Trace 事件提取终端展示字段。
- [`summary.py`](summary.py) 汇总结果、独立验收、用量、工具和工作树。

交互执行可以在终端回看工具进展与任务结果。输出重定向时可生成便于保存和检索的纯文本。[终端界面设计](../../docs/design/terminal-ui.md)
