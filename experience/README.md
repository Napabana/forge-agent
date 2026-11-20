# 经验候选

从运行轨迹提取候选经验，对候选进行受控评测并决定是否晋升。

## 怎么使用

通过 `python scripts/run_skill_evolution.py --help` 查看可用输入和输出。

## 从哪里读代码

- [`trajectory.py`](trajectory.py)
- [`candidate.py`](candidate.py)
- [`promotion.py`](promotion.py)

## 预期效果

从可复查的轨迹提取带来源的候选经验，衡量候选能否改善任务表现，再按晋升门槛决定是否采用。

## 设计文档

[评测与经验迭代](../docs/design/evaluation.md)
