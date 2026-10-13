# Mem0 源码审查：自动抽取、写入检索、冲突处理与普通召回

> 交接说明：本文说明审查版本的原生行为与当时的设计启示；启示中的扩展关系、时态策略不自动成为首版需求。当前编码约定以 [v1 设计与实施说明](agent_long_term_memory_v1_design.md)为准。

> 审查日期：2026-09-28  
> 当前版本锚点：PyPI `mem0ai==2.2.1`，发布证明对应上游提交 `94c3fe9f238f3dbf29c9ce98643bd71eb13077cd`。  
> 历史版本锚点：Git tag `v1.0.11`，作为本审查中“旧版动作式记忆更新”的可复核样本。  
> 本文只把当前 OSS Python SDK 的源码行为称为“当前行为”。Mem0 Platform 的托管能力单独标注，不据此推断 OSS 具备相同功能。

## 1. 结论摘要

Mem0 的自动写入算法经历了两种不同设计：

- **历史版（本文审查 `v1.0.11`）：** 一次 LLM 抽事实，再对每条候选事实做向量检索，第二次 LLM 根据候选旧记忆和新事实输出 `ADD / UPDATE / DELETE / NONE`，代码按动作执行。它确实有自动更新和语义冲突处理，但判断主要依赖提示词中的语义规则，没有结构化事实有效时间、证据权威等级或置信度策略。
- **当前版（`2.2.1` / 新的 V3 算法）：** 把一段新消息整体做一次向量检索，连同近期消息交给一次 LLM 调用；LLM 只产出 `ADD` 记忆。程序做 MD5 完全相同去重后写新记录。发生变化时默认让新旧记录并存，自动 `add(infer=True)` 不会更新、删除旧记忆。

因此，“Mem0 新版是 ADD-only”只描述 **自动推断式写入路径**，不代表 SDK 没有 `update()`、`delete()` 这类显式 CRUD API。当前检索里的“写入时找旧记忆”也不是普通 `search()` 的完整混合检索调用：它是一次作用域内的向量 Top-10 候选检索。普通 `search()` 才会组合语义候选、BM25 词项分数和实体加权；而且 BM25 和实体分数只重排语义检索已经找到的候选。

版本命名也容易造成误读：**“V3”是 Mem0 文档对新算法的称呼，不是当前 PyPI 包的版本号。** 当前包号是 `2.2.1`。另外，一些当前文档页和 `add()` docstring 还保留“自动判断 ADD/UPDATE/DELETE”的旧描述，与迁移指南、Add 文档和实际 `2.2.1` 源码不一致。

## 2. 版本和证据范围

| 观察对象 | 本文采用的锚点 | 为什么选它 |
|---|---|---|
| 当前 OSS 算法 | `mem0ai==2.2.1`，提交 `94c3fe9f238f3dbf29c9ce98643bd71eb13077cd` | PyPI 发布证明将该包和提交关联，可对当前源码作精确定位。 |
| 历史动作算法 | `v1.0.11` tag | 用作源码审计样本，不把“v1.1”误当作 PyPI 包版本。该代码确实包含两次 LLM 调用和四种动作。 |
| 算法迁移说明 | 官方 OSS v2→V3 migration 文档 | 官方明确说明旧版两次调用与新版 ADD-only 的差别。 |

这里不把旧算法笼统命名为“Mem0 v1.1”：历史源码还出现 `v1.1+` 的 API 输出格式说明，仓库中的 package tag、API 格式编号和后来称作 V3 的算法编号属于不同命名维度。之前提到的 `v1.1` 可理解为旧动作式算法/接口时期的简称；本审查用可固定的 `v1.0.11` tag 作为证据锚点。

## 3. 历史版：`v1.0.11` 的自动记忆写入

### 3.1 调用输入和角色策略

`Memory.add()` 接收字符串、单条消息字典或消息字典列表，并要求至少提供 `user_id`、`agent_id`、`run_id` 之一作为记忆作用域。常见输入是同一轮或一段对话中的带角色消息；API 不强制恰好一轮。

在 `infer=True` 时，代码用 `parse_messages()` 把消息变成包含角色标签的对话文本，再送给提取 LLM。默认提示词按作用域和角色分流：

