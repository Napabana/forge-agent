# Repo Map 增量索引与真实任务消融 TODO

## 背景

当前 Forge Agent 的 Repo Map 已经完成从静态仓库摘要到 Query-aware 排序的重构，但现有实现仍处于“内存扫描缓存 + Query 重排 + 写后全量失效”的中间形态。

最初需要解决的问题是：

1. 大仓库首次生成完整 Repo Map 成本较高；
2. 很多任务只涉及局部模块，没有必要把整个仓库结构都注入模型；
3. Repo Map 又希望尽量保持稳定，以利于 system prompt 前缀复用和 provider cache；
4. 对话过程中用户需求可能变化；
5. Agent 或用户修改代码后，旧的仓库结构快照可能失效。

当前实现已经解决了其中一部分，但还没有完成持久化增量索引，也没有做 Repo Map 对真实 Coding Agent 成功率与成本影响的正式消融。

---

## 当前真实实现

### 1. Query-aware Repo Map

`context/repo_map.py` 当前会先扫描仓库并提取结构信息，再根据任务描述进行动态排序。

Query relevance 主要使用：

- 文件路径；
- 代码符号；
- 源码正文；
- import / reference 等结构信号；
- snake_case / camelCase 拆词；
- 简单词形归一；
- 高频通用词降权。

最终通过 Token Budget 截断，只把预算范围内的高相关仓库结构放入 prompt，而不是把完整仓库地图全部注入模型。

### 2. 仓库扫描与任务视图已经部分解耦

同一个 `Agent` 在同一个仓库中执行不同任务时：

- 仓库未变化时，可以复用 `RepoMap` 内存中的扫描结果；
- `task.description` 变化时，会更新 Query 并重新生成排序后的 Repo Map 文本；
- 不需要仅因为用户问题变化就重新读取和解析整个仓库。

因此当前逻辑可以理解为：

```text
仓库文件
   ↓
结构扫描快照
   ↓
任务 Query 重排
   ↓
Token Budget 截断
   ↓
模型侧 Repo Map
```

### 3. Chat 中的需求变化

`entry/chat.py` 中每个用户轮次都会创建新的 `Task(description=user_input)`，同时复用：

- 同一个 Agent；
- 同一个 backend / registry；
- 跨轮 ConversationHistory。

因此，对话轮次之间用户改变需求时，会使用新的任务描述重新排序 Repo Map。

当前不支持单个 `Agent.run()` 执行过程中实时注入新的用户目标。若用户需要中途改变当前正在执行的任务，只能等当前 round 结束，或先 cancel 再开启新 round。

### 4. 代码修改后的 Repo Map 刷新

当前同一 Run 中，如果 `file_write`、`file_edit` 或 `edit` 成功：

```text
成功写代码
   ↓
invalidate_repo_map_cache()
   ↓
_repo_map_force_refresh = True
   ↓
下一 step repo_map.build(force_refresh=True)
   ↓
重新扫描整个仓库
```

也就是说，当前“写后刷新”是仓库级全量刷新，不是 changed-file 增量更新。

Chat 两轮之间还会用 `repository_fingerprint()` 检查仓库状态变化。当前 fingerprint 由：

- Git HEAD；
- working-tree 状态；
- changed/untracked 文件内容 hash

共同组成，用于判断仓库是否发生变化。

---

## 当前设计的优点

1. 不再把整个仓库摘要无差别塞给模型，而是按任务动态排序；
2. Query 变化和仓库结构变化被区分开，任务变化不必重新 parse 仓库；
3. 同一 Run 内未修改代码时可以复用 Repo Map 文本；
4. 代码修改后会主动失效缓存，避免 Agent 继续使用已经过期的仓库视图；
5. 已经有冻结的 12-case commit-history retrieval benchmark，可验证 Query-aware ranking 相比 static ranking 的检索质量。

已有正式结果：

- MRR：`0.097 → 0.319`
- 固定 Token 预算下目标文件召回率：`36.5% → 63.5%`
- reference counting 子步骤中位耗时：`35.1s → 0.49s`

注意：`35.1s → 0.49s` 仅是 reference counting 子步骤，不代表整个 Repo Map 首次构建耗时。

---

## 当前未解决的问题

### 1. 写代码后仍会全量重建

这是当前最明显的性能边界。

如果 Agent 只修改了一个文件，下一 step 仍然会重新：

- discover files；
- read source；
- tree-sitter parse；
- extract symbols；
- calculate import/reference；
- rerank；
- render Repo Map。

在大仓库中，这种策略会放大每次代码编辑后的额外成本。

### 2. 没有持久化索引

