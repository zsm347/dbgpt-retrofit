# LangMem 源码审查：消息输入、旧记忆检索、更新与存储

> 交接说明：本文说明审查版本的原生行为；Store Manager 的默认链路和后半部分扩展建议不是当前首版运行路径。当前使用无状态 manager，并以 [v1 设计与实施说明](agent_long_term_memory_v1_design.md)的 schema、局部提示适配及写回约定为准。

> 审查日期：2026-09-28  
> 发布包锚点：PyPI `langmem==0.0.30`，wheel SHA-256：`142f040014493eebd67e1055c0642f9ab38868b5b1fde5c8f2d39add57f4ba5b`。  
> 源码锚点：`56d85939d80bb731bd5e237567148d817d7bfd16`。我下载并校验了发布 wheel；其中 `extraction.py`、`tools.py`、`utils.py` 与该提交中的同名文件逐字节一致。  
> 范围：LangMem Python OSS 和 LangGraph `BaseStore` 接口。LangMem 的托管服务不在本文结论内。

## 1. 结论摘要

LangMem 不是单一的“自动记忆算法”，而是几种可组合的构件：

- **`create_memory_manager`：** 接收消息和调用方可选传入的旧记忆，调用模型抽取、更新或删除结构化记忆，返回结果；它本身不检索数据库，也不负责持久化。若没有 `existing`，模型就没有旧记忆可供比较。
- **`create_memory_store_manager`：** 带存储的管理器。它先从 LangGraph `BaseStore` 检索最多 `query_limit` 条候选，再把对话和候选一起交给上述管理器，最后按结果新增、覆写或删除存储项。公开工厂的默认 `query_limit=5`、新增开启、更新开启、删除关闭。
- **`create_manage_memory_tool` / `create_search_memory_tool`：** 面向 agent 当前对话的工具路径。agent 自己决定何时调用；更新和删除需要提供已有 memory ID，管理工具不会先按语义自动找出应更新的记录。

因此，LangMem **确实实现了自动记忆更新**，但这不是带时间逻辑的冲突引擎。它主要依赖 LLM 对“本轮消息 + 已检索候选”的语义理解。默认提示要求关注新颖性、可靠性、近因、冗余和矛盾；源码却没有给模型事实有效时间、记录来源引用或候选的 `created_at` / `updated_at` 元数据。候选之外的旧记忆也不会参与本轮判断。

写回时，更新沿用原存储项的 key 并覆写其值；删除会调用 `BaseStore.delete`。管理器没有独立的版本日志或冲突审计表。源码 docstring 所说“maintains a versioned history of all changes”与可执行写回路径不符。

另一个实现细节会影响输入理解：默认 `query_model=None` 时，不是让 LLM 为整段对话生成检索问题，而是从最近消息构造指数扩张的后缀窗口。默认 `query_limit=5` 时窗口数为 `5 // 4 = 1`，所以**旧记忆检索 query 只有传入消息列表的最后一条消息**。如果调用方传入 `[user, assistant]` 一整轮，检索可能只看 assistant 回复；抽取模型仍会看到完整消息列表。

## 2. 版本、模块与调用形态