- 一般 `user_id` 记忆使用用户记忆提示词，要求只从用户消息抽事实；assistant 复述内容不作为用户事实。
- 同时有 `agent_id` 且输入含 assistant 消息时，改用 agent 提示词，要求抽取 assistant 关于自身的事实。
- 可提供 `custom_fact_extraction_prompt` 覆盖默认事实抽取提示词。

所以历史版不是不加区分地把“用户和模型所有话都记成用户记忆”。消息会带角色进入提取上下文，默认用户记忆与 agent 记忆有角色归属规则。但消息的来源片段/消息 ID 不会自动成为每个事实的可追溯证据字段。

`infer=False` 是另一条路径：把输入消息逐条作为原始记忆写入，不做事实提取、相似旧记忆决策或语义去重。

### 3.2 两阶段抽取和冲突更新

```text
输入的带角色消息
  → parse_messages（形成角色标记的对话文本）
  → LLM #1：抽取 facts
  → 对每条 fact 单独生成 embedding
  → 同一 user/agent/run 作用域内向量检索 Top-5 旧记忆
  → 合并重复候选旧记忆，临时把 UUID 映射为短数字 ID
  → LLM #2：对旧记忆和新 facts 输出 ADD / UPDATE / DELETE / NONE
  → Python 按 event 更新向量库和变更历史
```

第二次 LLM 调用拿到的是**已检索的旧记忆文本和本轮提取的 facts**，不是整个历史记忆库。每条新 fact 都单独做向量查询，最多取 5 条候选，并按 `user_id`、`agent_id`、`run_id` 作用域过滤。这是写入路径的候选召回，不等于先调用公开 `Memory.search()` 再做冲突处理。

历史提示词定义了四种动作：

| 动作 | 提示词意图 | Python 执行结果 |
|---|---|---|
| `ADD` | 新信息不存在于已有记忆 | 创建新 UUID 记录。 |
| `UPDATE` | 同主题信息更丰富或值发生变化；提示词要求沿用旧 ID | 覆写同一向量记录的 `data`，更新 `hash` 和 `updated_at`，保留 `created_at`，另写 `UPDATE` history，含旧值和新值。 |
| `DELETE` | 新 fact 与旧记忆矛盾，或用户要求删除 | 从向量库物理删除当前记录；SQLite history 记录旧值、`DELETE` 和删除时间。 |
| `NONE` | 已有相同信息或不相关 | 通常不改内容；在传入新 session ID 等情况下，旧实现可以更新 scope 元数据。 |

提示词示例中，“喜欢打板球”更新为“喜欢和朋友打板球”属于补充信息后更新；“喜欢奶酪披萨”与“爱吃奶酪披萨”被视为同义，倾向 `NONE`；“喜欢奶酪披萨”与“不喜欢奶酪披萨”被视为矛盾，旧值被 `DELETE`。这些例子体现的是模型按提示词解释语义，并非数据库约束或规则引擎。

### 3.3 历史冲突判断的依据与边界

自动冲突判断主要依据：

1. 新事实与检索出的旧文本是否谈同一主题/属性。
2. 新旧文本是同义重复、信息补充还是相互矛盾。
3. 提示词给出的“保留信息更丰富的表述”“同一 ID 更新”“矛盾时删除旧记录”等示例。

该路径没有把 `valid_at`、`invalid_at`、证据来源可信度、事实置信度或显式“这是历史描述/纠正/假设”字段作为决策结构输入。消息内容里自然语言出现时间可能被抽进 fact，但并没有独立的事实有效区间字段来参与规则判断；系统记录的 `created_at` / `updated_at` 不是事实生效时间。因此，不能把旧 Mem0 的处理描述成“时间逻辑冲突算法”。它是**语义检索召回候选 + LLM 按提示词决策**。

由于先做 Top-5 检索，候选召回漏掉相关旧记忆时，第二次 LLM 就没有机会处理那条旧记录。模型动作本身也不是确定性保证：来源不同、模型不同、提示词定制不同，都可能令同类输入得到不同动作。

### 3.4 历史存储和可选图分支