当前扫描缓存主要是进程内状态。

新进程、新 session 或重新启动后，无法直接复用之前已经完成的仓库结构解析结果。

### 3. Repo Map 的真实 Agent 收益还没有正式证明

目前的正式实验主要证明：

```text
Static ranking
vs
Query-aware ranking
```

在 commit-history retrieval case 上，Query-aware 排序能够提高目标文件排名和预算内召回。

当前没有正式证明：

```text
No Repo Map
vs
Static Repo Map
vs
Query-aware Repo Map
```

在真实 Coding Agent 任务上是否提高：

- task solved / verifier pass；
- 首次定位正确文件的 step；
- 总读取文件数；
- input / total Token；
- latency；
- Agent 总体成功率。

因此简历和面试中不能把当前 Repo Map retrieval benchmark 直接表述为 Agent 成功率提升。

### 4. Prompt Cache 仍有进一步优化空间

当前 Repo Map 位于 system prompt 的 Repository 区域，而工具说明位于其后。

只要 Query 变化导致 Repo Map 文本变化，后续 prompt prefix 也会变化，因此：

- 底层扫描缓存可以复用；
- 但 provider prefix cache 不一定能够完整复用。

Repo Map 构建性能与 Prompt Cache 是两个不同问题，需要分别优化。

---

## 建议的下一阶段设计

目标：将 Repo Map 从“可缓存摘要”升级为“持久化结构索引 + 增量更新 + Query-aware 渲染”。

推荐分层：

```text
Repository State Detector
        ↓
Changed Files
        ↓
Persistent Structural Index
        ↓
Query-aware Ranking
        ↓
Token Budget Rendering
        ↓
Model-visible Repo Map
```

### A. Repository State Detector

职责：只判断仓库发生了什么变化，不负责仓库理解。

Git 仓库优先使用：

```text
HEAD / old HEAD
+ git status
+ git diff --name-status
```

需要覆盖：

- added；
- modified；
- deleted；
- renamed；
- staged；
- unstaged；
- untracked。

非 Git 目录可回退到：

```text
mtime + size
→ 必要时 content hash
```

### B. Persistent Structural Index

建议先用 SQLite，不引入 FAISS、Embedding 或向量数据库。

最小持久化字段：

```text
file
- path
- content_hash
- mtime
- language

symbol
- file
- name
- kind
- line

import
- source_file
- target/module

reference
- source_file
- symbol
```

首次运行：

```text
full scan
→ tree-sitter parse
→ build index
→ persist
```

后续启动：

```text
load index
→ detect changed files
→ only update changed files
```

### C. Changed-file Incremental Update

如果 Forge 自己执行 `file_write/file_edit`，实际上已经知道具体 changed file，因此不需要先重新扫描整个仓库。

理想流程：

```text
file_edit("agent/core.py")
        ↓
重新 parse agent/core.py
        ↓
删除其旧 symbol/import/reference 数据
        ↓
写入新的结构数据
        ↓
局部更新受影响关系
        ↓
重新 ranking
```

用户在 Forge 外部改文件时，再通过 Git diff / repository fingerprint 找 changed files。

### D. Reference Graph 增量维护

单文件变更可能改变其他文件与该 symbol 的关系，因此需要避免只更新单文件自身信息。

建议显式维护：

```text
file → definitions
file → references
symbol → defining files
symbol → referencing files
```

更新文件时：

1. 删除旧 definitions / references；
2. 加入新 definitions / references；
3. 找出受影响 symbol；
4. 只重算相关 reference edge。

无需每次重新做全仓 symbol × file reference counting。

### E. Query-aware View

Query 改变时：

```text
Persistent Index 不变
→ 重新计算 query relevance
→ rerank
→ Token Budget render
```

需求变化不应触发仓库重新解析。

### F. Prompt Cache Layout

后续可单独评估 system prompt 结构。

目标是把稳定内容尽量放到前缀：

```text
stable system rules
stable tool schema
...
query-dependent repo context
current conversation
```

避免动态 Repo Map 位于过早位置导致后面的稳定工具 schema 也失去 prefix cache 复用机会。

该项必须通过真实 provider 的 cached token 数据验证，不应仅凭结构推断宣称收益。

---

## 建议的真实 Coding Agent 消融实验

### 实验目标

正式回答：Repo Map 是否真的改善 Agent 的任务完成能力与探索成本，而不只是 retrieval benchmark 指标。

### Variants

```text
A. No Repo Map
   仅依赖 find_files / search / file_read

B. Static Repo Map
   使用静态 importance，不使用任务 Query

C. Query-aware Repo Map
   当前实现

D. Incremental Query-aware Repo Map
   完成持久化增量索引后加入
```

