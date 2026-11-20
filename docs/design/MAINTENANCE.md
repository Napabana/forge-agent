# 文档维护

以源码和可重现测试为依据维护设计说明。改动职责、入口或状态流时，更新对应设计、模块 README、源码索引和 `architecture-map.json`。

```bash
python scripts/check_docs.py
python scripts/check_docs.py --validate
python -m pip install -r requirements-docs.txt
python scripts/check_docs.py --validate --build
```

影响分析按源码路径列出专题。严格构建检查导航、链接、源码符号、图、全文检索和本地站点资源。

## 归档与发布

运行产物、私人配置、历史交接和实验流水存放在源码树外。公开文档优先写可复现的流程，验证结果及适用边界放在末尾。
