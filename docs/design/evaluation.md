# 评测与经验迭代

## 为什么实现

评测把模型行为与独立评分放入可复现实验。经验模块把通过验证的轨迹加工成候选知识。

## 怎么使用

运行 `python -m evals.long_horizon --output evals/results/runtime.json` 生成本地故障注入报告。用 `python -m evals.coding_agent --help` 查看任务清单、重复运行和模型开关。Harbor 参考 `examples/harbor/README.md` 的示例。

## 怎样实现

`EvaluationHarness` 为任务准备仓库、固定配置、执行生产 Runner、收集 Trace 并运行独立 grader。试验记录 verifier 结果、结束类型、用量与失败来源。`experience/` 从轨迹提取候选并通过评测与 PromotionGate 决定是否可晋升。

仓库导航和压缩支持独立消融。先用对应评测写出 JSON 或 JSONL，再用分析器读取指定的结果路径。命令 `python -m evals.repo_map_trace_analysis --raw PATH` 生成仓库导航 Trace 明细。输出落在本机评测结果目录。

## 预期效果

可比较不同配置在同一任务、预算和验收协议下的表现。确定性故障验证能检查重复副作用、来源闭包及检查点恢复。

## 使用边界

模型自报成功、独立 verifier 得分、基础设施失败、超时和未执行分别统计。真实供应商运行和本地故障注入分别解读。