单条向量记忆的主要 payload 包含 `data`、`hash`、`created_at`、`updated_at`、作用域标识（`user_id` / `agent_id` / `run_id`）和可选 metadata；有角色信息时还可能有 `role`、`actor_id`。SQLite history 单独记录 `memory_id`、旧值、新值、事件、时间、角色/actor 和删除标志等变更信息。UPDATE 复用旧 ID；DELETE 则移除向量记录并留下变更历史。

`v1.0.11` 还保留可选的 `GraphStore` 支路。`add()` 会让向量记忆处理和 `_add_to_graph()` 并行；后者收到的是非 system 原始消息的拼接文本，不是上面提取后的 fact 列表。图支路是另外一个存储/抽取子系统，不应和四动作向量记忆决策混为一个更新机制。当前迁移指南说明外部 graph store 集成已从 OSS 移除，Graph Memory 转到 Platform。

## 4. 当前版：`2.2.1` / V3 自动写入

### 4.1 输入内容和模型上下文

当前 `add()` 支持字符串、消息字典或 `list[dict]`；单个字符串会包装为 `{"role": "user", "content": ...}`。调用方传 `user_id`、`agent_id`、`run_id` 及 metadata/过期日期；至少一个作用域 ID 必填。常见用法仍是把一轮对话或一个片段（用户和 assistant 消息）传给 `add()`，但 API 不限定一轮。

`infer=True` 的当前实现会给抽取 LLM 以下上下文：

| 输入部分 | 当前代码如何提供 | 用途/边界 |
|---|---|---|
| 新消息 | 先解析成保留 role 的文本；其整体用于向量检索，原消息列表/格式文本进入 prompt | 默认 system prompt 明确要求从 user 和 assistant 消息抽取，使用 `attributed_to` 区分归属。 |
| 最近消息 | 从 SQLite 中相同作用域取最近 10 条已保存消息 | 帮助解析指代和对话上下文；不是把所有历史会话原文都放进 prompt。 |
| 相关旧记忆 | 一次对整段新消息做 embedding，作用域内向量检索 Top-10 | 给模型去重/关联上下文；这是直接 vector store search，不走普通 `search()` 的 BM25/entity 融合。 |
| 自定义指令 | `prompt` 参数优先，否则用配置的 `custom_instructions` | 调用方可约束抽取边界。 |
| summary / recently extracted | prompt builder 支持，但 `add()` 当前调用未传这些参数 | 当前调用中按空 summary、空 recently-extracted 内容格式化。 |
| Observation Date | prompt builder 支持 `timestamp`，但当前 `add()` 调用未传；默认到当前 UTC 日期 | 当前 OSS 的 `add(timestamp=...)` 会直接抛不支持错误，因此导入旧对话里的“昨天/上周”等表达，可能按处理当天而非原对话日期解析。 |

**角色处理需要谨慎理解：** 当前 prompt 写明“从 user 和 assistant 两类消息抽取”，同时要求输出 `attributed_to: user|assistant`。这能把 assistant 的建议/确认记为 assistant 来源，不等同于验证用户采纳了建议。调用端如果把所有召回文本一视同仁地当作用户事实，就会混淆建议与用户确认。

### 4.2 当前写入流程和去重机制

```text
add(messages, scope, metadata)
  → 同一 scope 最近 10 条消息（SQLite）
  → 将整段新消息 embedding，直接 vector search Top-10（同 scope）
  → 一次 LLM：只抽取 ADD 候选记忆
  → 批量 embedding 新记忆文本
  → 与 Top-10 返回项及当前 batch 做 MD5 精确文本去重
  → 新 UUID + ADD history 写入向量库/SQLite
  → 从已写入文本抽实体，写到辅助 entity store
```

当前 prompt 明确说模型的唯一操作是 `ADD`。候选旧记忆用于帮助模型识别重复/关联，不被第二次差异决策调用处理。代码再用 MD5 对**完全相同文本**去重：候选 Top-10 中相同 `hash` 的记录、以及本次 batch 的相同 `hash` 不会重复落库。改写/同义句不是 hash 相等；这类重复能否跳过主要依赖模型按提示词识别，且相关旧记录必须先进入 Top-10。

