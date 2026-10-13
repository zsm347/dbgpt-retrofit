# Graphiti 源码审查：episode 抽取、旧图检索、冲突与时间

> 交接说明：本文是固定版本的图记忆机制审查；Graphiti 当前用于来源/时间设计参考，首版不要求安装图数据库或接入其运行链路。编码约定以 [v1 设计与实施说明](agent_long_term_memory_v1_design.md)为准。

> 审查日期：2026-09-28  
> 发布包锚点：PyPI `graphiti-core==0.30.2`，wheel SHA-256：`97674a49514130db175faecf23221ce9338240be3cbe13a7e23e5a5033892b9f`。  
> 源码锚点：Git tag `v0.30.2`，提交 `eaa4128681bc53487138a4bbc22d58336ebe70d2`。从 PyPI 下载的 wheel 与该 tag 下 10 个关键源码文件逐字节一致。许可证 Apache-2.0。  
> 范围：Graphiti 开源 `graphiti-core` 的 episode 写入、实体与事实抽取、旧图候选检索、冲突失效及普通召回；不把 Zep 托管产品能力算作开源核心能力。

## 1. 结论摘要

Graphiti 的主写入路径 `Graphiti.add_episode()` **不是“只追加、不处理冲突”**。它会抽取事实关系，检索候选旧关系，由 LLM 判断重复和矛盾，再结合事实的 `valid_at` 时间决定是否给旧关系写入 `invalid_at`。旧关系不会因冲突而物理删除；它保留在图中，作为历史关系继续可查。

不过“Graphiti 自动按最新时间解决所有冲突”也不准确。发生失效至少依赖三件事：旧关系进入有限候选集、LLM 把新旧事实识别为矛盾、双方具有可比较的 `valid_at`。候选漏召回、时间缺失或时间相同，都可能使旧事实没有被失效。冲突识别主要由 LLM 完成，时间比较和区间写回由代码完成。

Graphiti 有几种不同的“找旧信息”流程，不能统称为向量检索：

1. **前文 episode：** 按 `valid_at <= reference_time`、`group_id` 和 `source` 查最近 episode；不是语义/向量搜索。
2. **实体去重：** 把新实体名称嵌入，按余弦相似度取最多 15 个节点候选（阈值 0.6），再走精确名、模糊名或 LLM 消歧。
3. **事实去重/冲突：** 先按关系两端节点取已有边，再用 BM25 全文检索和事实向量相似度检索组成的混合搜索找候选；默认 RRF 重排，最终每条检索路径最多 10 条。

`reference_time` 必须由调用方传入。它是输入 episode 的时间锚点，不是 Graphiti 从消息元数据中自动推断出的必然“事实发生时间”。若调用方没有可信来源时间，却用处理时刻填入，模型就会拿处理时刻解析“最近”“现在”等表达；这会将系统处理时间误作事件时间。

## 2. 写入输入与完整调用链

### 2.1 `add_episode()` 接收什么

`add_episode()` 的关键输入是：

| 参数 | 含义 | 是否必需/默认值 |
|---|---|---|
| `name` | episode 名称 | 必需 |
| `episode_body` | episode 原文，必须是字符串 | 必需 |
| `source_description` | 数据来源描述 | 必需 |
| `reference_time` | episode 的参考时间 | 必需，方法签名没有默认值 |
| `source` | `message`、`text`、`json` 等输入类型 | 默认 `EpisodeType.message` |
| `group_id` | 图的隔离分区 | 可选；省略时使用默认 group |
| `entity_types` / `edge_types` | 自定义实体/关系 schema | 可选 |
| `previous_episode_uuids` | 显式指定用于上下文的历史 episodes | 可选；设置后取代自动检索 |
| `custom_extraction_instructions` | 追加到抽取提示的规则 | 可选 |

Graphiti 接收的是一条 episode 字符串，不是 `list[HumanMessage, AIMessage]` 这样的消息对象。`EpisodeType.message` 约定把多轮内容格式成 `speaker: message` 行，例如 `user: ...\nassistant: ...`。调用方可将一轮或多轮拼成一个 episode；Graphiti 不替调用方决定边界，也不默认只读取用户消息。