审查采用 PyPI `0.0.30` 发布包，发布日期为 2025-10-27，许可证为 MIT；本文不承诺它一直是最新发布。审查时通过发布 wheel 的 SHA-256 校验版本，并将关键源码文件与固定提交逐字节比对；源码锚点不是从“latest”文档推断出的浮动版本。[PyPI 发布页](https://pypi.org/project/langmem/0.0.30/)

| 构件 | 检索旧记忆 | 模型输入和输出 | 是否直接持久化 |
|---|---|---|---|
| `create_memory_manager` | 不检索；调用方传 `existing` | `messages`、可选 `existing`、可选 `max_steps` → `ExtractedMemory(id, content)` 列表 | 否 |
| `create_memory_store_manager` | 是；走 `BaseStore.search/asearch` | 原始消息 + 最多 `query_limit` 条已检索记忆 → 对旧条目更新、插入或删除 | 是 |
| `create_manage_memory_tool` | 否；更新/删除要求已知 ID | agent tool call：`create/update/delete`、`content`、`id` | 是 |
| `create_search_memory_tool` | 是；由调用参数提供 query | `query`、`limit`、`offset`、`filter` → 搜索结果 | 否，只返回结果 |
| `create_memory_searcher` | 是；LLM 根据对话生成搜索 tool call | 完整消息 → 一项或多项检索 query → 按分数排序的结果 | 否 |

官方把后台记忆和热路径工具分开描述。`create_memory_store_manager` 是可运行的管理器，但“后台”由应用调用时机或 `ReflectionExecutor` 提供；它不是自动监听对话的常驻服务。官方 quickstart 的基本用法是在生成回复后调用 manager；延迟、合并多轮处理也需要应用通过 `ReflectionExecutor` 配置。[后台 quickstart](https://langchain-ai.github.io/langmem/background_quickstart/)、[延迟处理指南](https://langchain-ai.github.io/langmem/guides/delayed_processing/)

## 3. 输入内容与提取提示

### 3.1 输入是什么

两个 manager 的核心输入都是 `messages: list[AnyMessage]`。LangMem 不会自动限定成“仅用户消息”或“仅一轮”：调用方可以传一条用户消息，也可以传入用户、assistant、工具等多条消息。`utils.get_conversation()` 会合并连续消息段并用 `pretty_repr()` 格式化，保留其角色和内容；提取管理器把传入的消息列表整体格式化到一个 `<session_…>` 块中。

所以：

- 若应用传完整的用户消息和 assistant 回复，提取模型会同时看到两者；默认提示允许它学习关于用户、agent 自身以及“如何行动”的信息。
- 若应用只传用户消息，assistant 内容自然不会进入提取上下文。
- 框架没有默认的事实归属字段或角色过滤规则来保证“assistant 的建议不会成为用户事实”。需要由 prompt、Pydantic schema、应用的输入筛选共同约束。

### 3.2 默认抽取/整理提示词

默认 `_MEMORY_INSTRUCTIONS` 要求模型：

1. 提取事实、关系、偏好、推理程序及情境；不确定结论要给出概率和理由，必要时引用支持内容。
2. 比较新旧信息；关注偏离已有预期的新信息，按可靠性和近因整理冗余内容，移除错误或重复记忆。
3. 对用户、agent 或环境作综合归纳，并说明推断依据。

`MemoryManager._prepare_messages()` 还要求“enrich, prune, organize”，若旧记忆错误或过时则更新，并避免重复抽取。默认 schema 只有 `Memory(content: str)`；调用方可传 Pydantic schema 定义三元组、用户画像、episode、证据字段等。默认提示是决策指引，不是验证规则；除 schema 字段验证外，语义判断仍由模型完成。

### 3.3 单次还是多次模型调用

`max_steps` 默认值是 1：manager 内部给 Trustcall 一次结构化提取/更新调用。一次响应可以包含并行的多个 schema tool calls，因此“一步”仍能产出多条记忆。显式把 `max_steps` 设大于 1 时，代码会把前一步输出和工具结果反馈到下一步继续整理；`Done` 完成工具从第二步起才加入。Trustcall 自身还会为 schema 验证错误执行修正调用，因此真实模型请求数不总等于 `max_steps`。

## 4. `create_memory_store_manager` 的完整写入链路

```text
调用方传入 messages（及配置中的用户/租户 namespace）
  → 确定 namespace
  → 为本轮对话构造检索 query（默认后缀窗口，或 query_model 生成）
  → BaseStore.search/asearch，合并候选并按 score 保留前 query_limit 条
  → 把完整 messages + 候选旧记忆交给 create_memory_manager / Trustcall
  → LLM 通过结构化工具调用新增、patch/update，或（若启用）RemoveDoc
  → 将原有 key 对应的修改覆写，将新条目写入新 key，将 RemoveDoc 映射为 delete
```

`create_memory_store_manager` 只在入口检索一次。可配置 `phases` 做额外整理轮次；每个 phase 默认不重新传原始 messages（`include_messages=False`），而是基于前一阶段整理后的旧记忆继续处理。若设置 `include_messages=True` 才把本轮消息再交给该 phase。

### 4.1 无 `query_model`：默认检索 query 不是整个对话

源码 `get_dialated_windows(messages, N)` 从列表尾部生成大小为 `1, 2, 4, 8, …` 条消息的后缀窗口，再把每个窗口格式化成对话文本。Store Manager 设置 `N = query_limit // 4`，每个窗口单独调用 `store.asearch(namespace, query=...)`；最后按相似度分数去重并保留前 `query_limit` 条。[`utils.py`](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/utils.py#L91-L111)、[`extraction.py`](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/extraction.py#L991-L1039)

例如 `query_limit=5` 时 `N=1`，只产生最后一条消息的 query。`query_limit=10` 时最多生成最后 1 条和最后 2 条消息两个 query。这个参数同时控制候选数量和检索窗口数，语义并不单一；`query_limit < 4` 时 `N=0`，这段默认逻辑不会产生检索 query。

这只是**检索 query 的输入范围**。之后送入记忆管理器的仍是调用方传入的完整 `messages`，不是被窗口截断的消息。因此“抽取看完整对话”和“检索旧记忆时只看最后一条”可以同时发生。

### 4.2 配置 `query_model`：LLM 生成多个语义检索 query

若调用方设置 `query_model`，该模型看到 `get_conversation(messages)` 格式化后的完整对话；系统提示它并行搜索“与该对话相关的不同记忆”，search-tool 指令要求把 query 写成“可能与当前对话相关的假设记忆”。模型可生成一个或多个带 query / filter 等参数的搜索 tool calls，Store Manager 并行调用 `store.asearch`，每次最多取 `query_limit`，再跨 query 去重、按 `score` 排序并裁成最终的 `query_limit` 条。[源码构造 query model 与 search tool](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/extraction.py#L880-L895)、[检索及合并](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/extraction.py#L1015-L1039)

这与无 `query_model` 的后缀窗口策略不同：前者增加一次 LLM query-generation 步骤，后者是代码构造固定窗口文本。二者最终都通过 `BaseStore.search/asearch` 找候选。

### 4.3 是不是向量检索或混合检索

LangMem 管理器没有自己实现向量索引、BM25、关键词候选合并或 reranker；它调用 LangGraph `BaseStore.search/asearch`。`BaseStore` 将 `query` 定义为自然语言语义查询，但是否做向量检索取决于具体 Store 是否支持、是否配置 embeddings/index，以及索引哪些字段。可附加精确 `filter`；LangMem manager 本身没有把 BM25 与向量分数组合的混合检索逻辑。[LangGraph BaseStore 接口](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint/langgraph/store/base/__init__.py)

因此准确说法是：**LangMem 的写入冲突候选检索复用 BaseStore 的搜索接口；常见带向量 index 的存储实现会做语义向量检索，LangMem 本身不保证所有存储都使用向量，也没有内建 BM25 混合排序。**

## 5. 更新、删除和冲突依据

### 5.1 模型如何更新已检索记忆

Store Manager 把检索到的记录以 `(stable_id, kind, content)` 形式传入 `MemoryManager`；`stable_id` 由 namespace 与 store key 生成。Trustcall 收到 schema、对话和这些带 ID 的已有 JSON 文档，支持对现有文档做 JSON Patch、验证结果并按 schema 修正。更新沿用原 document ID；新插入的内容获得新 ID；删除以 `RemoveDoc(json_doc_id=...)` 表示。[LangMem Store Manager](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/extraction.py#L936-L1004)、[Trustcall 更新机制](https://github.com/hinthornw/trustcall/blob/main/README.md#updating-schemas)

`create_memory_manager` 的默认开关是 `enable_inserts=True`、`enable_updates=True`、`enable_deletes=False`。公开工厂 `create_memory_store_manager` 暴露新增和删除开关，但没有暴露 `enable_updates`；它创建内部 manager 时未覆盖该参数，所以更新默认开启。公开工厂默认删除关闭。需要删除时必须显式启用 `enable_deletes=True`，模型才可产出 `RemoveDoc`，Store Manager 随后物理删除候选 store key。

这不是 Mem0 旧版那样明确返回统一 `ADD/UPDATE/DELETE/NONE` 枚举的 API。LangMem/Trustcall 的表达方式是新增或 patch 某 schema 文档，或在允许时移除文档；其底层更新可靠性来自结构化 patch 和校验，不等于有了确定的事实身份匹配或冲突判别算法。

### 5.2 冲突判断具体基于什么

默认指令只给出语言层面的准则：比较新旧内容、关注新信息、可靠性、近因、冗余与矛盾；Trustcall 让模型以已有对象为基础生成 JSON Patch。模型主要依据：

1. 完整的本轮对话文本与角色顺序；
2. 检索返回的有限旧记忆内容和 schema；
3. 默认提示词或调用方提供的 `instructions`；
4. 若自定义了 schema，则模型还需按字段组织和验证结果。

没有一个独立于模型的冲突打分器、实体唯一性约束、来源可信度排序或“新值必胜”的规则。**默认处理依据主要是语义理解和 prompt 约束。** 如果用户陈述与 assistant 推断冲突，除非 schema 和 prompt 明确归属规则，框架不会自动证明哪一个应作为用户事实。

### 5.3 时间是否参与冲突判断

默认输入 schema 没有 `reference_time`、`valid_at`、`invalid_at` 或来源时间字段；Store Manager 给内部管理器的候选也不包含 `BaseStore` 项目的 `created_at`、`updated_at` 或相似度分数。因此，默认模型看不到结构化的事实生效时间，也不会拿记录时间排序判断哪项现实事实更新。

提示词虽然说根据可靠性和近因强化记忆，但 manager 不把 store 的写入时间作为字段交给冲突模型。它能利用的时间线索主要是消息文本中明确写出的日期和当前消息排列顺序。调用方可以通过自定义 schema/instructions 把事件时间、来源和置信信息抽出并保存；那是可定制方案，不是默认时间冲突算法。

## 6. 写入数据、历史和工具路径

### 6.1 `create_memory_store_manager` 的典型 Store 项

默认无结构化字段的记录大致是：

```json
{
  "namespace": ["memories", "user-123"],
  "key": "<stable memory id>",
  "value": {
    "kind": "Memory",
    "content": {
      "content": "User prefers concise answers."
    }
  },
  "created_at": "<BaseStore timestamp>",
  "updated_at": "<BaseStore timestamp>"
}
```

自定义 Pydantic schema 会替换 `kind` 名称和 `content` 对象结构。namespace 用来隔离用户、团队或领域；key 是文档 ID。`created_at`、`updated_at` 是 Store 元数据，不等同于事实的有效时间。默认 `Memory` 内容对象没有 source message ID、evidence span、speaker、confidence 或 valid-time 字段。

更新时对已有 key 调用 `store.aput`，不是 append-only。删除时对 key 调用 `store.adelete`。LangMem manager 没有把旧值写入专门 history 表、没有 change event schema、没有软失效区间。可能存在的 backend 自身审计能力不属于 LangMem 算法保证。

### 6.2 Hot-path memory tool 与 background store manager 不应假定格式兼容

`create_manage_memory_tool` 的 create/update 路径保存 `{ "content": ... }`，删除是按 ID 直接删；它不保存 Store Manager 所需的 `kind` 包装字段。background manager 读到候选后直接取 `item.value["kind"]` 与 `item.value["content"]`。因此若两种工具共享 namespace，hot-path 写入的项被背景 manager 搜到时可能因缺少 `kind` 而失败。这是两个公开写入路径的数据契约差异，需要在复用时自行适配或统一 schema。[hot-path 写入源码](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/tools.py#L249-L303)、[background manager 读取源码](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/extraction.py#L1065-L1084)

## 7. 写入时检索与普通召回的区别

| 项目 | `create_memory_store_manager` 写入前候选检索 | `create_memory_store_manager.search()` | `create_search_memory_tool` / `create_memory_searcher` |
|---|---|---|---|
| 用途 | 给抽取模型提供可能需要更新/合并的旧记录 | 应用主动按 query 查长期记忆 | agent 工具搜索，或先让 LLM 从对话生成查询再搜 |
| query 来源 | 默认最近消息后缀窗口；或可选 `query_model` 生成多 query | 调用方显式传入 query/filter | tool call 的 query；`create_memory_searcher` 会用 LLM 生成 tool call |
| 最终候选数 | 默认 top 5 | `BaseStore.search` 的 limit 参数决定 | 工具默认 limit 10，可配置 |
| 搜索引擎 | `BaseStore.search/asearch` | 同一个 BaseStore 接口 | 同一个 BaseStore 接口 |
| 自动冲突更新 | 后续交给 MemoryManager + Trustcall 决策 | 不会自动更新，只返回搜索结果 | 不会自动更新，只返回搜索结果 |

所以写入阶段和普通召回**共用存储搜索底层，但不是同一条完整 pipeline**：默认写入阶段有自己的 query 构造和 5 条候选限制；应用普通 search 可传入任意 query、过滤器和数量；`create_memory_searcher` 又加了一层 LLM query generation。没有证据表明 LangMem 默认搜索自动使用 Mem0 式 BM25/实体混合分数。

## 8. 默认、可配置与需要调用方设计的边界

| 能力 | OSS 默认提供 | 调用方需要决定/实现 |
|---|---|---|
| 消息抽取 | 默认 prompt、单字符串 `Memory` schema | 哪些角色可信、哪些信息值得长期保存、对用户/agent/工具信息的归属规则 |
| 旧记忆候选 | Store Manager 自动检索，默认前 5 条 | Store 向量 index、隔离 namespace、candidate limit、`query_model` 和查询过滤策略 |
| 更新与去重 | Trustcall 可 patch 已检索文档；prompt 要求避免重复和整理冗余 | 如何定义同一事实、状态演变、补充、纠错和无法解决的冲突 |
| 删除 | 能以 `RemoveDoc` 删除 | 是否启用删除、删除后审计/恢复/保留策略 |
| 时间、来源、置信度 | Store 有记录时间；模型可以读消息内显式时间 | schema、抽取规则、事件时间与系统时间区分、来源引用和置信策略 |
| 历史保留 | BaseStore 当前项的创建/更新时间 | 版本表、证据保留、软失效、可回滚变更和审计事件 |
| 运行时机 | Manager Runnable、ReflectionExecutor 工具 | 什么时候触发、延迟多久、如何合并多轮、失败重试和任务持久化 |
| 记忆检索 | 通过 BaseStore API | 选用何种 Store/embedding、关键词与结构过滤如何融合、召回结果如何提供给 agent |

这也解释了“LangMem 支持可定制记忆更新策略”的准确含义：**框架提供了管理器、可控操作开关、schema、prompt、检索器和 Store 接口；业务上什么算纠正、谁的陈述更可信、状态如何保留历史，需要调用方通过 schema/prompt/额外代码定义。**

## 9. 与首期场景的源码映射

| 场景 | 源码层面可以做到什么 | 默认行为缺口 |
|---|---|---|
| S1/S2 稳定偏好、个人事实 | 能从消息抽取为 Memory 或自定义 schema；可对候选旧记录做更新 | 默认无证据 message ID；新旧候选漏召回则无法去重；不自动验证说话者归属 |
| S3 状态变化 | 检索到旧值且模型认为应更新时，可 patch 原记录 | 更新覆盖原条目，不保留历史状态/有效区间；无时间逻辑 |
| S4 明确纠正 | prompt 可令模型 patch 旧记录；若开启删除可移除旧项并插入新项 | 更正关系和原错误证据默认没有单独保存；删除默认关闭，覆盖也无版本日志 |
| S5 不确定计划 | 自定义 schema 和 prompt 可以保存状态/置信度 | 默认 schema 只是一段 content，未结构化区分计划和事实 |
| S6 冲突未决 | 可在 prompt 要求并存/询问/保留不确定文本 | 默认更偏向“整理一致”，没有强制待核实冲突状态，模型可能武断更新 |
| S7/S8 时间事件/无时间 | 文本中有日期时模型可提取；自定义 schema 可定义时间字段 | 没有 reference time 参数，也不自动区分 event time 与 record time；未知时间没有默认表达 |
| S9/S10 assistant 和工具来源 | AnyMessage 角色可被输入模型看到；schema 可含来源类型 | 默认不强制区分 assistant 建议、工具观察、用户确认 |
| S11 临时信息 | 自定义 schema 可保存期限；BaseStore TTL 取决于 backend | 不会自动识别临时状态已结束；Store TTL 是条目寿命，不是事实有效区间 |
| S12 当前、历史和来源查询 | BaseStore 可语义 query/filter；自定义 schema 能留字段 | 无默认时间范围或证据链召回；当前查询和历史查询由应用组织 |
| S13 删除/忘记 | `enable_deletes=True` 时 manager 可物理删除；hot-path 工具可按 ID 删除 | 删除默认关闭；无跨副本策略、删除意图日志或可审计保留方案 |
| S14 用户隔离 | namespace template 可绑定 user/team ID | 必须由应用正确传身份配置；没有默认租户模型或授权策略 |

## 10. 审查发现及复用启示

### 值得借鉴

- 把抽取核心与持久化层分开，先由无状态 manager 产出可审查结果，再用 Store Manager 自动落库；这为独立测试/替换冲突策略留下接口边界。
- 使用可定制 Pydantic schema 约束记忆形状；用 Trustcall 的 patch 和验证降低整份 JSON 重生成导致字段丢失的风险。
- 在写入之前找有限的相关旧记忆并作为上下文给模型，比完全无上下文地追加更有机会做合并/更新。
- 将 query generation 与底层 Store search 分层，可插入 query model、scope、filter 和不同 BaseStore adapter。
- 通过 namespace 模板表达 user/team/domain 的隔离层级。

### 不应直接照搬

- Top-5 候选是模型可处理上下文的上限，也是更新召回的硬边界；重要旧条目没被搜到时，LLM 并不知道它存在。扩大数量只降低漏召回概率，不会建立事实身份或冲突规则。
- 默认检索只用最后一条消息 query（默认 `query_limit=5`），当传入完整 user/assistant 轮次时可能偏向 assistant 回复；自研时应把查询输入构造做成明确策略，并记录它用的是哪些消息。
- “按 recency 加权”的 prompt 与结构化时间数据之间存在落差；不要把 Store `created_at` 当事实时间，也不要宣称默认系统按时间冲突处理。
- patch 覆写对 profile 很实用，但不适合作为唯一历史模型；我们的首期场景要求能够还原旧状态、证明更正来源，应该另有不可变来源证据和变更记录。
- 热路径和背景路径的存储格式不一致，证明接口组合不自动意味着数据契约统一。若将 LangMem 作为实现基础，应先统一 Memory envelope / schema，并补上来源和时间字段。

## 11. 主要一手资料

- [PyPI `langmem==0.0.30` 发布页、发布日期和 wheel 文件](https://pypi.org/project/langmem/0.0.30/)
- [固定提交 `extraction.py`](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/extraction.py)：`MemoryManager`、默认提示词、Store Manager 搜索、patch/update/delete 和写回。
- [固定提交 `utils.py`](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/utils.py)：消息文本化及 `get_dialated_windows()` 后缀窗口 query。
- [固定提交 `tools.py`](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/tools.py)：hot-path manage/search tool 的 API 和 Store 数据格式。
- [官方 Semantic Memory 指南](https://langchain-ai.github.io/langmem/guides/extract_semantic_memories/)：无状态 manager 与 Store Manager 的定位、schema 例子和更新示例。
- [官方 Background Quickstart](https://langchain-ai.github.io/langmem/background_quickstart/) 与 [Delayed Processing](https://langchain-ai.github.io/langmem/guides/delayed_processing/)：触发时机、后台调度和多轮 debounce。
- [Trustcall README：更新 schema 与 patch 验证](https://github.com/hinthornw/trustcall/blob/main/README.md#updating-schemas)
- [LangGraph `BaseStore` 接口](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint/langgraph/store/base/__init__.py)：语义 query、index 和 backend 能力边界。