当前 prompt 要求每条新记忆可带 `linked_memory_ids`，表示与检索到的旧记忆有关。但当前 Python 自动写入代码读取和持久化的是 `text`、`attributed_to` 等字段，并未把模型返回的 `linked_memory_ids` 写进新记忆 payload 或建立对应的记忆到记忆边。代码确实另有**实体到记忆**关联：从记忆文本抽取实体，实体记录存 `linked_memory_ids`，普通 search 可通过实体命中给关联记忆加分。这不是通用的记忆图，也不是 prompt 中声明的记忆间链接；V3 migration 文档也说明 OSS 不再提供图记忆替代品。

### 4.3 当前记录字段

当前写入的向量 payload 主要包含：

- `data`：抽取后的文本记忆。
- `text_lemmatized`：供 BM25 keyword search 使用的词形归一文本。
- `hash`：`data` 的 MD5，供精确重复检测。
- `created_at`、`updated_at`：写入/修改记录时间；创建时通常相同。
- `attributed_to`：抽取输出中有值时记 `user` 或 `assistant`。
- `user_id`、`agent_id`、`run_id` 与调用方 metadata；可设置 `expiration_date`。
- 向量 embedding 和向量库自身 ID。

SQLite history 记录 `memory_id`、`old_memory`、`new_memory`、`event=ADD`、`created_at`、`is_deleted=0` 等。没有单独的 `valid_at` / `invalid_at`、来源消息 ID、证据片段、置信度或冲突状态字段。metadata 虽可存任意值，但系统不会因此自动解释它们为时间有效期或可信度策略。

当前 SDK 的 `Memory.update(memory_id, ...)`、`Memory.delete(memory_id)` 和 `history(memory_id)` 仍存在。它们是调用方明确指定记忆 ID 后的操作；与 `add(infer=True)` 的 ADD-only 自动路径是两个接口层次。显式 delete 会从主向量记录中删除，但 history 会保留被删前的文本和删除事件，因此不能把它等同于“所有副本/审计内容都物理擦除”。

## 5. 写入时旧记忆检索 vs 普通 `search()`

| 项目 | 历史版写入检索 `v1.0.11` | 当前版写入检索 `2.2.1` | 当前普通 `search()` |
|---|---|---|---|
| 查询文本 | 每条 LLM 抽取出的 fact 单独查询 | 本次 `add()` 的整段 role-labeled 消息 | 用户/应用传入的 query 字符串 |
| 向量化 | 每个 fact 单独 embed | 整段新消息 embed 一次 | query embed 一次 |
| 初始候选 | 每条 fact 向量相似 Top-5 | 同作用域向量相似 Top-10 | 语义检索 over-fetch，候选数 `max(top_k*4, 60)` |
| 作用域 | user/agent/run IDs | user/agent/run IDs | `filters` 中的 user/agent/run ID及 metadata 条件 |
| BM25 | 写入候选阶段没有 | 写入候选阶段没有 | 支持时跑 lemmatized keyword/BM25 search，并对语义候选打分 |
| Entity | 旧版可配置独立 GraphStore 分支，不属于向量冲突候选检索 | 本阶段不做 entity 检索；写入后才抽实体/关联 | Entity store 对 query 实体检索，给关联记忆加分 |
| 相似度阈值与重排 | 该内部候选步骤未调用公开 threshold/reranker | 该内部候选步骤未调用公开 threshold/reranker | 默认先用原始语义分数做 threshold=0.1 过滤；可配置，额外 reranker 需显式开启且已配置 |
| 过期过滤 | 不套用当前版过期过滤说明 | 该直接检索步骤未应用公开 search 的 expiration_date 过滤 | 默认过滤 expiration_date 已过期条目；show_expired 可调整；与 valid_from/to 时态查询不同 |
| 结果消费 | 旧候选压成 ID/text，供第二次 LLM 决策 | 旧候选压成 ID/text，供提取 LLM 去重与链接 | 返回正文、ID、分数、记录时间和 metadata，供应用构造回答上下文 |
| 是否等于 `search()` | 否；写入时直接按事实逐条查向量库 | 否；写入时直接向量查 Top-10 | 是独立公开查询路径，默认 `top_k=20`、`threshold=0.1`、`rerank=False` |

当前普通 search 的关键细节：