消息抽取提示会把冒号前的发言者也作为实体抽取，并从当前文本抽实体和关系。它没有通用的“用户陈述可信、assistant 建议不可信、工具输出待确认”归属模型。若把用户与 assistant 的内容一并放进 episode，提示词虽可看到角色文本，但写入 schema 没有强制的 speaker/authority/confidence 字段。

### 2.2 主写入流程

```text
add_episode(name, episode_body, source_description, reference_time, source, group_id, ...)
  → 取同一 group、同一 source 且 valid_at 不晚于 reference_time 的最近 episodes
  → LLM 从当前 episode 抽实体；向量检索相似旧实体并去重/消歧
  → LLM 从当前 episode + 实体列表抽关系事实及 valid_at / invalid_at
  → 为新事实取旧边候选：同端点候选 + 全图语义/全文候选
  → LLM 判断 duplicate_facts 与 contradicted_facts
  → 代码按 valid_at 比较事实时间，必要时给旧边或迟到的新边写 invalid_at
  → 保存 episode、实体、关系边、来源链接及失效字段
```

`add_episode()` 会在一次调用中完成节点抽取、关系抽取、消歧、冲突处理与图写入。官方实现建议对同一图中的 episode 顺序调用并等待前一个完成；文档推荐将较重的 `add_episode()` 放入后台任务/队列，以免阻塞对话响应。这里的“后台”描述执行时机，不是一个自动监听所有对话的记忆服务。

另一个文档/实现差异：`add_episode()` 的参数说明称自动历史按 `created_at` 日期取最近记录，但默认 `retrieve_episodes()` 实际按 `valid_at <= reference_time` 过滤和排序。本文以可执行查询为准。

### 2.3 抽取模型具体看到什么

实体抽取模型收到：

- 当前 `episode_body`；
- 由 `reference_time` 得到的 episode 时间；
- 最多 10 条被选为上下文的历史 episode 内容和时间戳；
- 可选实体类型说明及自定义抽取指令。

消息模式的实体提示要求只抽取当前消息中明确出现、足够具体的实体，并要求抽取发言者；历史消息仅用于指代消歧，不应从历史中重新抽实体。它排除抽象概念、代词、泛化名词等。之后关系抽取模型收到当前 episode、抽出的实体清单、历史 episode 的内容/时间、参考时间及可选关系类型 schema。它只应抽出连接实体清单中**两个不同实体**的事实关系；关系端点不在实体清单中时，该关系会被代码丢弃。

默认关系结果结构包括 `source_entity_name`、`target_entity_name`、`relation_type`、自然语言 `fact`、`valid_at`、`invalid_at` 和 episode 来源索引。提示要求保留数字、日期、限定词等具体细节，禁止从无关事件臆测时间；对现在仍成立的陈述，使用该 episode 时间作 `valid_at`。自定义 Pydantic 实体/关系类型可增加 schema 属性，但不会自动建立用户记忆的事实类型分类或置信度体系。

## 3. 三类写入时检索到底怎么做

### 3.1 历史 episode：时间过滤，不是相似度召回

没有显式传 `previous_episode_uuids` 时，`add_episode()` 调用 `retrieve_episodes(reference_time, last_n=10, group_ids=[group_id], source=source)`。默认数据库实现按 `episode.valid_at <= reference_time` 过滤，再按 `valid_at` 由近到远取最多 10 条，最后恢复时间正序交给模型。它是时间/分区/来源约束的数据库查询，不是向量检索。它给抽取模型补上下文、帮助解析代词和连续事件；它不是“检索语义最相似的 10 条旧记忆”。

如果调用方给了 `previous_episode_uuids`，则直接按 UUID 取回这些 episode，自动的 group/source/time 选择不再控制这个列表。这个参数应由可信调用方在正确作用域内维护。

### 3.2 实体节点：名称向量候选 + 确定性/LLM 消歧