### 控制变量

固定：

- model；
- provider；
- temperature / sampling；
- max_steps；
- Token Budget；
- tool set；
- completion guard；
- independent verifier；
- task fixtures；
- repo revision。

真实模型有随机性时，正式结论应尽量做多次重复，而不是单次结果。

### 指标

至少记录：

1. task solved / verifier pass；
2. 首次读取 ground-truth 目标文件的 step；
3. 读取文件数量；
4. search / find_files 调用次数；
5. input tokens；
6. total tokens；
7. total latency；
8. Repo Map initial build time；
9. Repo Map incremental update time；
10. cached input tokens / cache hit ratio（provider 支持时）。

### 建议任务集

优先使用固定真实代码仓库中的：

- 单文件 bug；
- 跨文件 bug；
- 模块级功能修改；
- 文件名无法直接暴露目标位置的任务；
- 需要通过 symbol/reference 定位的任务；
- 中途至少产生一次代码修改并继续探索的任务。

这样才能同时测到：

- 初次定位能力；
- 写后索引更新成本；
- Query-aware ranking；
- 长链路执行成本。

---

## 推荐实施顺序

### P2-RM1：建立 baseline

- [ ] 增加 No Repo Map / Static / Query-aware 三组真实 Agent 消融；
- [ ] 固定 task、model、budget 和 verifier；
- [ ] 记录定位 step、文件读取数、Token、latency、verifier pass；
- [ ] 不修改现有 Repo Map 逻辑，先得到 baseline。

原因：如果当前 Repo Map 对真实 Agent 帮助不明显，应先知道问题在 ranking、prompt layout 还是 Agent 自身探索策略，而不是直接投入较大的增量索引重构。

### P2-RM2：Persistent per-file index

- [ ] SQLite 建立 file/symbol/import/reference 基础表；
- [ ] 首次 full scan 写入 index；
- [ ] 重启后复用 index；
- [ ] content hash / mtime 失效策略；
- [ ] 保持 Query-aware ranking 结果与现实现语义一致。

### P2-RM3：Changed-file incremental update

- [ ] file_write/file_edit 后只更新目标文件；
- [ ] Git diff 检测外部修改；
- [ ] 支持 add / modify / delete / rename；
- [ ] 增量维护 reference relationships；
- [ ] 增加 full rebuild fallback，保证索引异常时正确性优先。

### P2-RM4：Prompt Cache layout

- [ ] 记录当前不同 round 的 cached tokens；
- [ ] 比较 Repo Map 位于 system prompt 前部/后部的 cache 行为；
- [ ] 保证 prompt 语义和工具能力不变化；
- [ ] 只有真实 provider usage 显示收益后再写正式结论。

### P2-RM5：最终消融

- [ ] 加入 Incremental Query-aware variant；
- [ ] 与 P2-RM1 使用完全一致协议；
- [ ] 单独报告 retrieval quality、index performance 和 Agent E2E 指标；
- [ ] 不混淆子步骤性能、检索指标和 Agent 成功率。

---

## 简历 / 面试证据边界

当前可以说：

> 将仓库结构扫描与任务相关视图部分解耦，缓存扫描结果，并根据任务描述对路径、符号和源码内容动态排序，在 Token Budget 内优先注入高相关文件；在 12 个真实 commit-history case 中，MRR 从 0.097 提升到 0.319，预算内目标文件召回率从 36.5% 提升到 63.5%。

当前不应说：

- Repo Map 让 Coding Agent 成功率提升 X%；
- Repo Map 将完整仓库扫描耗时从 35.1s 降到 0.49s；
- 当前已经是增量索引；
- 当前只扫描任务相关模块；
- 当前实现已经解决所有大仓库首次索引问题；
- 当前 Prompt Cache 因 Repo Map 获得稳定 X% 命中率。

如果未来 P2-RM1～RM5 完成，再根据真实报告更新 Evidence Pack 和简历口径。

---

## 设计结论

当前 Repo Map 的核心方向是正确的：

```text
任务变化 ≠ 仓库变化
```

因此两者不应共用同一个失效机制。

长期更合理的结构是：

```text
仓库变化
→ changed-file detection
→ incremental persistent index update

用户需求变化
→ query rerank
→ Token Budget render
```

即：

> Repo Map 不应继续被实现为“每次需要时重新生成的一段仓库文本”，而应逐步拆成“持久化仓库结构索引”和“面向当前任务动态生成的模型视图”两个独立层。

该方向属于 P2 性能与大仓库扩展，不影响当前 P0/P1 已完成状态。