1. 对 query 做词形归一和实体抽取，并生成语义 embedding。
2. 在向量库先取语义 Top-N 候选；如 backend 支持，另外执行 keyword/BM25 search。
3. 查询实体在 entity store 中搜索，获取对应 `linked_memory_ids`，转为候选记忆分数加权。
4. 对语义候选应用 semantic threshold，再组合 semantic、归一化 BM25 和 entity boost 排序，截取 `top_k`。
5. 可选 reranker 默认关闭；vector backend 若未实现 `keyword_search()`，BM25 被禁用，退化为语义检索。

因此普通召回是“语义召回候选 + 词项/实体分数融合排序”，并非三种信号各自召回后做 union。某条记忆如果未进语义候选集，BM25 或 entity boost 不能把它新加进结果集。当前 OSS 的 `reference_date` 不支持；过期日期过滤存在，但这不是以事件有效时间为基础的时间线检索。

### 5.1 两种检索为什么分开：证据与推断（2026-10-08）

**有直接证据的用途差异：**历史写入检索为新 facts 的 ADD/UPDATE/DELETE/NONE 决策准备旧候选；V3 官方迁移说明将写入 Top-10 定义为去重上下文，固定提取提示词还要求既有记忆仅供去重和关联，不能从旧记忆重复生成新事实。普通公开 search 返回与应用 query 相关的记忆，官方聊天示例将它们交给回答模型。因此两条路径各自有明确的结果消费者和输入粒度。[迁移说明](https://docs.mem0.ai/migration/oss-v2-to-v3#how-the-new-algorithm-works)、[提取提示词](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/configs/prompts.py)、[官方聊天示例](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/README.md#basic-usage)。

**从实现推断的取舍：**旧版按 fact 查找，查询主题更集中，适合逐项比较，且 fact embedding 被缓存，后续实际保存正文相同时可复用；代价是多个 facts 要分别查询。V3 按整段新消息只检索一次，并固定只给提取模型 10 条旧候选，符合单次 LLM 提取和有限上下文的方向；多个话题也可能相互稀释。公开搜索先扩大语义池，再用可用信号排序，更侧重 query 相关性选择。这些是代码支持的工程解释，不是维护者关于“为什么写入没有调用公开 search/没有启用 BM25”的直接声明。

官方明确介绍单次 ADD-only 提取和多信号检索，但本轮未找到一手材料证明写入使用纯向量 Top-10 对所有去重/关联场景更好，或证明公开 search 用于写入必然更好；不把猜测写成官方设计动机，也不把 Top-5/Top-10 当成最优参数。[历史源码](https://github.com/mem0ai/mem0/blob/v1.0.11/mem0/memory/main.py#L555)、[固定当前源码](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L919)。

**对组合方案的约束：**当前复用公开 search 给 LangMem 准备旧候选是一项适配选择，不是照搬 Mem0 自动 add。即使 BM25/entity 关闭，公开 search 的过取、threshold、过期过滤和输出格式仍与内部直接向量检索不同。写入和回答应分别设置 query、候选数量、threshold 等参数；是否需要更改写入检索，应根据旧候选覆盖与整理行为决定，不能只凭接口复用方便认定效果等价。

## 6. 为什么文档会出现旧 `ADD/UPDATE/DELETE/NONE` 说法

这是**版本演进期间的文档/注释漂移**，不是当前 `2.2.1` 运行时还在自动走旧两次调用：

- 官方 v2→V3 migration 明确写明旧版是两次 LLM 调用、输出 `ADD/UPDATE/DELETE`；新版合成一次且只新增。
- 当前 Add 文档称 OSS/Platform 自动写入都采用 additive pipeline，既有记忆不会被自动覆盖或删除。
- 但当前仓库的 `memory-types.mdx` 对 `infer=True` 仍写着由单次 LLM 决定 `ADD/UPDATE/DELETE/NONE`；当前 `Memory.add()` docstring 也仍描述 LLM 决定 add/update/delete。
- 锁定的 `2.2.1` 源码实际只解析新 memories，按 `text`、`attributed_to` 建新记录；自动路径返回 ADD 事件。

判断版本行为时应优先以锁定发布对应的**可执行源码**和迁移指南为准，不能只看一个尚未同步的概念页或 docstring。历史版本的四动作描述本身并没有错；错位在于它被当前页面继续套用到了新算法上。

## 7. 按首期场景矩阵的初步判断

本节是源码能力映射，不是跑过 DB-GPT 需求评测后的分数。对“做不做得到”的判断限于 Mem0 默认自动路径。

| 场景 | 历史动作式路径 | 当前 ADD-only 路径 | 主要限制 |
|---|---|---|---|
| S1/S2 稳定偏好和个人事实 | 可抽取，向量候选加 LLM 合并/去重 | 可抽取；精确文本去重加 prompt 语义跳过 | 没有证据片段/来源消息 ID；语义相同的改写依赖 LLM。 |
| S3 状态变化 | 模型可能 UPDATE 覆写旧状态 | 新状态另加一条，旧值保留 | 历史版会丢失当前向量中的旧文本；当前版没有可靠的 current-state 选择保证。 |
| S4 明确纠正 | 语义提示可能 UPDATE 或 DELETE | 自动 ADD 新条目；不会自动标错旧值 | 当前可显式 update/delete，但调用端需要找到旧 ID 并决定动作。 |
| S5 未确定计划 | 依赖 LLM 是否保留“可能/计划”措辞 | prompt 可抽丰富文本，但没有独立 certainty/intent schema | 回答端必须理解自然语言文本；无结构化状态分类。 |
| S6 无法判定的冲突 | 模型被要求对候选旧记忆作动作，可能武断删除/覆写 | 允许新旧并存，但没显式记录“待核实/冲突” | 新旧同时召回时，普通 relevance 排序不等于判定谁可信。 |
| S7/S8 时间解析或缺失 | 无 `valid_at`/`invalid_at` 字段，提示词中的日期不构成时间模型 | 提示词有 Observation Date，但 OSS add 不接受 timestamp，默认为处理日期 | 导入历史对话可能错误解析相对时间；两代都不能回答完整双时间问题。 |
| S9 assistant 建议 | 用户记忆默认排除 assistant 事实；agent scope 可单独记 assistant 信息 | 两类角色均抽取并用 `attributed_to` 标注 | current 需下游尊重 attribution，不能把建议误当用户决定。 |
| S10 工具结果/用户确认 | 常规 role schema，没有统一 tool/evidence provenance contract | role 归属字段有限；未给模型结构化工具来源 | 无法仅凭默认字段审计执行观察与用户确认。 |
| S11 临时信息和过期 | 可用 metadata/外部策略，但不自动识别语义过期 | 可写 `expiration_date`，search 默认隐藏已过期记忆 | 过期时间不同于事实历史有效时间；默认算法不会根据事件结束自动设定。 |
| S12 当前/历史状态与来源查询 | query-time 主要语义向量检索，可 rerank；没有双时间筛选 | hybrid search 提高词项/实体召回，但无 OSS temporal ranking | 不能仅凭 Mem0 普通 search 保证“现在/去年/来源”分别答对。 |
| S13 用户要求忘记 | 自动冲突 prompt可 DELETE，显式 `delete` 也可操作 | 自动 add 不删除，显式 `delete` 可操作 | 显式删除 history 保留旧文本；要先定义删除和审计的产品语义。 |
| S14 作用域隔离 | 向量检索按 user/agent/run filter | 写入检索和 search 皆按作用域 ID filter | 调用端必须稳定传入正确身份和租户范围；metadata 不能替代可信身份。 |

## 8. 对自研长期记忆可复用的设计启示

### 值得借鉴的部分

- 将来源消息角色保留到抽取 prompt，并明确事实的归属人/来源类别。
- 在写入时先找候选旧记忆，避免完全无上下文地判断重复；候选检索应严格 scope filter。
- 对一批抽取结果批量生成 embedding、批量写入，并在本次 batch 内做幂等去重。
- 将当前 payload 与变更历史分开保存；更新和删除时保留旧值、事件和记录时间，形成可审计轨迹。
- 普通召回可以利用向量相关性之外的精确词项、实体和元数据过滤信号，并保留可解释分数。
- 明确区分“辅助实体索引”与完整知识图谱，避免把实体相似度加权误称为关系推理。

### 应改造或不能直接照搬的部分

- 历史版“检索 Top-5 后让 LLM 直接覆写/删除”把检索遗漏和模型判断放大为不可逆状态变化。若需状态更新，应先保留证据版本，再显式产生“补充/替代/纠正/冲突待查”等关系。
- 新版 add-only 保留历史，但不等于有冲突管理；当多条互相矛盾的事实并存时，仅靠普通相关性排序不能保证当前状态正确。
- 不能把 `created_at`/`updated_at` 当成事实的 `valid_at`。至少要分开来源记录时间、事实时间/有效区间，并允许未知。
- prompt 中模型生成的链接字段要么真实校验并持久化，要么不要把“形成记忆图”列作已经实现的能力。当前 OSS prompt 的 `linked_memory_ids` 不会被主写入路径保存。
- 记忆条目需要可追溯到原始证据 ID/片段，并区分用户原话、assistant 建议、工具观察和用户确认；仅有 `attributed_to` 不够完整。
- 对自动抽取失败、候选检索失败和存储失败，应区分“无可记内容”和“处理失败”；当前锁定版本在 LLM/向量写入失败时会抛错误，这有利于上游区分失败，但持久化与 history/entity 多存储之间仍可能出现部分成功。

## 9. 仍待和 LangMem、Graphiti 对照验证的问题

完成 Mem0 审查后，当前不能据此选基座。接下来审查另外两者时应重点回答：

1. 是否保存原始 episode/证据并能反查候选记忆来源？
2. 写入检索除了语义相似度，还有没有结构字段、关键词、图关系或时序候选？
3. 冲突处理结果是覆盖文本、创建新版本、标旧失效，还是保留待确认冲突？每种动作能否审计/恢复？
4. 时间字段来自哪里；输入没有时间时怎样表达未知；系统时间是否错误替代事件时间？
5. 召回是否按“当前/历史/时间范围”查询，而不仅仅是相似度排序？
6. 默认策略和调用方自定义策略分别覆盖什么，真正需要自行实现的部分在哪里？
7. 三者哪些能力属于开源代码，哪些依赖服务端或托管产品？

三家源码审查完成后，再用 [首期场景矩阵](agent_long_term_memory_scenarios.md) 做统一取舍，并更新总计划里的数据模型与核心流程，而不是从 Mem0 的字段直接推出我们的设计。

## 10. 一手资料索引

- [PyPI `mem0ai` 2.2.1 发布与 provenance](https://pypi.org/project/mem0ai/)：提交 SHA、发布时间及包版本。
- [旧版 `v1.0.11` `Memory.add()` 和动作执行源码](https://github.com/mem0ai/mem0/blob/v1.0.11/mem0/memory/main.py#L337)：事实抽取、Top-5 搜索、第二次 LLM、CRUD 执行以及向量/图支路。
- [旧版 `v1.0.11` 用户/Agent/更新提示词](https://github.com/mem0ai/mem0/blob/v1.0.11/mem0/configs/prompts.py#L52)：角色抽取约束和四动作决策规则。
- [旧版 `get_fact_retrieval_messages()` / `parse_messages()`](https://github.com/mem0ai/mem0/blob/v1.0.11/mem0/memory/utils.py#L13)：如何选择用户或 agent prompt，以及如何保留角色标签。
- [当前固定提交 `main.py`](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L697)：当前 add、Top-10 写入检索、add-only 数据处理、search、update/delete/history。
- [当前固定提交 `prompts.py`](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/configs/prompts.py#L419)：V3 提示词、`attributed_to`、`linked_memory_ids` 和 Observation Date。
- [当前固定提交 `scoring.py`](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/utils/scoring.py#L53)：BM25、语义和 entity boost 的分数组合。
- [官方 OSS v2→V3 迁移指南](https://github.com/mem0ai/mem0/blob/main/docs/migration/oss-v2-to-v3.mdx)：算法变化、ADD-only 和当前 OSS 图记忆边界。
- [官方 Add 文档](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/add.mdx)：当前 add 的 additive 描述和 `infer=False`。
- [当前仍保留旧动作表述的 Memory Types 文档](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-types.mdx)：与当前迁移指南和可执行源码对照。