对每个抽取实体，Graphiti 将 `node.name` 做 embedding，在相同 `group_id` 下做节点名称向量相似度检索，最多取 15 个候选，最低余弦分数 0.6。随后优先做小写、空白归一化后的精确同名匹配；对足够长且信息量较高的名称，再走 MinHash/LSH 候选与字符 3-gram Jaccard 相似度（阈值 0.9）。没有确定匹配的候选才升级给 LLM。

实体 LLM 消歧输入包括抽取实体名/类型、候选实体名/类型/最多 120 字摘要/属性、当前 episode 和历史 episode 文本/时间；提示要判断是否同一现实实体并合并摘要。该步骤是实体同一性消歧，不是“这个新属性是否与旧属性冲突”的处理。实体节点本身没有 `valid_at`/`invalid_at` 区间；关系事实才是主要的时间冲突载体。

### 3.3 事实关系：两个混合检索候选集

每条新事实边先生成 `fact` 的 embedding。代码先按新旧关系的 `source_node_uuid`、`target_node_uuid` 取端点相同的旧边，再从中通过 `EDGE_HYBRID_SEARCH_RRF` 找到相关边，用于重复判断。随后对当前 `group_id` 的图按新事实文本再跑一次同一混合搜索，作为可能冲突/失效的候选；与前一候选集重叠的 UUID 会从后一列表去重。

`EDGE_HYBRID_SEARCH_RRF` 的检索信号是：

1. 对关系 `name` / `fact` 做 BM25/全文检索；
2. 对 `fact` embedding 做 cosine similarity 检索；
3. 两个候选列表各自先取最多 `2 × limit`（默认 limit=10，即每路最多 20），再用 Reciprocal Rank Fusion 合并，最终候选最多 10 条。

同端点的重复候选还受 `edge_uuids` filter 限制；宽泛的失效候选搜索只限定 `group_id`。所以“Graphiti 写入前是纯向量检索”不正确：关系冲突路径默认是**全文 + 向量 + RRF**，而前文 episodes 和实体节点检索又是另外两种路径。具体全文索引和向量能力依赖所选 graph backend；Neptune 的全文检索还使用 OpenSearch Serverless。

## 4. Graphiti 怎样判断和执行冲突处理

### 4.1 LLM 负责语义识别

若新旧事实没有端点、文本都相同的快速命中，Graphiti 调用 `dedupe_edges.resolve_edge`，结构化输出：

- `duplicate_facts`: 与新事实表达同一事实的旧边索引；
- `contradicted_facts`: 被新事实矛盾的旧边索引，可来自重复候选或宽泛失效候选。

提示明确要求数字、日期和关键限定不同的事实不能当作重复；它用“Alice 是软件工程师”变成“高级工程师”作为矛盾示例，两个不同日期的跑步事件则不是矛盾。

但这个冲突判定 prompt 实际只拿到新旧**事实文本**和索引。它没有结构化的 `valid_at`、`invalid_at`、`created_at`、来源 episode、speaker、confidence 或证据跨度。时间可以因为事实文本中包含日期而被模型读到，但代码没有把候选边的时间字段作为显式判据交给这个 LLM。也没有模型置信度或“待核实”结果字段。

### 4.2 代码负责时间区间和失效写回

语义判断之后，时间逻辑不是简单地“无条件以最新插入者为准”：

- 如果新事实与候选旧事实矛盾，且旧事实 `valid_at < 新事实 valid_at`，代码将旧边 `invalid_at` 设为新事实开始生效的时间，并将系统记录失效操作的 `expired_at` 设为当前 UTC 时间。
- 如果新到达的事实在事件时间上早于一条已知的矛盾事实，代码会将这条迟到的新边的 `invalid_at` 设成较新候选的 `valid_at`，并给新边设 `expired_at`。这样回填历史事实时可保留它曾成立的时间段，而不让旧事件覆盖较新的状态。
- 若已有区间已在新事实开始前结束，或新事实已在候选旧事实开始前结束，代码会跳过重复失效。
- 若 `valid_at` 缺失或相等，严格的先后比较无法触发；即使 LLM 判为矛盾，也可能没有哪一方被失效。
- 完全相同端点、归一化后完全相同文本的快速路径会直接复用旧边并追加 episode UUID，不再调用冲突模型，也不重算它原有的时间区间。
- Prompt 允许同一旧事实同时被标为 duplicate 和 contradicted；但实现会先把已有重复边选为 `resolved_edge`，后续时间比较也围绕这条旧边进行。语义更新若同时得到这两个标签，是否能按预期形成新有效区间并未由源码保证，应作为专门用例验证。

