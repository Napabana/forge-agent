# 开发与评测脚本

提供文档校验、任务与评测辅助入口。

## 怎么使用

先运行 `python scripts/check_docs.py --validate`，各评测脚本支持 `--help`。

## 从哪里读代码

- [`check_docs.py`](check_docs.py)
- [`run_skill_evolution.py`](run_skill_evolution.py)

## 预期效果

文档校验、评测准备和报告生成可以用同一输入重复运行。生成内容写到命令指定的本地位置。

## 设计文档

[评测与经验迭代](../docs/design/evaluation.md)