因此主路径包含写入时的自动冲突处理，但不是独立、无条件运行的“冲突引擎”：它是**候选检索 → LLM 判断事实是否重复/矛盾 → 按事实时间做有限区间修改**。旧边保留，系统不会因这一步物理删除它。

### 4.3 时间字段从哪里来

| 字段 | 语义 | 来源 |
|---|---|---|
| Episode `created_at` | 系统处理/写入 episode 的时间 | `add_episode()` 内部 `utc_now()` |
| Episode `valid_at` | episode 的参考时间 | 调用方必传的 `reference_time` |
| Edge `created_at` | 系统创建关系边的时间 | 代码内 `utc_now()` |
| Edge `reference_time` | 产生该关系边的 episode 时间 | `episode.valid_at` / 输入 `reference_time` |
| Edge `valid_at` | 事实在现实中开始成立的时间 | 抽取提示从文本及 episode reference time 解析 |
| Edge `invalid_at` | 事实在现实中不再成立的时间 | 提示从更正/结束表达抽取，或冲突代码用替代事实的 `valid_at` 补写 |
| Edge `expired_at` | 系统记录这条边被失效的时间 | 代码在失效时写入当前 UTC 时间 |

关系抽取 prompt 在可解析时用 episode 时间解析“去年”“上周”等相对表达；若陈述为当前持续状态，则用 episode 时间作为 `valid_at`。如果历史事实的时间没有明确表达且无法解析，提示允许把 `valid_at` 和 `invalid_at` 留空。核心 API 本身要求 `reference_time` 非空；某些外围队列/API wrapper 会在未提供时用 `datetime.now(UTC)` 补上，这只是应用层默认，不是从原始消息中恢复出的真实发生时间。

## 5. 保存的数据结构和来源

| 图对象 | 核心字段 | 含义/限制 |
|---|---|---|
| `EpisodicNode` | `uuid`, `name`, `group_id`, `source`, `source_description`, `content`, `created_at`, `valid_at`, `entity_edges` | 原始 episode 与其时间/来源。默认 `store_raw_episode_content=True`；关闭后保存前会清空 content。 |
| `EntityNode` | `uuid`, `name`, `labels`, `group_id`, `created_at`, `name_embedding`, `summary`, `attributes` | 实体身份、摘要和自定义属性；不承载事实有效区间。 |
| `EntityEdge` | `uuid`, `source_node_uuid`, `target_node_uuid`, `name`, `fact`, `group_id`, `fact_embedding`, `episodes`, `created_at`, `valid_at`, `invalid_at`, `expired_at`, `reference_time`, `attributes` | 关系事实、其有效区间、系统失效记录和来源 episode ID。 |
| `EpisodicEdge` (`MENTIONS`) | episode UUID、entity UUID、`created_at` | 连接来源 episode 与其提及的实体。关系边另保存其来源 episode UUID 列表。 |

Graphiti 的 provenance 优势是事实关系能反向链接到产生/支持它的 episode；episode 默认可保留原文。但它不是每条抽取事实都有精确消息 ID、字符跨度、说话者归属和置信分数的证据模型。`EpisodicNode` 声明了 `episode_metadata` 字段，但在该提交的默认 save / load 代码路径中没有持久化或还原它；不要仅凭 Pydantic 模型字段就认为自定义 episode metadata 已进图。

图中还会有 `CommunityNode` 等摘要层，但 `update_communities` 默认关闭；它不替代 episode 和 fact edge 的来源与有效区间。Graphiti core 的边/节点可由底层 API 修改或删除，但自动冲突路径是保留旧关系并写失效时间，而不是删除旧关系或生成独立的变更审计事件表。

## 6. 写入候选与普通召回不是同一条检索流程

| 项目 | 写入前 episode 上下文 | 实体去重 | 关系去重/冲突 | `Graphiti.search()` 普通召回 |
|---|---|---|---|---|
| 查询内容 | 时间锚点、group、source | 新实体名称 | 新关系 `fact` | 用户提供的 query |
| 目标 | 补充近期对话上下文 | 判断是不是同一实体 | 找可能重复/矛盾的事实 | 召回相关关系事实 |
| 默认检索 | `valid_at <= reference_time` 的最近 episode | 节点名称 cosine top 15、min 0.6 | BM25 + fact cosine，RRF top 10；另有限制相同端点的重复候选 | BM25 + fact cosine，RRF，默认 top 10 |
| 时间处理 | 以 episode `valid_at` 做上界和排序 | 不做实体时间冲突判断 | LLM 标矛盾后，代码比较 fact `valid_at` 写失效区间 | 默认没有时间过滤；可以显式传 `SearchFilters` |
| 返回结果 | episodes 给抽取 prompt | 节点候选给消歧 | 旧边事实文本给冲突 prompt | 关系边对象（含时间字段） |

高级 `search_()` 可以配置 nodes、edges、episodes、communities 的检索方法和 reranker，包括 BM25、cosine、图 BFS、RRF、MMR、node distance、episode mentions、cross-encoder，以及显式的 `valid_at` / `invalid_at` / `expired_at` filters。普通 `search()` 则只跑 edges 的 BM25+cosine RRF。

一个容易误读的细节：`Graphiti.search()` docstring 写着检索以“当前时间”作为 temporal relevance reference，但该公开方法没有 `reference_time` 参数，主搜索实现也没有默认附加有效时间过滤；默认 `SearchFilters()` 是空的。搜索结果带有 `valid_at` / `invalid_at`，由调用方把这些区间交给后续 LLM 判断，或由调用方显式传过滤条件。不能仅凭方法注释就把默认普通召回说成自动只返回当前有效事实。

## 7. 生命周期、批量路径与实现边界

### 值得借鉴

- 将原始 episode 保留为来源对象，关系事实通过 episode UUID 保留 provenance；比只存一段被重写后的 profile 更容易解释“这条记忆从哪里来”。
- 把事件有效时间与系统创建/失效时间分开，并支持迟到历史事实进入图后仍保留正确时间区间。
- 冲突候选不局限于“同一字符串”：实体端点限定的事实去重之外，还用混合文本/向量检索召回其他潜在矛盾边。
- 按实体、关系、自定义 Pydantic 类型组织知识，普通检索可组合向量、全文、结构过滤和图遍历。
- 同一旧事实被重复观察时可将新的 episode UUID 链到同一条关系边，避免只为重复提及再造一条文本边。

### 默认策略的缺口

- 关系抽取要求两个不同实体；独立偏好、用户属性、目标、计划等需要特意建模成实体/边或扩展 schema。它并不是通用的“任意一句对话提炼成一条用户记忆”接口。
- 角色和来源没有可信度/归属决策：assistant 的建议或工具输出可能和用户陈述一样进入当前文本抽取。`source_description` 是文字元数据，不是自动验证的信任级别。
- 冲突 LLM 只看到候选事实文本，不看到候选有效时间、证据来源与置信度；时间由后置代码处理。语义判断错、候选没找全或事实时间不明时，系统没有“冲突待澄清”状态可兜底。
- 关系失效候选默认最多 10 个，搜索分数门槛与排序可能漏掉重要旧事实。该上限是模型决策上下文的边界。
- 默认普通 search 不自动按当前时间排除失效事实；应用应显式使用时间 filters，或把有效区间和来源作为回答上下文。
- 自定义 `edge_types` 能约束边结构，但仍需自行规定计划/事实、主观偏好、确认状态、来源可信度和隐私删除语义。
- Graphiti 要求 graph database、LLM 和 embedding 能力，运行成本比单纯向量存储高。每个 episode 至少涉及节点抽取、边抽取和 embedding；若有相似候选，还会增加消歧/冲突模型调用，若时间字段缺失则可能再做轻量时间抽取。

### 普通与批量写入差异

`add_episode_bulk()` 在同一批 episode 之间有单独的内存去重阶段。该阶段用同端点、词项重叠或 embedding 相似度收候选，但 `bulk_utils.dedupe_edges_bulk()` 明确丢弃这一阶段的 invalidation 结果。之后的 `_resolve_nodes_and_edges_bulk()` 仍会逐 episode 调用 `resolve_extracted_edges()`，因此能与已在图中的旧事实做冲突处理；但同批 episode 互相矛盾时，不应假设有与逐条串行 `add_episode()` 完全一致的失效保证。

官方当前 adding-episodes 文档提醒仅在空图或不需要 edge invalidation 时使用 bulk；所审查的 `0.30.2` 源码确实有对已有图再次 resolve/invalidate 的后续步骤，但批内 invalidation 结果被丢弃。文档与实现描述不完全一致，工程方案应对所选发布包的实际行为做集成验证，而不是只看 quickstart 文案。

## 8. 与首期场景的源码映射

| 场景 | `graphiti-core==0.30.2` 能力判断 | 主要限制 |
|---|---|---|
| S1/S2 稳定偏好、用户事实 | 若抽成两个实体间的关系，可以写成边并连来源 episode | 抽象偏好/单体属性不天然适配双实体边；没有默认用户事实 schema |
| S3 状态变化 | 检索到相关旧边、模型标矛盾且 valid time 可比较时，旧边失效，新旧时间区间保留 | 候选漏召回、时间缺失/相等或 LLM 判断漏掉时不会自动解决 |
| S4 明确纠正 | 新事实可基于更晚的 valid time 让旧边 `invalid_at` 回填 | 没有固定的“用户纠正”类型、来源权重或独立更正审计事件 |
| S5 不确定计划 | 可借助自定义 edge schema 表达计划类别/时间 | 默认关系没有 certainty/status，模型必须从文本中保留“可能”语义 |
| S6 无法判定的冲突 | 图可保留多个关系和 episode provenance | 没有强制 abstain / pending-review 状态；LLM 会返回候选索引而非冲突置信度 |
| S7 相对时间 | 关系抽取按 `reference_time` 解析“去年”等文本 | 参考时间必须可靠；处理时间不等于事实时间 |
| S8 无明确时间 | 当前态可按 episode reference time 作为 valid_at；无法解析的时间允许 null | wrapper 若自动用 now 补 reference time，会把处理时间带入事实时间 |
| S9 assistant 建议 | episode 原文能保留 `assistant:` 标签 | 默认没有阻止 assistant 输出写成用户事实的信任规则 |
| S10 工具结果/用户确认 | 可通过不同 episode source/source_description 记录来源并留原始 episode | 没有内建的 tool result vs user confirmation 结构化区分 |
| S11 临时信息/过期状态 | 明确结束或后续矛盾可以写 `invalid_at`，旧关系留在图中 | 没有默认 TTL/生命周期分类；不会自动知道一次性状态何时结束 |
| S12 当前/历史/来源查询 | edge 有 valid/invalid time、episode provenance；高级 search 支持 filters | 普通 `search()` 没有默认当前时点过滤；要应用侧指定过滤或提示 LLM 看时间 |
| S13 用户要求忘记 | 底层有 CRUD/清理能力可用于显式删图对象 | 不会把自然语言“忘记这个”自动映射为完整、可审计的跨对象删除策略 |
| S14 用户/项目隔离 | `group_id` 是内建的图分区和候选过滤条件 | 应用必须将可信 user/tenant/project 身份映射到 group，并始终一致传入 |

## 9. 对我们设计长期记忆的启示

### 可复用思想

- 保留原始对话/事件 episode，并让候选记忆带有可逆向查找的 episode 引用。
- 采用“有效时间 + 系统记录/失效时间”两类时间，不用入库时间冒充事实时间；允许时间未知。
- 写入去重时用结构约束缩小范围，再组合精确匹配、向量/关键词候选和语义判断。
- 将“语义是否矛盾”与“哪条事实在哪段时间成立”拆成两个可测试的步骤；将失效写回成显式字段而不是悄悄覆盖正文。
- 把用户/项目 namespace 作为所有候选检索与写入路径的硬约束。

### 不应直接照搬

- 不应只用“两个实体之间的边”作为所有长期记忆类型；首期场景需要偏好、计划、个人属性、事件、agent 工作记录等不同类型。
- 不应让冲突模型只看到事实字符串。至少还需向决策层提供有效时间、来源角色/类型、证据引用和置信度，并允许返回冲突未决。
- 不应把时间候选视为完整冲突逻辑。相同时间、未知时间、用户纠正和多来源矛盾需要显式策略；不能静默保留两条“当前”事实而让召回端自行猜测。
- 不应默认以 top-10 检索候选代表某个属性的全部历史。重要实体/属性可先按实体、关系类型、用户范围进行结构过滤，再做相关性搜索。
- 不应把普通语义 search 当成当前状态查询。查询协议应显式支持 `as_of` / 历史时间段与来源追溯。

Graphiti 最值得借鉴的是**episode provenance + 关系图 + 时间有效区间 + 写入时将旧事实失效而保留历史**这一组设计。它不等于已经解决偏好抽取、角色可信度、所有冲突和时间召回；是否把它作为代码基座仍需与记忆类型需求、数据库运营负担和依赖成本一起比较。

## 10. 一手资料索引

- [PyPI `graphiti-core==0.30.2`](https://pypi.org/project/graphiti-core/0.30.2/)：发行包、许可证和依赖信息。
- [GitHub `v0.30.2` release](https://github.com/getzep/graphiti/releases/tag/v0.30.2)：发布记录；本审查固定到该版本的提交。
- [固定提交 `graphiti.py`](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/graphiti.py#L1043-L1278)：`add_episode()`、历史 episode 查询、抽取顺序与持久化；同文件 `#L1586-L1688` 是普通 `search()` / 高级 `search_()`。
- [固定提交 `graph_data_operations.py`](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/utils/maintenance/graph_data_operations.py#L67-L155)：历史 episode 的 `valid_at` 上界、group/source 过滤和排序。
- [固定提交 `node_operations.py`](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/utils/maintenance/node_operations.py#L418-L450)：按名称 embedding 找实体候选；`#L627-L708` 是 exact/fuzzy/LLM 去重路径。
- [固定提交 `dedup_helpers.py`](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/utils/maintenance/dedup_helpers.py#L193-L285)：精确名称、MinHash/LSH 与 Jaccard 阈值。
- [固定提交 `extract_nodes.py`](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/prompts/extract_nodes.py#L83-L170)：消息模式实体抽取、speaker 提取与排除规则。
- [固定提交 `extract_edges.py`](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/prompts/extract_edges.py#L94-L175)：事实关系抽取输入、时间规则与输出字段。
- [固定提交 `edge_operations.py`](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/utils/maintenance/edge_operations.py#L325-L535)：关系候选检索、RRF 调用、实体端点过滤和写回结果；`#L538-L573` 是时间区间失效代码；`#L623-L847` 是 LLM 冲突判定及迟到事实处理。
- [固定提交 `dedupe_edges.py`](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/prompts/dedupe_edges.py#L20-L102)：duplicate/contradiction 判定 prompt。
- [固定提交 `search_config_recipes.py`](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/search/search_config_recipes.py#L110-L116) 和 [`search.py`](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/search/search.py#L270-L374)：BM25、cosine 与 RRF 的候选合并。
- [固定提交 `edges.py`](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/edges.py#L263-L285) 与 [`nodes.py`](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/nodes.py#L318-L355)：episode、entity node、fact edge 字段及 episode 元数据持久化路径。
- [固定提交 `bulk_utils.py`](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/utils/bulk_utils.py#L489-L580) 与 [`graphiti.py` bulk resolve](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/graphiti.py#L1423-L1467)：批内去重与后续对图冲突处理的实现边界。
- [官方 Adding Episodes 文档](https://help.getzep.com/v2/graphiti/core-concepts/adding-episodes)：episode 输入格式及 bulk 操作说明；关于 bulk edge invalidation 的文档表述与本次固定版本源码有差异，详见第 7 节。
