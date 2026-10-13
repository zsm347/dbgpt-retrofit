# Agent 长期记忆 v1：设计与实施说明

> 状态：第一批独立写入原型和第二批最小替代写回/普通召回/遗忘入口已实现，并通过编译、导入、配置、fake/stub 接线检查及第一轮真实模型调用；整体记忆效果尚未验证。  
> 日期：2026-09-29；最近决策更新：2026-10-09。  
> 相关材料：[Mem0、LangMem、Graphiti 对照](agent_long_term_memory_comparison.md)、[组件探针记录](agent_long_term_memory_prototype_eval.md)。

阅读顺序：前半部分说明目标和取舍；后半部分给出接口、schema、提示词、Mem0 调用和写回过程。第一批和第二批实际代码见 `examples/agent_memory_v1/`；实施说明 17 对应当前最小实现。历史对照中的大对象模型、多路检索和完整状态机仍属候选。

**交接入口：** 接手者先读实施说明 16、17，尤其 17.8 的失败修正与残留问题，再按实施说明 2–13 理解代码；实际提示词以 `examples/agent_memory_v1/memory_v1/prompts.py` 为准。实施说明 15 是 2026-10-08 的历史审核记录。本文是当前实施约定，其他文档中的早期候选字段和流程不能覆盖它。

## 目标

记住对未来 agent 任务有用的信息；保留它来自哪里、何时被说出或观察到；需要时找出来交给 agent。评估关注记忆形成和使用是否帮助多类后续任务，历史问答只是一个场景。

长期记忆保存可复用的用户信息和持续任务上下文，例如明确偏好、用户提供的事实、约束、决定和进展。它不等于整段聊天记录、隐藏推理或 agent 的每一步执行日志。

## 复用优先的实施方向（2026-10-08）

**已确认的工作原则：能直接复用的开源实现优先直接复用；能参考的实现优先按固定源码和示例适配。新增设计限定在来源、时间和保留旧记录等现有需求的缺口。**

当前实施起点是 Mem0 存取/检索 + LangMem 无状态 manager + 薄适配层；第一批组合依赖及本地初始化已经核对，真实模型和整体记忆效果尚未验证，不据此宣称它是最终最优基座。

| 部分 | 复用或适配方式 |
|---|---|
| 存取和检索 | 优先复用 Mem0 公开 `search()`、`add(infer=False)`、`get()`、`update()`、`delete()` 和 metadata；不重新实现向量库适配。 |
| 提取和整理 | 复用 LangMem `create_memory_manager` 的结构化新增、patch 和 schema 校验；候选由调用方传入。 |
| 写回比较 | 参考 LangMem Store Manager 按 ID 与内容比较、跳过未变化对象的实现，适配为 Mem0 的存取调用。 |
| 提示词和上下文 | 提取部分参考 Mem0 V3 的新消息/近期上下文/旧记忆分区、来源归属及时间参照；重复与补充参考旧 Mem0 的 NONE/UPDATE 示例；输出方式遵循 LangMem schema 工具调用。 |
| 必要的补充 | 输入中明确展示来源 ID、角色和消息时间；校验引用与范围；变化或纠正保留旧记录；用户遗忘处理；提示模板的小范围调整。 |

六种新旧语义关系不成为六种写入操作，也不要求必填六值 `relation`。底层只需跳过、新增、更新；用户要求忘记走独立删除。变化与明确纠正都可新增并保留旧记录；正文保持自包含，必要的事实限定与纠正原意保留，但不写“本条替代记忆 ID”等管理说明。第二批用程序维护的 `superseded_by` 明确替代关系，不重复存储通用关联数组和独立 `superseded` 状态。

以下接法仍需在实现中核对：

- Mem0 V3 内部写入检索是整段新消息向量 Top-10；公开 `search()` 是另一条搜索管线。首版优先接公开 API，不能把两者当作相同实现；复用现成搜索不要求再自建多路混合检索。
- 最近 10 条是消息数，不是对话轮数；单独复用 `search()` 或 `add(infer=False)` 不会运行自动提取中的近期消息准备。近期上下文从聊天记录提供，并与本次新消息明确区分。
- LangMem 会返回未改动的旧对象；写回必须比较输入与输出。其提示模板还会追加“旧信息错误或过时则更新”的通用指令，需要与变化/纠正新增保留旧记录的约定协调。
- Mem0 的 `history()` 可用于操作追踪，但不等于旧版本自动进入普通召回。`add(infer=False)` 也不自动获得 V3 的实体关联流程。

实现依据见 [Mem0 审查](agent_long_term_memory_mem0_audit.md)、[LangMem 审查](agent_long_term_memory_langmem_audit.md)。提示词仍是待运行验证的适配草案，不能因参考了开源项目就视为效果已验证。

## v1 最小闭环

```text
带有可信范围、说话者和可用消息时间的输入
  → 准备本次新消息与近期上下文
  → 在相同范围内查找相关旧记忆
  → LangMem manager 结合新消息与旧候选提取、整理
  → 比较输入输出，判断跳过、新增或更新
  → 保存记忆和来源

后续任务
  → 先按可信范围限制候选
  → 用任务问题检索相关记忆
  → 将记忆内容、来源角色和可用时间交给 agent
```

写入时检索用于比较新旧信息；回答时检索用于给当前任务找依据。两者可以复用同一个搜索后端，但目的和查询内容不同。搜索分数只帮助找候选，不证明记忆内容为真。

## 首版必需

| 能力 | v1 要求 |
|---|---|
| 范围隔离 | 每条记忆归属由程序提供的用户或项目范围；每次读写都必须按范围过滤。 |
| 记忆内容 | 每条记录表达一项可独立使用的信息，内容简短、可理解，保留“计划”“可能”“已更正”等原意。 |
| 来源归属 | 记录来源引用和说话者/来源类型；不能把 assistant 建议或工具观察写成用户确认。 |
| 消息时间 | 有原始消息时间就保存为观察时间；系统另记保存时间。二者不能冒充事实发生时间。 |
| 基本整理 | 识别不写、重复、补充、状态变化和明确更正；缺少依据时不把推断写成确定事实。 |
| 基本检索 | 复用现有搜索能力，先做硬范围过滤，再按任务查相关记忆；向 agent 提供命中内容及可用的来源/时间。 |
| 用户要求忘记 | 删除被要求忘记的记忆，使其不再从记忆检索中出现。原始聊天、备份等外部副本如何删除，另按产品隐私策略决定。 |

## 一条记忆需要哪些信息

这是逻辑信息要求，不预先限定关系型表、JSON 或向量库的物理结构。

| 信息 | 首版 | 用途 |
|---|---|---|
| `id`、`scope_type`、`scope_id` | 必需 | 标识记录和所属范围；范围类型/ID 由程序提供，用于区分用户与项目记忆。 |
| `content` | 必需 | 保存独立、可复用的记忆表述；主体、必要时间及限定条件完整，不依赖另一条正文才能理解。 |
| `source_refs` | 必需 | 指向来源消息/文档/工具记录，并保留说话者或来源类型；输入没有 ID 时由程序生成稳定 ID。 |
| `observed_at` | 有来源时间时保存 | 表示来源消息或观察发生的时间；不等于事实的起始时间。 |
| `recorded_at` | 必需 | 系统首次保存这条记忆的时间；更新时保留，不能代替来源时间或事实时间。 |
| `valid_from` / `valid_to` | 可选 | 只有来源明确给出或能可靠换算时才记录事实有效时间；未知就留空。 |
| `superseded_by` | 第二批，可空 | 程序保存的替代目标 Mem0 ID。仅明确已发生的状态替代或明确纠正时设置；时间未知不妨碍记录已经确认的替代。 |

首版不要求 `kind`、`topic`、必填六值 `relation`、数值置信度或完整状态机。不单独持久化 `status="superseded"`；可从非空 `superseded_by` 得知已被替代。没有对应替代记录时该字段为空，不能因为记忆较旧就标为替代。第一批仍使用 `related_memory_ids`，第二批的迁移和模型临时输出约定见实施说明 17。

## 写入整理规则

> **已讨论确认的 v1 写入约定：**重复默认不新增、不改内容，有值得保留的新来源/观察时间时可追加元数据；补充信息更新记忆并保留新来源；状态变化保留新旧记录及各自消息观察时间；明确更正后的内容用于当前回答，旧内容仅作为先前说法追溯，不能当作过去成立的事实；原因不明的矛盾不按时间新旧自动裁决。以下规则不要求首版引入复杂冲突状态机。

| 新信息与旧记忆的关系 | 首版处理 |
|---|---|
| 不值得未来使用，或只是无依据推断 | 不写为确定记忆。 |
| 重复表达同一信息 | 不新增、不改内容；有值得保留的新来源或观察时间时可更新来源元数据。 |
| 新信息补充旧信息且不矛盾 | 更新记忆内容，并保留新旧来源。 |
| 明确报告状态变化 | 新增自包含的新记录，保留旧正文、旧来源和各自消息时间；程序在旧记录上保存 `superseded_by`。有可靠事实切换日期时允许补充旧 `valid_to`；不能用消息时间编造精确结束日期。 |
| 用户明确纠正旧信息 | 新增自包含的更正记录，保留必要的更正限定；程序链接被明确纠正的旧 ID。旧内容可追溯先前说法，不能视为过去真实成立；不能通过填一个结束日期把错误陈述改造成历史事实。 |
| 信息不一致但原因不明 | 保留两边的来源，不只凭时间新旧自动选胜者；检索到时把冲突依据交给 agent。 |

普通记忆整理不使用“删除旧事实”的操作。用户要求忘记属于独立的删除请求，按上面的遗忘要求处理。首版不建设人工审核队列；遇到证据不足时，agent 应说明不确定性。

## 时间怎么参与判断

- 实时对话有消息时间戳时，记录该时间作为 `observed_at`。它表示系统何时收到/观察到这条消息，不一定等于事实开始生效的时间。
- 导入旧对话时优先用原消息时间；没有原消息时间就留空，不能用导入时间伪装成事情发生时间。
- 只有明确日期或可靠参照能支持时，才填写 `valid_from` / `valid_to`。例如用户说“现在在上海”，可以记录这条位置报告的消息时间；不能据此推断用户何时到上海或一直住在上海。
- 第二批允许在已确认同一状态被替代且切换日期有依据时，给旧记录补充 `valid_to`，并给新记录提取 `valid_from`。日期未知时保留替代关系和各自消息时间，事实时间仍可为空。
- 当前时间、消息时间和事实有效时间含义不同；检索时把可用时间交给 agent，供它结合问题和来源判断，不声称时间戳本身解决了所有冲突。

## 召回怎么做

> **v1 召回原则：**先按可信范围检索；旧记录不预先排除。把每条命中的正文、来源、消息时间、事实时间和 `superseded_by` 交给 agent。已被替代的旧记录不能独立证明当前状态，但仍可用于适当的历史问题；已被纠正的旧说法不能作为过去事实。agent 根据任务使用这些依据，不全局偏向最新记录。首版不做通用查询意图分类器，也不为每种问法编写专门规则。

首版优先复用 Mem0 的同范围搜索 API，底层可以包含其现成的语义及关键词评分，无需先自建检索融合。消息观察时间表示“这条信息何时被说出或观察到”，不是事实必然从何时生效；agent 只有在任务需要时间判断时才把它作为时间依据。搜索可能漏掉候选，v1 不承诺完整时间线或审计查询。

## 首版暂缓

- 必须使用知识图谱；为 Episode、Evidence、Change Event 等拆多张表。
- `kind`/`topic`/`status` 分类体系，复杂冲突状态机，自动置信度或自动胜者排序。
- 无证据推断事实有效区间、按消息/入库时间机械失效、TTL 自动过期和时间衰减。第二批的有依据结束日期写回不在此暂缓项内。
- 完整 metadata 历史快照、独立版本链与恢复系统。正文变更追踪先复用 Mem0 `history(ID)`；状态变化保留独立新旧记录。
- 自建通用混合检索融合、额外 reranker、图遍历、完整历史时间线和专用查询意图分类器；可直接复用后端已有搜索能力。
- 多模态记忆、共享组织记忆、复制整段聊天或隐藏推理。
- 把 Mem0 或 LangMem 的一次组件探针结果当作长期记忆整体效果的结论。

这些能力只有在实现最小闭环后出现明确问题，才逐项评估是否加入。

## 原型边界的暂定默认值

| 决策 | 暂定默认值 | 原因/待核实处 |
|---|---|---|
| 记忆范围 | 覆盖用户长期信息和持续任务/项目上下文；不保存完整执行日志或隐藏推理。 | 这覆盖个性化和后续任务连续性；执行记录是否值得记忆由其可复用价值决定。 |
| 搜索后端 | 第一批采用 Mem0 的 Qdrant 本地 provider；通过薄封装连接 LangMem。 | 共同依赖、本地初始化和第一轮真实模型/embedding 调用已检查，生产后端和整体效果未最终确定。 |
| 来源和遗忘 | 记忆保存来源 ID、角色和可用消息时间，不复制整段原文；用户要求忘记时删除确认范围内的记忆记录。 | 来源 ID 不自动保证原文可读；原始聊天、备份等副本的级联删除属于独立隐私策略，不在记忆原型中承诺。 |

这些是推进原型的默认假设，不是最终产品策略；若实现中发现与真实数据/权限模型冲突，再调整。

## 下一步

两批实现和 DB-GPT 默认关闭的可选桥接已经完成。当前下一步是阶段 6 的正式记忆评测：用多类信息检查记忆形成、整理、召回和对后续任务的帮助，并记录失败案例。已经按用户决定暂缓的“无明确日期时间处理、明确日期与历史状态组合、assistant/tool 来源归属”不重新展开；评测先使用已有场景和已完成的真实链路。评测发现明确问题后再修改实现，生产部署准备后置。

## 实施说明 1：哪些已确定，哪些仍待验证

| 状态 | 内容 |
|---|---|
| 已讨论确认 | 开源复用优先；用户/项目范围由程序决定；保留来源角色和消息时间；补充可更新；变化/纠正保留旧记录；旧记录可参与召回；用户遗忘走独立删除。 |
| 首版实施约定 | Mem0 负责存取/检索，LangMem 无状态 manager 负责提取/整理；以 ID 和对象差异驱动写回；模型 schema 和提示词见下文；优先使用公开存取 API。 |
| 工程默认值 | 从已审查版本起步，独立环境先用现成后端；近期上下文 10 条消息、写入候选 10 条、普通召回 10 条、manager `max_steps=1`。这些数值仅是可配置起点。 |
| 已做接线检查 | 第一批固定依赖、配置解析、本地 Qdrant 初始化、编译/导入和无外部模型的写回检查。 |
| 尚未实现或验证 | 真实模型提取/更新选择、完整 metadata 存取覆盖、检索覆盖、端到端效果和成本；跨进程恢复和生产事务也未由原型提供。 |
| 后续接入决定 | DB-GPT 中的触发位置、配置归属、部署后端与生产并发/重试方式。当前独立核心不依赖这些决定。 |

业务上已足够开始实现。不能将“约定清楚”写成“效果正确”“组合运行通过”或“改造量很小”。工程默认值可以根据实际适配修改，但若涉及补充/变化/更正、来源或时间语义变化，应先说明原因并更新本文。

## 实施说明 2：模块职责与入口

首版保留四类职责，代码落点在实施时依仓库规范确定；职责划分不要求建立多个独立服务。

| 职责 | 做什么 | 主要复用 |
|---|---|---|
| 输入与数据对象 | 校验 scope、新消息、来源 ID 与时间；区分新消息和近期上下文。 | 现有消息对象和 Pydantic。 |
| 提取/整理适配 | 构造模型可见的输入；调用 LangMem manager；处理固定提示模板的冲突。 | LangMem MemoryManager/Trustcall。 |
| 存取与写回适配 | 搜索候选、比较原对象与结果、校验引用，调用新增/更新/删除。 | Mem0 API；参考 LangMem Store Manager 的差异比较。 |
| 对外记忆入口 | 编排 add/search/get/forget，返回明确结果与失败信息。 | 上述组件，不再另建冲突判定模型或查询意图分类器。 |

逻辑接口如下，参数属于我们的薄封装，并非声称 Mem0 或 LangMem 原样提供这些签名：

```python
await memory.add(
    scope=scope,
    new_messages=new_messages,
    recent_messages=recent_messages,
    batch_id=batch_id,
)
await memory.search(scope=scope, query=query, top_k=10)
await memory.get(scope=scope, memory_id=memory_id)
await memory.forget(scope=scope, memory_ids=memory_ids)
```

- `add` 返回新增/更新后的实际记忆 ID、跳过数量，以及本次处理的候选和写回摘要。没有值得保存的信息是成功的空写入，不是模型/存储失败。
- `search` 返回结构化记忆上下文；记忆核心不自行生成用户答案。应用或 agent 将结果交给回答模型。
- `get` 按实际 ID 获取并校验 scope，可供 agent 获取命中的替代目标；未知或已删除目标应明确返回缺失，不当作仍存在。
- `forget` 是程序/agent 内部的按 ID 删除接口，用户不需要提供 ID。用户提出自然语言请求，agent 先检索并确定对象，程序再校验范围后执行；不能只删一次 Top-k 命中就宣称已忘记某主题的全部内容。

首版异步封装优先使用 Mem0 `AsyncMemory` 和 LangMem `ainvoke()`。下文 Mem0 不带 `await` 的片段用于说明公开 API 参数，按同步 `Memory` 展示；真正异步实现应使用对应的 `await mem0.search/add/get/update/delete(...)`，不能直接在事件循环中调用同步客户端。两种 SDK 的参数与行为仍应以安装的固定版本核对。

## 实施说明 3：输入、范围与来源

### 3.1 输入对象

```python
class MemoryScope(BaseModel):
    scope_type: Literal["user", "project"]
    scope_id: str

class SourceMessage(BaseModel):
    id: str
    role: Literal["user", "assistant", "tool", "system"]
    content: str
    observed_at: datetime | None = None
```

`id`、`role`、`observed_at` 都由调用程序提供或维护。输入来自其他文档/工具事件时，可先由程序适配成带可靠来源引用的输入；首版优先实现对话消息，不先扩展多模态和文档导入管线。

- `new_messages` 是本次待处理的一轮或一段消息。首版普通对话输入建议使用用户消息和 assistant 回复，不要求每条用户消息单独提取。schema 中的 tool 角色表示可预留的应用输入扩展：只有调用方显式提供工具返回结果时才存在，不能据此声称 Mem0 普通自动提取默认纳入工具消息。固定 Mem0 2.2.1 的 parse_messages 处理 system/user/assistant，未处理 tool；若后续纳入工具结果，需要明确输入和检索格式适配。
- `recent_messages` 是之前的对话，默认最多 10 条，用于解释指代、省略和已有任务上下文；它不触发重新提取。
- 已在 `new_messages` 中的消息不得再次放入 `recent_messages`。近期上下文由聊天记录提供，不另用 Mem0 自动 add 来保存一份。
- 当前任务重要的系统设定应从应用配置提供，不能把普通 system 模板全文当作用户事实提取。
- 输入本来没有消息 ID 时，由调用封装生成并在该输入重试期间保持一致；批次内重复 ID 应校验角色、内容和时间是否一致，不允许同 ID 指向不同消息。
- 来源 ID 在同一 scope 中应能区分不同对话和批次，例如使用聊天记录的全局 ID，或组合 conversation_id 与 message_id。不能让不同会话中的局部编号 `m1` 指向不同消息却被合并为同一来源；构造目录时，新输入与旧来源的冲突 ID 同样需要拒绝。

模型实际输入必须能看到来源 ID、角色和时间。仅在 LangChain 消息对象上附加 metadata 不够；默认 `get_conversation()` 的格式化不能作为这些字段一定进入提示的保证。

### 3.2 范围映射

薄封装使用单一确定性规则将 scope 映射为 Mem0 的 `user_id` 存储隔离键。首版可用 `user:<scope_id>`、`project:<scope_id>`；这里是容器键，不表示把项目事实归属给某个用户。读写必须使用同一映射，必要时对 ID 做无歧义编码。

- `add()`：scope 键通过顶层 `user_id` 提供。
- `search()` / `get_all()`：通过 `filters={"user_id": scope_key}` 提供。
- `get()` / `update()` / `delete()`：Mem0 接口按 ID 操作；封装必须先读取并核对其 scope，不能仅因为模型返回了 ID 就写入或删除。
- 项目范围的访问授权由调用方负责；模型不能决定是否跨用户/项目查找。
- 首版不自动合并用户记忆和项目记忆。后续任务需要两个范围时，由应用明确分别调用。

### 3.3 来源目录

每次调用构造一个临时来源目录：`source_id -> role / observed_at / 原内容或引用`。它包含本次新消息、近期消息以及旧候选已保存的来源元数据。

这个目录用于让模型理解依据、校验返回 ID、填充最终 `source_refs`。它是本次调用的内存映射，不要求新建 Evidence 表或复制完整聊天原文到记忆库。对旧来源，若只能取得已保存的角色/时间/原始引用而不能取得原文，就明确其可追溯程度，不能生成不存在的证据片段。

来源 ID 能标识出处，但不自动保证原文可读取。原聊天系统已有记录时，调用方应提供可在该系统定位的稳定 ID；封装自行生成的 ID 若没有对应的原文存储，只能用于关联本次输入及已保存的角色/时间。首版不另建原文存储或通用来源解析服务，也不把后者宣称为可回查完整原文的证据。

## 实施说明 4：LangMem 模型 schema

第二批仍复用一个 LangMem schema，不引入 `kind/topic/status` 分类体系。第一批代码的字段是 `related_memory_ids`；第二批实际使用临时的 `supersedes_ids` 请求字段，并兼容读取旧字段：

```python
class MemoryContent(BaseModel):
    content: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    valid_from: str | None = None
    valid_to: str | None = None
    supersedes_ids: list[str] = Field(default_factory=list)
```

| 字段 | 由谁提供 | 含义与约束 |
|---|---|---|
| `content` | 模型整理 | 一项自包含的信息，保留必要主体、时间、计划/建议/不确定性与纠正限定。无需附加“本条替代旧记忆”等管理说明，不往正文写记忆 ID。 |
| `source_ids` | 模型选择，程序校验 | 只能引用输入来源目录中的 ID。新增信息应由本次新消息支持；对已有对象的补充保留原有来源。 |
| `valid_from/to` | 模型提出，封装校验 | 有明确依据才填写；无依据留空。消息时间通过来源保存，不塞进这两个字段。 |
| `supersedes_ids` | 模型提出，程序校验 | 本次新对象明确替代的旧候选 ID，可空。仅是本次写回请求，不作为通用关联数组保存；程序在取得新 Mem0 ID 后，在旧记录 metadata 写 `superseded_by`。 |

记忆对象的 ID 由 LangMem 返回的 `ExtractedMemory.id` 携带，不再让模型在 content 中生成第二套 ID。scope、来源角色/消息时间和入库时间都由程序管理，模型只选择来源 ID。

`supersedes_ids` 只能引用本次候选中的实际旧 Mem0 ID，不引用其他新增草案的临时 ID。旧对象回带时该请求字段为空；已有 `superseded_by` 作为程序维护的候选 metadata 展示，不开放给模型编造/清空。这个请求字段与落库字段方向不同：前者定位已知旧对象，后者保存 ADD 后才知道的新 ID；不是重复持久化同一关系。

第二批在当前 `ExistingMemory` 上增加程序字段 `superseded_by: str | None`，不把它塞进模型可 patch 的 `MemoryContent`。从 Mem0 读取时，`MemoryContent.supersedes_ids` 总是空数组；写入编码器忽略这个临时字段。普通召回把 `ExistingMemory` 展平为实施说明 11 的上下文，保留程序字段。向 manager 提供的 `existing` 仍是原来的三元组，已存替代标记另放入模型可见的记忆 metadata 分区，不能只附在 Python 对象上。

`valid_from/to` 的字符串保留证据已有精度，例如 `2026`、`2026-10`、`2026-10-08` 或带时区的明确时间。不把月份强制换成月初，不把一天强制换成零点。首版不对这些不同精度值做通用时间区间过滤；原来的模糊表达继续保留在内容中。时间格式和引用校验不等于证明模型的事实时间推断正确。

六种关系的必填 `relation` 已放弃；也不要求输出 Mem0 风格的 `event` 字段。普通更新通过已有 ID 的 patch 表达；替代通过新对象及可空 `supersedes_ids` 请求表达。代码映射与迁移约束见实施说明 17。

## 实施说明 5：完整提取/整理 instructions 草案

以下是最初的逻辑指令草案，不是当前代码提示词的逐字副本。它是对固定源码和当前需求的适配，不是开源项目原文。2026-10-09 实际修正的工具选择、来源和保留要求见实施说明 17.8 与 `memory_v1/prompts.py`；少量真实样本已运行，但不能据此宣称普遍效果达标。

```text
你负责从对话中形成对未来对话或任务有用的长期记忆。
使用提供的 MemoryContent schema，通过结构化工具调用形成结果。

输入含四部分：
1. New Messages：本次需要处理的新消息。
2. Recent Context：之前的对话，仅用于理解指代和上下文。
3. Existing Memories：检索到的有限旧候选，用于比较和补充。
4. Source / Memory Metadata：程序提供的来源 ID、角色、可用消息时间，
   以及按旧记忆 ID 对应的已有替代标记。

提取：
- 保留值得未来使用的事实、明确偏好、约束、决定、任务进展
  和可复用的经验；不要求每条消息产生记忆，不设置提取数量配额。
- 一条记忆表达一项自包含的信息，补全必要主体、时间和限定条件。
  不把管理关系写成正文，不附加“本条替代某记忆 ID”。
- 新信息必须由 New Messages 支持。Recent Context 和旧候选
  可以帮助理解，不要仅据其内容重新生成一批已有记忆。
- 用户陈述、assistant 建议/研究结果和工具观察按原来源表达。
  assistant 复述不产生额外用户事实；建议不能写成已经采纳的决定。
- 保留“计划”“可能”“尚未确认”等原意。
- 不把没有依据的推断或猜测写成确定事实，不保存隐藏推理过程。

整理：
- 没有值得保存的新信息，或已有相同信息：不新增记忆，
  不为改写措辞而更新已有内容。
- 需要补上有效的新来源/观察时点时，可以给对应旧对象
  追加 source_ids；不要因此新增一条同义事实。
- 新信息让已有记忆更完整且不矛盾：patch 对应旧对象，
  沿用旧 ID，保留仍成立的信息与原有来源，不同时新增同义概括。
- 新信息明确报告已发生的状态替代：新增自包含的新对象，保留旧正文。
  用 supersedes_ids 指出被替代的旧候选 ID；程序负责写替代关系。
  有明确事实切换日期时，由程序在新增成功后补旧 valid_to，保留原旧来源。
  未知日期不填；不会因为新陈述较晚就自动替代。
- 新消息明确纠正先前错误：新增更正对象，保留旧对象。
  正文保留理解该信息必需的纠正限定；用 supersedes_ids 指出明确纠正的旧 ID。
  不能写成旧说法过去真实成立，也不能用结束日期包装错误陈述。
- 新旧信息矛盾但原因不明：保留旧对象并记录有来源的新陈述，
  必要时说明与旧说法不一致；不只凭入库先后裁定哪条为真。
- 候选中的不相关旧对象保持原样。
- 不执行删除；用户要求忘记由应用的独立操作处理。

来源与时间：
- source_ids 必须使用输入目录中的 ID，保留已有对象原来源。
- supersedes_ids 只使用 Existing Memories 中的实际旧 ID；旧对象回带时为空。
- 源 metadata 中的 superseded_by 由程序维护，不能自行生成新目标 ID 或清空旧值。
- 来源时间说明该来源何时被说出。来源可能只支持结束日期等元数据，
  不能把来源列表中的最大时间当作旧正文再次被确认的时间。
- 每条来源的消息时间是理解“今天”“昨天”等表达的参照；
  使用各消息自己的时间，不使用记忆入库日期代替消息日期。
- “现在”可以表示消息当时报告的状态；不据此补造状态开始或结束时间。
- 事实起止时间有明确依据才填写 valid_from/to，否则留空。
- 原时间只有年/月/日时保留其精度，不能编造更精确的时间。

输出：
- 按 schema 调用工具新增或 patch 对象。
- 不额外输出 ADD/UPDATE/DELETE/NONE 的 JSON 协议。
- 不为完整扫描了候选而强制重写全部对象。
```

模型所见的输入分区应由代码构造，示意：

```text
<new_messages>
[{"id":"m2","role":"user","observed_at":"...","content":"..."}]
</new_messages>
<recent_context>
[{"id":"m1","role":"assistant","observed_at":"...","content":"..."}]
</recent_context>
<source_metadata>
新来源和旧候选来源的角色/消息时间对照；未知字段明确为空
</source_metadata>
<memory_metadata>
[{"id":"actual-old-memory-id","superseded_by":null}]
</memory_metadata>
```

`Existing Memories` 通过 manager 的 `existing` 输入交给 Trustcall，保持其文档 ID 和 schema，不必在 messages 内再复制一遍完整旧对象。来源/时间对照要展示给模型，不能仅放在模型看不到的程序变量里。

来源引用是记录级的依据，不是逐句证据标注。例如，新消息既支持新状态，也支持旧状态的结束日期，可以同时被两条记录引用；这不表示旧正文在新消息时刻仍成立。首版把这个限制明确交给提取和回答模型，不新增逐字段证据表，也不按 `max(source_refs.observed_at)` 生成所谓的当前事实时间。

### 提示词参考依据

| 规则 | 依据 | 本次调整 |
|---|---|---|
| 新消息、近期上下文、旧记忆分区；角色归属 | [Mem0 V3 提示词](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/configs/prompts.py#L418) | 保留结构和有效原则；去掉 ADD-only 限制、强制提取数量和过度宽泛的归纳。 |
| 重复不变、补充沿用旧 ID | [旧版 Mem0 更新提示词](https://github.com/mem0ai/mem0/blob/v1.0.11/mem0/configs/prompts.py#L151) | 采用 NONE/UPDATE 的判断及示例；删除改走用户遗忘入口；变化/纠正采用保留旧记录的新增。 |
| 结构化新增/patch 与校验 | [LangMem manager](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/extraction.py#L213) | 按 Schema 工具调用，不套用 Mem0 的另一套 JSON event 协议。 |
| 有限候选内整理冗余信息 | [LangMem 默认指令](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/extraction.py#L168) | 保留整理目的；不直接采纳广泛推理归纳、自动概率估计和错误/过时信息覆盖要求。 |
| 消息时间参照与事实时间分开 | Mem0 提示词的 Observation Date；Graphiti 审查中的 episode/事实时间区别 | 输入逐消息时间；不强行精确化“最近”等模糊表达。 |

实际提示中应加入少量与当前 schema 一致的整理示例，覆盖无用/重复、非矛盾补充、状态变化、明确纠正及来源归属。下文的执行例是行为说明，不能当成记忆效果评测集。

## 实施说明 6：LangMem 的最小适配

manager 配置起点：

```python
# 实际工厂还会为各次调用约束本轮来源；不新增持久化字段。
schemas = [MemoryContent, Done]
enable_inserts = True
enable_updates = True
enable_deletes = False
max_steps = 1
```

`max_steps=1` 指 manager 的一次业务整理循环；Trustcall 的校验修复可能增加实际模型请求，不能据此承诺只请求 LLM 一次。

`max_steps` 是 `ainvoke()` 的输入字段，不是 `create_memory_manager()` 的构造参数。工厂原样返回的是默认 `MemoryManager`；要替换提示构造，需要实际实例化我们的适配类，不能只调用原工厂并误以为固定追加句已经消失。固定版本的起点如下：

```python
from langmem import utils
from langmem.knowledge.extraction import Done, MemoryManager

class SourceAwareMemoryManager(MemoryManager):
    def _prepare_messages(self, messages, max_steps=1):
        return [
            {"role": "system", "content": self.instructions},
            {"role": "user", "content": utils.get_conversation(messages)},
        ]

manager = SourceAwareMemoryManager(
    chat_model,  # 配好的 LangChain BaseChatModel；不是 DB-GPT LLM 对象原样直传
    schemas=[MemoryContent, Done],
    instructions=memory_instructions,
    enable_inserts=True,
    enable_updates=True,
    enable_deletes=False,
)
results = await manager.ainvoke({
    "messages": manager_messages,  # 含已序列化的新消息/近期上下文/来源目录分区
    "existing": existing_documents,
    "max_steps": 1,
})
```

这里仅替换提示准备方法，仍沿用父类的 Trustcall、patch、校验及结果合并。`manager_messages` 可以是一条承载输入分区文本的 HumanMessage；原始角色在分区 JSON 中标注，不能根据承载消息的 role 推断事实来源。`existing_documents` 单独传入，不再复制完整旧正文。两批代码已经采用这个适配方法并完成无外部模型的接线检查；真实模型的工具调用结果仍待验证。内部方法不是上游保证稳定的公开扩展 API，升级时需复核。

需要一个局部提示适配，保留 LangMem 的 extractor、patch 应用、校验和返回结果处理：

1. 默认 `_prepare_messages()` 将消息统一格式化，并在 `instructions` 后追加“错误/过时则更新”。这与本方案新增保留变化/纠正的约定冲突。
2. 第一批已在固定版本下继承 `MemoryManager`，调整输入提示构造并去掉冲突追加句；第二批沿用这个边界，补充程序维护的替代 metadata 展示，不复制 Trustcall 或另写 patch 引擎。
3. 不采用默认宽泛推理归纳指令，使用上述草案及与 schema 匹配的示例。
4. `existing` 构造成 `(实际旧记忆 ID, "MemoryContent", schema 对象字典)`；新旧来源均放入目录。当前实现只将未被替代的候选开放为可编辑对象，已替代对象仍在消息 metadata 中作为只读上下文。
5. 对没有修改的旧对象，manager 会原样带回。写回应比较对象，而不是把返回列表全当作新增或更正。

返回对象可能包含已构造的 Pydantic 对象，也可能包含原样带回的字典。比较前统一转换并按 `MemoryContent` 校验；来源 ID 和临时替代请求 ID 去重后按稳定顺序表示，避免仅因数组顺序不同产生更新。持久化字段的比较与临时替代请求分开，不能把两者混成 metadata 更新。

源码依据：[提示构造](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/extraction.py#L457)、[输入及结果合并](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/extraction.py#L237)。

## 实施说明 7：写入前旧记忆检索

首版建议将本次用户消息和 assistant 回复拼成保留角色的文本，例如 `user: ...\nassistant: ...`，作为写入检索查询。来源 ID 和时间另外给提取模型，不需要作为向量查询正文。保留 user/assistant 标签参考 Mem0 parse_messages；把 tool 返回内容加入查询属于我们的应用扩展，未在固定 Mem0 普通自动提取的格式化器中实现，不作为首版默认输入。system 模板不作为默认查询主体。源码：[固定 parse_messages](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/utils.py#L55)。

```python
hits = mem0.search(
    query=query_from_new_messages,
    filters={"user_id": scope_key},
    top_k=write_candidate_limit,  # 起点为 10
    rerank=False,
)
```

- 查询来自本次新消息；近期历史只帮助提取理解，首版不把所有历史拼成搜索 query。
- 使用一个公开搜索入口，不新增“对话查询 + 逐事实查询 + 实体查询”三路编排。
- SDK 可以使用其已有关键词评分等能力；后端不支持的部分不声称已经生效。
- 本轮首版接法建议先使用语义检索：在独立环境安装核心依赖，不引入 `fastembed`、spaCy/NLP 和全部 extras，并核对锁文件没有将这些可选包带入。当前 Qdrant provider 在缺少 fastembed 时返回无关键词信号；Mem0 在没有可用 spaCy 模型时不产生实体提取结果。这依赖实际环境，不能虚构 `search(bm25=False)` 开关，也不能认为 `rerank=False` 会关闭 BM25。
- 此处 NLP 扩展专指 Mem0 的可选依赖 mem0ai[nlp]，包含 spaCy；固定加载器使用英语模型 en_core_web_sm，供本地实体提取和 BM25 词形还原。它与 LangMem 通过 LLM 做记忆提取/整理是不同组件。FastEmbed 是另一项可选依赖，Qdrant provider 用其 BM25 稀疏编码器。[依赖声明](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/pyproject.toml)、[spaCy 模型加载](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/utils/spacy_models.py)。
- Mem0 公开搜索与 V3 内部 Top-10 向量检索不同。前者的关键词/实体信号主要重排语义候选，不保证召回向量漏掉的旧记忆。
- `add(infer=False)` 绕过自动实体关联，不能声称新建记忆都已进入实体索引；但显式 update 在正文变化时会调用实体重新关联，所以它不等于全局关闭实体功能。本阶段不额外建设实体维护管线，实际生效能力由固定依赖环境记录。
- 每条命中解析 content 与我们的 metadata，核对 scope，形成 `existing` 映射。遇到缺来源的旧格式记录应明确识别，不能编造来源或当作完整 schema 文档无声处理。
- 候选不足或漏召回属于已知边界；只有观察到可重复失败，再考虑事实级补检索或其他现成检索方案。

## 实施说明 8：记忆落库格式与 ID

### 8.1 Mem0 与模型对象的对应

| 模型/逻辑字段 | Mem0 中的保存位置 |
|---|---|
| `content` | SDK 保存的记忆正文，接口通常返回为 `memory`，底层 payload 为 `data`。 |
| `source_ids` + 来源目录 | metadata 中的 `source_refs_json`，每个元素保存 ID、role、observed_at。 |
| `valid_from/to` | metadata 同名可选字段；新增时未知可省略，读取时转回 `None`。普通整理会保留旧边界，只有调用底层 update 时显式传 `None` 才表示清空；模型省略字段不等于清空。 |
| `supersedes_ids` | 第二批模型临时写回请求，不持久化。校验后用 ADD 返回的实际新 ID 写回对应旧记录的 `superseded_by`。 |
| `superseded_by` | 第二批程序管理的 metadata 标量字段，可空；指向同 scope 的实际替代记录。 |
| scope | SDK 的 `user_id` 容器键，metadata 另保留 scope_type/scope_id 供封装核对。 |
| `recorded_at` | metadata 保存程序首次写入时间；当前原型更新时保留原值。SDK `created_at/updated_at` 另行读取，不冒充来源或事实时间。 |

来源数组序列化为 JSON 字符串；替代目标直接保存为字符串或 null。读取后由封装解析结构化对象。第一批已有 `related_memory_ids_json`；第二批读取旧数据时可兼容该键，但不能把其中的通用关联自动推断为替代。新写入不重复保存通用关联数组，具体迁移见实施说明 17。

示意 metadata：

```json
{
  "scope_type": "project",
  "scope_id": "project-123",
  "source_refs_json": "[{\"id\":\"m2\",\"role\":\"user\",\"observed_at\":\"2026-10-08T10:00:00+08:00\"}]",
  "superseded_by": null,
  "recorded_at": "2026-10-09T02:00:00+00:00",
  "valid_from": null,
  "valid_to": null
}
```

嵌入的是自包含正文；来源、替代目标与记录时间在 metadata 中。补充来源时按 source ID 合并，保留每条来源自己的时间。scope、Mem0 保留字段和已有 `superseded_by` 不能被普通模型 patch 覆盖。

不把 `valid_to` 传成 Mem0 原生 `expiration_date`。固定版公开 search 默认排除已过 `expiration_date` 的记忆；本方案的事实结束日期只存在于自定义 metadata，结束的旧状态仍能参与历史召回。SDK 的存储过期与事实有效期是不同功能，第二批不启用存储 TTL。[固定搜索结果过滤](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L3331-L3374)

Mem0 `update()` 在原 payload 上合并 metadata；省略字段不会删除旧值。来源写完整 JSON；`valid_from/to` 若被合法清空则显式传 `None`，普通更新必须保留已有 `superseded_by`，不能用默认 null 意外重新激活旧记录。已有通用关联键保留但不参与第二批逻辑，新记录不再写它，不为停用模型字段执行数据清除或批量迁移。首个 Qdrant 后端应核对可选字段 null 的保存和读取，换后端时再次确认。源码依据：[metadata 合并](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L2052)。

### 8.2 新增与更新

```python
# 每条记忆使用自己的 metadata，不能把一个批次的来源广播给所有记录。
added = mem0.add(
    content,
    user_id=scope_key,
    metadata=metadata,
    infer=False,
)
mem0.update(old_memory_id, text=updated_content, metadata=merged_metadata)
```

字符串形式的直接 add 会被 Mem0 包装成 user 消息。这个 role 只反映 SDK 的写入载体；我们的召回与归属判断必须读取 `source_refs` 中的真实角色，不能把 SDK 默认 role 当成原始说话者。

LangMem 新对象 ID 与 Mem0 新增后返回的 ID 不是同一生成过程。返回应用的是 Mem0 实际 ID；旧候选直接使用 Mem0 ID。第二批先 ADD 新对象并取得实际 ID，再 UPDATE 被替代旧记录的 metadata。无需让模型引用新草案 ID，但程序必须保存本次实际 ID 映射和已完成动作，不能把临时 ID 写入 `superseded_by`。

## 实施说明 9：写回算法和校验边界

写回参考 LangMem Store Manager 的差异比较，不新增一个语义冲突分类器：

```text
输入 old_by_id 与 manager 的结果列表
  → 完成整批结构、scope、来源、时间和替代请求校验
  → 生成写回计划；保护原来源，归并重复输出和同一旧 ID 的更新
  → 对象及请求均未变化：跳过
  → 先执行计划中的新增，保存 manager ID → 实际 Mem0 ID 映射
  → 再执行旧记录更新；依赖新增的更新须已取得对应实际 ID
  → 替代标记、旧有效期及支持来源合并为该旧 ID 的一次 UPDATE
  → 返回实际写入 ID 和结果摘要
```

具体执行约定：

1. 先完成整批结构、scope 与引用校验，再进行写入。`RemoveDoc` 在本路径不允许执行，删除必须走 forget。
2. 新对象必须有本次新消息的支持；已有对象如果被修改，也必须有本次输入的依据。原样返回的旧对象不因没有新来源而失败。
3. 更新对象必须来自本次旧候选，程序再次核对实际存储 scope。丢失旧来源时按原来源并集保存，不能任由 patch 抹掉来源。
4. 内容和元数据都未变化则跳过；仅来源增加可以做 metadata 更新，仍沿用同一 ID。
5. 非矛盾补充由模型选择 patch；变化/纠正/未解矛盾由模型选择新增。程序不凭字符串差异强行推断是哪种语义。
6. 更新复用 Mem0 的显式 update 与 history 操作追踪；来源引用保留在当前记录。history 的旧文本不自动成为普通向量搜索结果，也不是包含全部旧 metadata 的完整快照保证。
7. 明确已发生的替代或明确纠正，第二批使用 `supersedes_ids` 请求并在旧记录保存 `superseded_by`；原旧正文仍可召回。相互矛盾、不同情境、计划或单纯较晚入库，均不能自动视为替代。
8. 有依据的事实切换时间可以写入新 `valid_from` 和旧 `valid_to`，保留日期精度及支持该边界的新来源；消息时间和替代标记不能自动补成事实区间。更正错误不代表旧说法曾成立。
9. 新增及旧记录 metadata 更新可能部分成功；必须先保存新对象再填旧目标，失败时返回实际 ID 和未完成动作，不能回报整批成功。完整执行规则见实施说明 17。
10. 本批确定性去重需同时比较持久化对象和待执行替代请求，不能因正文相同就吞掉不同目标；重复请求归并并避免重复 UPDATE。非空替代请求不能沿用第一批的“相同正文转为旧 ID 更新/跳过”捷径；具体规则见 17.2。语义重复仍依赖有限候选和 manager。

manager 返回顺序不是数据库写回顺序。例如，旧对象的 `valid_to` patch 可能排在新对象之前；程序必须先生成完整计划，把这类结束日期更新推迟到新记录 ADD 成功之后。旧记录正文变化与“保留该旧正文并替代它”的请求同时出现时，整批校验失败，不能任选一个执行。目标新增失败时，该目标关联的旧日期、来源和替代标记都不提前更新。

仅来源变化仍通过公开 `update(old_memory_id, metadata=merged_metadata)` 保存，不必传新正文。该版本支持只传 metadata，但其内部 `_update_memory()` 仍会重算正文 embedding；这是逻辑上的元数据补充，不是零 embedding 成本的保证。不为优化这一点先维护另一套底层写入实现。

来源存在、scope 相同、格式正确这些校验由程序完成；“补充是否真的不矛盾”“事实时间推断是否可靠”“模型是否选错 ID”主要仍是模型语义判断，不能宣传为确定性校验已经保证正确。

### 执行例：用于读懂代码行为

| 输入关系 | manager 期望输出 | 写回动作 |
|---|---|---|
| 旧记忆“回答先给结论”，新消息同义表达 | 保留旧对象；有必要时仅追加来源。 | 跳过或同 ID 元数据更新。 |
| 旧记忆“负责数据质量”，新消息补充“主要维护 ETL 监控” | 对对应旧对象 patch，保留原内容中仍成立的信息和来源。 | 同 ID 更新，不再新增一条同义概括。 |
| 旧记录项目用 SQLite，新消息明确说已改为 PostgreSQL | 新正文独立表达项目使用 PostgreSQL，`supersedes_ids` 指旧 ID；有切换日期时补旧有效期。 | 先新增，再填旧 `superseded_by`；保留旧正文和来源。 |
| 新消息明确说此前 SQLite 的说法错误，一直用 PostgreSQL | 新正文保留“一直使用 PostgreSQL”等有依据的事实和必要纠正限定，提出旧 ID 的替代请求。 | 新增并链接旧记录；不能用结束日期把先前错误说法写成过去事实。 |
| assistant 提议 PostgreSQL，用户未采纳 | 若建议值得记住，则写成 assistant 提议及相应来源。 | 不改写成“项目已用 PostgreSQL”。 |

这些例子不限定记忆只服务数据库或历史问答，也不构成完整效果评测。

## 实施说明 10：时间处理的统一规则

| 输入情况 | 消息时间 | 事实时间 |
|---|---|---|
| 实时消息有服务器时间或原始发送时间 | 保存为对应来源的 observed_at，统一保留可解析时区。 | 由消息内容决定，不自动照抄。 |
| 新消息没有时间，应用正在实时接收 | 程序可记录本次接收时间，并说明这是接收/观察时间。 | 无依据仍为空。 |
| 旧资料导入，有原消息时间 | 使用原时间。 | 以原消息时间理解相对日期。 |
| 旧资料导入，没有原时间 | observed_at 为空；入库时间另由 SDK 记录。 | 不用导入日期换算“昨天”等相对表达。 |
| “现在已改为……”但未说明开始日期 | 记录来源报告时间。 | 不编造变更恰好发生在消息时刻。 |
| “从 10 月 1 日开始……”且年份有可靠参照 | 保留消息时间。 | 按有依据的日期填写，保留精度及原意。 |
| 已确认同一状态切换，且具体切换日期可靠 | 新旧记录分别保留各自来源时间。 | 可以给新 `valid_from`、旧 `valid_to` 填该边界；仅能确定已经切换时，`superseded_by` 有值但事实时间可空。 |
| “最近”“可能下个月”等模糊或计划表达 | 保留消息时间。 | 保留模糊/计划措辞，不强行填精确生效区间。 |

不会因为两条报告时间先后不同就机械选择较晚值。时间用于帮助模型解释陈述顺序、当前报告与历史变化；来源、语境和是否纠正也一起参与。

## 实施说明 11：普通召回及回答上下文

```python
hits = mem0.search(
    query=user_or_task_query,
    filters={"user_id": scope_key},
    top_k=read_limit,  # 起点为 10
    rerank=False,
)
```

普通召回使用当前用户问题或任务问题，与写入时查询新消息不同。对主记忆库的旧条目不按“较旧”或一个新增状态字段预先排除；不设通用“当前/历史/事件/来源”分类器。

“使用同一个搜索入口”是本组合方案的接法：写入前查旧候选和回答前查记忆都调用 Mem0 公开 search，但使用不同 query。Mem0 自带普通 add(infer=True) 的 V3 路径直接调用底层 vector_store.search（Top-10），并未调用公开 Memory.search。回答前通过公开 search 取记忆、拼入模型上下文的做法则有官方 README 的 chat_with_memories 示例。search 返回候选，最终回答仍由应用调用的模型生成。[固定写入/搜索源码](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py)、[官方聊天示例](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/README.md#basic-usage)。

返回 agent 的每条上下文至少包含：

```json
{
  "reference_time": "应用提供的当前参考时刻",
  "memory_candidates": [
    {
  "id": "actual-memory-id",
  "content": "自包含的记忆内容与必要事实限定",
  "source_refs": [
    {"id": "source-id", "role": "user", "observed_at": null}
  ],
  "valid_from": null,
  "valid_to": null,
  "superseded_by": null,
  "recorded_at": "系统首次保存时间"
    }
  ]
}
```

分数可以用于调试/候选排列，但不作为内容真实性或事实新旧的证明。上下文示例中的空值意味着未知，不需要模型补齐。

回答模型的简短使用指引：

```text
以下是与当前任务相关的记忆候选，不代表完整历史。
按来源角色和消息时间理解陈述；记忆入库时间不等于事实发生时间。
来源可能只支持元数据更新；不能把最大来源时间视为正文再次被确认的时间。
区分状态变化、明确纠正、计划和建议。
更正说明旧说法错误，不能把旧说法当作过去成立的事实。
superseded_by 非空表示已被另一条记录替代；旧记录不能独立证明当前状态。
替代标记不证明旧事实曾经成立，也不提供精确结束日期。
结合问题使用适用内容；仅凭较新的入库日期或较高分数不能裁定矛盾。
依据不足时说明不确定性；未检索到记录不等于事情从未发生。
```

第二批把命中的 `superseded_by` 一并提供，并提供有 scope 校验的 `get` 供 agent 按需取目标；不默认递归遍历整条链或构造完整时间线。只命中旧记录时至少能知道它已被替代；若目标未读取、已删除或缺失，不能补造当前事实。旧记录不从搜索中全局剔除；没有替代标记的记录也不等于已证明当前有效。Top-k 不保证覆盖所有旧候选，写入漏召回仍可能留下未被标记的旧记录。

## 实施说明 12：遗忘、失败、并发与重试

### 12.1 用户要求忘记

用户使用自然语言提出遗忘请求，不需要知道记忆 ID。agent 使用同 scope 的 `search/get` 查找并确定对象，再调用底层 `forget(scope, memory_ids)`；只有请求对象或范围确实不清楚时才澄清。第二批提供这些入口及可运行的 agent 工具接法，不能把按 ID 删除实现宣传成自然语言遗忘链路已经完成。

工具封装中的 scope 由应用绑定，不开放给模型自行选择用户或项目。模型只提供 query 或候选 ID；底层在同 scope 锁内核对整批 ID 的范围，再调用 Mem0 delete。发现越界或引用格式错误时，整批不开始删除；不存在的 ID 记为缺失，其余已核对对象逐项处理并返回结果。存储读取失败不能当成不存在。删除后的主记忆不再进入普通召回，普通整理始终关闭模型删除。不能把未启用管理删除的提取 manager 当作遗忘请求执行器。

Mem0 history 可能保留旧文本，原聊天、备份等也有独立存储。不能将 SDK delete 宣传为全副本物理擦除。已有相关记忆若复述被要求忘记的内容，需由调用方明确删除范围；首版按 ID 删除不承诺自动找全所有派生内容。

删除一个替代目标不会自动把旧记录重新设为当前，也不自动级联删除全部前后记录。若目标已删除，按 ID 读取应返回缺失；保留的替代标记只说明旧记录不能独立证明当前状态。广泛的主题遗忘需要确定完整删除范围，不能用一次 Top-k 搜索代表已找全。

### 12.2 失败处理

- 检索、LLM、embedding 或存储错误应返回明确失败，不装作“没有值得记住的信息”。
- Schema/引用校验未通过时不开始写回；模型工具结果的 schema 修复复用 Trustcall 已有机制。程序执行的 scope/来源/候选引用校验失败并不会自动反馈给 Trustcall，应明确报错，不增加另一轮语义裁决或无条件重新提取。
- SDK 的主存储写入与 history 不承诺跨存储原子事务。多条记忆部分写入失败时，返回已完成的真实 ID 和未完成操作；不能回报整批成功或简单丢弃错误。
- 若 SDK 在报错前可能已经落库，却未返回可确认的 ID，将该动作标为结果未知并保留写回计划；不能直接当成未写入而重复新增。需要先核对实际存储状态，不能承诺 SDK 异常自动具备安全重试语义。
- 第二批错误摘要应区分已确认成功、尚未尝试和结果未知的动作，保留实际 ID 映射和未完成计划。已知新 ID 后，可核对并定向补写旧记录；不无条件重跑整个提取流程，否则新的模型输出可能再产生不同对象。

### 12.3 首版并发和重试边界

- 同一 scope 的 add/forget 通过同一个 `MemoryService` 实例串行；add 的串行区间涵盖候选读取、模型整理和写回。调用方在单进程内复用该实例；多个实例各自的锁不构成共同的串行保证。不同 scope 可以独立处理。旧候选漏返不等于删除指令，不通过输入/输出集合差删除旧对象。
- 当前 `batch_id` 只是追踪标识，不是幂等键：第一批没有缓存计划或恢复入口，相同 ID 再次调用仍会重新检索和提取。第二批要求准确报告部分成功及可核对的未完成计划，不要求另建批次缓存或自动重试器；定向补写前须重新核对存储与 scope。
- 首版不新增分布式锁、持久队列和多库事务协议；跨进程/重启后的完整幂等和部分失败恢复属于部署接入前需补齐的工程项，不能在当前未实现时宣称已经保证。
- 原始输入已经成功写入后再次提交并非一定无新增；不同模型提取、检索漏召回和新的观察时间都可能影响结果。记录此边界并用实际失败决定后续增强。

执行摘要记录 batch_id、scope、候选 ID、最终动作、写入 ID 和错误类别，避免为调试复制全量原文或保存隐藏推理。详细追踪内容由后续运行方式决定，不先建立独立审计服务。

第一批的 `MemoryWriteError.summary` 已携带已返回的新增/更新 ID，但 `events` 还混有计划动作，没有上述逐项执行状态和完整待补写计划。第二批需补齐，不能据第一批异常类声称已实现安全恢复。

## 实施说明 13：配置起点与实现顺序

### 13.0 当前运行模型覆盖（2026-10-12）

当前默认对话/记忆模型已切换为阿里百炼 `deepseek-v4.1-flash`，保留 thinking；`text-embedding-v3` 和 1024 维 embedding 不变。下文 13.1 的 Qwen 配置是 2026-10-08/10-10 的历史复核记录，不再代表当前默认值。DeepSeek 模型支持 Function Calling 和结构化输出；真实端点兼容性仍需通过后续 smoke/评测确认。

| 项目 | 起点 | 依据与限制 |
|---|---|---|
| Python | 本项目已有的 3.11/uv。 | 无需安装新的 Python。 |
| 组件版本 | Mem0 OSS 2.2.1、LangMem 0.0.30。 | 与源码审查一致；升级需核对提示模板/数据契约变化。 |
| 依赖环境 | 沿用第一批独立环境和共同锁文件。 | 共同依赖和导入已检查；不把它当作 DB-GPT 根环境兼容性或真实模型调用的证明。 |
| 向量后端 | Qdrant 本地持久化模式，使用 Mem0 现成 provider。 | 明确指定持久化目录，首版无需启动独立 Qdrant 服务；不是最终生产后端决策。 |
| 模型/embedding | `qwen3.8-max` / `text-embedding-v3`，后者使用 1024 维。 | 当前 DashScope 配置和独立原型已用该组合完成真实写入/回答观察；LangMem 结构化整理默认关闭思考模式，原因和边界见 13.1。密钥由环境或显式配置提供。 |
| 近期上下文 | 10 条消息，可配置。 | 参考 Mem0 的实际实现，非 10 轮，也非效果最优结论。 |
| 写入/普通召回候选 | 分别 10 条，可配置。 | 是编码起点；公开 search 的内部过取数量由 SDK 决定。 |
| manager | 一次业务整理循环；关闭删除。 | 复用校验修复，不保证物理模型调用次数固定。 |
| 额外组件 | 不另加图数据库、意图分类器或检索融合器。 | 可以直接使用已有 SDK 搜索信号；只在实际失败显示必要时扩展。 |

### 13.1 首版具体连接配置（2026-10-08）

配置模型是指定调用哪个服务及其参数，无需训练模型。本机已有 `/Users/zsm/.dbgpt/configs/qwen.toml`，本轮只读取模型名、provider、地址和密钥是否配置，没有输出密钥，也没有调用服务。首版采用以下起点：

| 用途 | 具体配置 | 由谁使用 |
|---|---|---|
| 记忆提取、合并与更新建议 | `qwen3.8-max`；`temperature=0`；LangMem/Trustcall 调用时 `enable_thinking=false`。 | LangChain `ChatOpenAI` 客户端供 LangMem manager 调用。DashScope 在 Qwen3 思考模式下不接受 LangMem 编辑旧文档所需的工具选择；关闭思考只作用于结构化记忆整理，不降低模型更换本身。 |
| 文本向量化 | `text-embedding-v3`；显式配置 `embedding_dims=1024`。 | Mem0 的 OpenAI 兼容 embedding provider。 |
| 服务地址 | `https://dashscope.aliyuncs.com/compatible-mode/v1`。 | 两类客户端都使用基础 URL；embedding 地址不再追加 `/embeddings`。 |
| 认证 | 从现有配置对应的模型/embedding 密钥读取，或由 `DASHSCOPE_API_KEY` 注入。 | 连接适配层处理；不在代码中硬编码。 |
| 记忆正文、来源 metadata 和向量 | Mem0 Qdrant provider；collection `agent_memory_v1`；`embedding_model_dims=1024`；显式持久化目录。 | Qdrant 本地模式保存完整 point 的 payload 和 vector。 |
| SDK 操作历史 | 显式指定独立 `history_db_path`。 | Mem0 自带的 SQLite 历史存储；不另建自研记忆主表。 |

DB-GPT 配置里的 `proxy/tongyi` 是 DB-GPT 的 provider 名称；这里通过 DashScope 的 OpenAI 兼容 API 连接。Mem0 配置中的 `provider="openai"` 表示选用兼容客户端，实际服务和模型仍是 DashScope/Qwen。LangMem 不是 embedding 或数据库实现，它在这里接收 chat model 和我们预先检索出的旧记忆。

下面是 SDK 连接参数示例，尚未执行。为独立运行展示环境变量读取；真实入口也可以安全读取上述 TOML 中对应密钥。若模型和 embedding 使用不同密钥，应分别传入各自配置。

```python
import os
from pathlib import Path

from langchain_openai import ChatOpenAI
from mem0 import AsyncMemory

api_key = os.environ["DASHSCOPE_API_KEY"]
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# 此默认值以仓库根目录为运行位置；实际入口应允许指定绝对路径。
data_root = Path("examples/agent_memory_v1/data").resolve()
data_root.mkdir(parents=True, exist_ok=True)

chat_model = ChatOpenAI(
    model="qwen3.8-max",
    api_key=api_key,
    base_url=base_url,
    temperature=0,
    extra_body={"enable_thinking": False},
)

memory_store = AsyncMemory.from_config({
    "llm": {
        "provider": "openai",
        "config": {
            "model": "qwen3.8-max",
            "api_key": api_key,
            "openai_base_url": base_url,
            "temperature": 0,
        },
    },
    "embedder": {
        "provider": "openai",
        "config": {
            "model": "text-embedding-v3",
            "api_key": api_key,
            "openai_base_url": base_url,
            "embedding_dims": 1024,
        },
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "collection_name": "agent_memory_v1",
            "path": str(data_root / "qdrant"),
            "embedding_model_dims": 1024,
        },
    },
    "history_db_path": str(data_root / "history.db"),
})
```

`chat_model` 传给实施说明 6 的 manager。Mem0 构造时也初始化自己的 LLM 客户端，因此明确配置同一个服务；本方案普通新增使用 `infer=False`，不再调用该客户端做第二轮自动提取。embedding 模型配置与 collection 维度必须一致；实际连接时确认返回向量长度。更换维度或向量模型时使用新的 collection 或重新建立向量，不能混用不同向量空间。

Qdrant 本地模式无需另开数据库服务器；本地目录负责持久化，SQLite 文件只承担 Mem0 原生操作历史。这些独立数据目录不替换 DB-GPT 当前 Chroma 或元数据库。多进程共享、服务部署等在接入阶段决定。

配置依据：[现有模型的 LangChain 官方接入示例](https://help.aliyun.com/zh/model-studio/use-bailian-in-langchain)、[embedding 官方 API 与维度](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)、[固定版本 Mem0 embedding 配置](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/configs/embeddings/base.py)、[Qdrant provider 配置](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/configs/vector_stores/qdrant.py)。以上是复用现有设置的工程起点，不代表已经证明这是最佳模型组合。

#### Qdrant 的选型依据与边界（2026-10-08）

Qdrant 是开源向量数据库/检索引擎。一个 point 包含 ID、vector 和 payload；本方案由 embedding 模型生成 vector，将记忆正文、来源和时间 metadata 保存到 payload，再按可信 scope 过滤并搜索相似记忆。Qdrant 提供存取和检索能力，记忆提取、合并及语义冲突处理由 manager 和适配层承担。

**重审结论：内置适配、metadata 过滤和本地持久化是共同的基础能力，不足以证明 Qdrant 比其他候选更合适。** 本轮进一步核对三个固定提交的 provider，按当前设计实际使用的能力比较；没有执行性能或记忆效果测试。

当前需求是独立记忆条目、来源/时间 metadata、按可信 scope 查相关候选、按明确 ID 补充/读取/删除，以及以新条目保留变化。来源原文由调用方引用，不另建来源主表；普通召回将这些信息交给模型，首版不做跨表关联、通用时间区间 SQL 或整批跨存储事务。下面区分数据库能力与 Mem0 实际接入的能力：

| 比较项 | Qdrant | PostgreSQL + pgvector | Chroma |
|---|---|---|---|
| 当前单条记忆及 metadata | point 的 vector + JSON payload，可保存正文和元数据。 | 固定 provider 建表 `id UUID, vector vector(n), payload JSONB`；原生保存向量和 JSON 数据。 | 固定 provider 把 payload 作为 metadata 存入 collection；来源/关联按本文约定保存为 JSON 字符串。 |
| scope 内向量搜索 | 固定 provider 传 query_filter；服务端有 payload 索引及 Filterable HNSW，provider 为 user_id 等常用字段建 keyword 索引。 | 固定 provider 使用 JSONB WHERE 条件与向量距离排序；默认配置建 HNSW，但不自动创建 scope 表达式索引或启用 iterative_scan。 | 固定 provider 使用 collection.query 的 where 条件；满足当前硬范围约束。 |
| Mem0 实际接入的关键词路径 | 有 keyword_search，使用 BM25 稀疏向量；需 fastembed 编码器及正确 collection。 | 有 keyword_search，使用 PostgreSQL `simple` 全文检索与 ts_rank_cd；不是 BM25。 | 当前固定 provider 未覆盖 keyword_search，继承基类返回 None；不代表 Chroma 产品没有全文/混合检索能力。 |
| 来源/历史的复杂查询及约束 | 可按 payload 过滤或按 ID 读取关联；复杂联接和跨记录约束需应用编排。 | SQL、JSONB、JOIN、约束和事务适合扩展复杂数据查询；本文 SDK 接法并未自动利用全部能力。 | metadata/ID 查询满足简单记忆存取；复杂关系处理需要应用编排。 |
| 初始运行与部署 | Python path 本地模式或服务模式。 | 需要 PostgreSQL 实例、vector 扩展和连接配置。 | 有本地持久化和服务模式；DB-GPT 已有相关依赖与使用基础。 |
| 整轮整理的事务保证 | 当前 Mem0 调用逐项写回，历史另存 SQLite，没有本方案的整轮事务。 | 数据库支持事务，但固定 provider 各 API 独立获取连接和提交，历史仍是 SQLite；只更换 provider 不会获得整轮原子事务。 | 当前 Mem0 调用同样没有本方案的整轮事务。 |

**Qdrant 暂作为编码起点，仍是候选；当前证据不足以认定它最适合本项目。** 当前以 scope 内相关候选检索及单条对象存取为主，Qdrant 的服务端过滤检索设计与此契合；它的现成 Mem0 provider 还保留了 Chroma provider 未接入的 BM25 搜索入口。这些事实支持沿用已经核对的工程路径，但没有证明它优于 pgvector、Milvus、Elasticsearch 等其他候选。BM25 是可复用能力，不是已经验证的效果优势；中文分词、实际候选覆盖和组合排序仍需观察。Mem0 的公开搜索当前以语义命中构建候选，关键词信号参与排序，不能承诺解决语义漏召回。先前“继续推荐 Qdrant”的措辞应按本段解释，不能当成最终数据库选型已经完成。

PostgreSQL + pgvector 是明确的替代方案：如果产品要求把来源、记忆版本、关联和操作记录放入同一事务中，或大量使用 SQL 查询、约束及联接，优先级应升高。届时需适配写回事务和历史存储，而非只改连接配置。pgvector 的精确搜索可满足严格范围内排序；使用 HNSW/IVFFlat 等近似索引时，官方说明过滤可使候选不足，需要合适的过滤索引、迭代扫描或分区。这是召回/配置问题，不表示 SQL 会返回其他用户的记录；Qdrant 的近似搜索也不保证完整召回。

Chroma 能满足当前基础链路，尤其在优先统一 DB-GPT 现有存储时有价值；当前用户把记忆机制本身置于接入便利之前，而固定 Mem0 adapter 未暴露关键词路径，因此本轮未将其列为首选。不根据数据库名称断言数据会丢失、检索一定更差或不能用于生产。

这是一项基于当前需求和代码能力的工程起点，没有完成三个后端的统一性能/成本/中文召回比较，也未完成所有注册后端的适配审查，不给出性能排名。强选型依据应是明确的必要能力、部署约束或实际工作负载表现；目前不存在已证实的 Qdrant 独有必要条件。最终生产配置仍应结合真实数据量、并发、事务和运维要求确定。采用 Qdrant 服务模式时仍需验证索引、并发和恢复行为；已有本地数据转入服务需要迁移，修改连接地址不会自动搬运数据。

本地模式不等于生产服务模式：官方将它定位于开发/原型，固定 Mem0 provider 还会跳过本地 payload 索引创建。它的运行表现不能代表 Qdrant 服务端索引或多进程部署能力。Qdrant 支持稀疏向量/混合检索，但本方案能否使用 BM25 仍取决于实施说明 7 的编码器、collection 和 SDK 路径，不能仅由数据库名称推定已启用。

依据：[Qdrant 数据结构与搜索概述](https://qdrant.tech/documentation/overview/)、[服务端过滤索引](https://qdrant.tech/documentation/manage-data/indexing/#filterable-hnsw-index)、[官方客户端本地模式](https://github.com/qdrant/qdrant-client#local-mode)、[固定提交 Qdrant provider](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/vector_stores/qdrant.py)、[Chroma provider](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/vector_stores/chroma.py)、[pgvector provider](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/vector_stores/pgvector.py)、[pgvector 过滤与索引](https://github.com/pgvector/pgvector#filtering)、[PostgreSQL 事务](https://www.postgresql.org/docs/current/tutorial-transactions.html)。

#### Mem0 支持范围与 BM25 用语说明（2026-10-08）

固定 Python 提交的 VectorStoreFactory 注册了 **25 个后端适配**：Qdrant、Chroma、pgvector、Milvus、Upstash Vector、Azure AI Search、Azure MySQL、Pinecone、MongoDB、Redis、Valkey、Databricks、Elasticsearch、Vertex AI Vector Search、OpenSearch、Supabase、Weaviate、FAISS、LangChain、Amazon S3 Vectors、Baidu、Cassandra、Amazon Neptune Analytics、Turbopuffer、Oracle AI Vector Search。FAISS 和 LangChain 是库/适配入口，不是独立数据库；注册支持也不表示依赖全部已安装，或所有后端能力相同。当前官网和 TypeScript 的支持范围与此固定 Python 实现应区分。[固定注册表](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/utils/factory.py#L167)、[官方支持列表](https://docs.mem0.ai/components/vectordbs/overview)。

BM25 是关键词检索评分方法，按查询词是否出现、词频、词的稀有程度和文本长度等衡量匹配度。例如组件名或专有词明确出现时，可以给含该词的相关记忆提供额外排序信号。向量检索提供语义相似度，BM25 提供词项匹配信号，两者的实际组合和效果由搜索实现决定。

“Qdrant 适配提供 BM25 入口”具体指上游已经实现 keyword_search：写入时可用 fastembed 将文本编码成 BM25 稀疏向量，存到 collection 的 bm25 槽；搜索时同样编码查询并调用 Qdrant 取关键词命中及分数。Mem0 公开 search 会调用这项能力，但目前最终候选来自向量命中；关键词分数只对这些候选参与排序。fastembed 未安装/初始化失败或 collection 不支持该槽时，关键词路径可能被禁用。

BM25 不属于 Qdrant 独有能力；例如 Elasticsearch 原生默认使用 BM25，Milvus 也提供 BM25 全文搜索。数据库原生能力与固定 Mem0 adapter 是否接入应分别检查。因此“存在 BM25 入口”可以说明复用方便，不能单独证明必须选 Qdrant。[Elasticsearch BM25](https://www.elastic.co/docs/reference/elasticsearch/index-settings/similarity#bm25)、[Milvus 全文检索](https://milvus.io/docs/full-text-search.md)。

#### BM25 稀疏表示的实际实现与待收敛项（2026-10-08）

“稀疏”表示只保存非零的词项坐标及其权重，不保存大量未出现词项的零值。为演示，假定文本已正确分词，Milvus、数据库、项目的词号分别是 17、42、86，文档权重均为 1.4，则可以写成 `indices=[17,42,86], values=[1.4,1.4,1.4]`。这些编号/数值是教学示意，不是运行输出。查询与文档用同一套词项坐标，通过相同坐标上的权重乘积和求和计算匹配分数；本例的每个坐标表示词项，与 text-embedding-v3 产生的语义表示分别承担不同搜索信号。

本轮读取 FastEmbed 官方 main 的 Bm25 实现：文本清理、分词、停用词/词干处理后，使用哈希生成词项 ID；文档权重是包含词频饱和与长度归一化的 BM25 词频部分。Qdrant 的 IDF modifier 在检索阶段补充词项稀有度，稀疏索引采用类似倒排索引的数据结构；不是将这些向量再送给 LLM 判断匹配。源码参数 avg_len 默认 256，是可配置值，不能写成该客户端会自动实时计算语料平均长度。

两项实际接法需要在首版后端/检索配置收敛时处理：

1. **中文词项切分。** 本轮查看的 Bm25 默认 language 为 english，使用 SimpleTokenizer 按非词字符和空白切分；连续中文可能作为整段词项，不能据此假定具备中文自然分词。需要确定实际文本的词项处理，以及选用/锁定的 FastEmbed 版本；不能把这一问题当成 BM25 算法不支持中文。
2. **查询编码。** FastEmbed 提供 query_embed，将查询中的词项权重设为 1；固定 Mem0 Qdrant adapter 的 keyword_search 却复用 _encode_bm25，而后者调用 encoder.embed，查询也经过文档词频/长度加权。因此描述该路径时应注明实际接法，不直接将其分数等同于标准查询编码；是否调整应结合选定版本和搜索参数处理，不先宣称这是运行故障。

FastEmbed 在当前共同环境中尚未安装或锁定，以上是官方上游源码分析，不能替代实际依赖版本和运行结果。来源：[Bm25 编码实现](https://github.com/qdrant/fastembed/blob/main/fastembed/sparse/bm25.py)、[SimpleTokenizer](https://github.com/qdrant/fastembed/blob/main/fastembed/sparse/utils/tokenizer.py)、[Qdrant 稀疏索引与 IDF](https://qdrant.tech/documentation/manage-data/indexing/#sparse-vector-index)、[固定 Mem0 adapter](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/vector_stores/qdrant.py)。

### 13.2 首版接法收敛建议（2026-10-08）

下面记录 2026-10-08 的工程接法，并按第一批实际进度更新：环境和最小写回已完成接线检查，第二批替代写回、普通召回与遗忘以实施说明 17 为准。最终生产后端和记忆效果未确定，不能由初始化成功推断质量。

| 问题 | 首版具体接法 | 收敛理由 |
|---|---|---|
| 数据库 | 沿用 Mem0 现成 Qdrant provider，本地持久化、单进程独立环境、collection `agent_memory_v1`。 | 当前是独立记忆对象与 metadata 存取，已核对这条 provider 的调用链；qdrant-client 已是固定 Mem0 的核心依赖。本地模式无需另起服务器。按已满足需求的复用路径开始编码，没有性能最优或独有能力的结论。 |
| 数据保存 | 正文、来源、时间、程序管理的替代 ID 与向量保存在同一个 point；第二批沿用实施说明 8、17 的编码。 | 不拆来源/版本主表。第一批通用关联字段只作兼容读取，不自动转为替代；日期可空，普通更新不能意外清空已存替代目标。 |
| 基础检索 | Mem0 公开 AsyncMemory.search，可信 scope 过滤，top_k=10、rerank=False；首版依赖环境不带 fastembed/spaCy。 | 复用现成语义候选及排序路径，避免一开始适配中文 BM25、查询编码和多路合并。top_k 为可调起点，不保证找全旧记忆。 |
| 写入检索文本 | 默认将本次用户消息和 assistant 回复按角色拼接。此前 10 条消息单独作为提取上下文；tool 结果属于调用方显式提供的预留输入扩展。 | user/assistant 角色文本有固定 Mem0 parse_messages 依据；tool 不在该格式化器默认分支中。搜索相关性与事实归属分别处理，检索命中不代表 assistant 建议成为用户事实。 |
| 普通召回 | 同一个 collection 和范围过滤；查询来自用户问题或 agent 当前任务；返回命中的正文与 metadata。 | 旧记录不预先剔除，消息时间、来源和更正原意供 agent 理解；不增加通用查询意图分类器或全局“最新优先”。 |
| 提取与写回 | LangMem 无状态 manager、现有 schema/instructions 和局部提示适配；按 ID/对象差异调用 Mem0 add(infer=False)/update。 | 复用结构化提取和 patch，薄层只处理范围、来源、时间格式、ID 和差异写回。 |

BM25 后续若接入，正文编码与查询编码必须采用相同的中文词项处理；保留原正文及语义 embedding 文本，分词结果只供关键词路径。优先复用成熟中文分词与 Qdrant/FastEmbed 能力。还要明确目标是“调整语义命中的排序”还是“补回语义漏掉的候选”：后者需要合并关键词候选，不能把固定 Mem0 search 当前的评分路径宣称为已经实现该目标。候选覆盖不足的后续改动应对应实际失败，不在本轮预建三路检索。

环境建立时记录实际启用的检索能力。固定版 Mem0 没有公开 BM25 关闭参数；首版以独立依赖环境实现上述范围，不通过修改全局环境或伪造 SDK 参数实现。若传递依赖意外引入可选 NLP/稀疏编码器，应调整独立环境或明确更新接法，而不是让启用能力静默变化。

源码依据：[Mem0 核心/可选依赖](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/pyproject.toml)、[Qdrant 编码器降级](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/vector_stores/qdrant.py)、[实体提取的无模型降级](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/utils/entity_extraction.py)、[公开搜索及显式 update](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py)。

### 13.3 实现顺序与分次交付

按下面顺序实现，每一步产物应明确：

1. **按实施说明 13.2 的接法建议初始化共同环境。** 固定 Mem0/LangMem 及完整传递依赖，连接 Qdrant 本地持久化与既有模型配置，核对实际启用的检索能力，落实基本导入和 metadata 存取路径。遇到真实接口/依赖问题再局部调整；结果记录为工程适配，不能当记忆效果评测。
2. **输入对象、schema 和提示适配。** 对照本文件落实来源目录、输入分区、manager 参数及旧候选数据转换。
3. **写入链路。** Mem0 候选检索 → LangMem 整理 → 引用/范围校验 → ID/对象差异比较 → Mem0 新增或更新。
4. **第二批增量。** 按实施说明 17 调整替代写回和有依据的有效期；解析 metadata 返回普通召回上下文；提供 scope 校验的 search/get/forget 及 agent 工具调用方式。
5. **运行观察并修正具体问题。** 使用多种有实际用途的消息与后续任务，观察记忆内容、重复、补充、变化、来源、时间及召回；不将单个位置/数据库例子当作整体效果标准。
6. **DB-GPT 接入。** 核心链路可运行后决定调用位置、配置与部署恢复策略，接入其任务流程。

分次交付：第一批完成步骤 1–3，提供可调用的写入入口；第二批完成步骤 4 的替代写回、普通召回和遗忘，随后开展步骤 5 的真实模型/embedding 运行观察，再进行步骤 6。第一批不因无外部模型 smoke 通过就等于真实提取、历史保留或整体质量达标。每批记录运行状态、使用方式和未完成项，不把全部功能、评测和 DB-GPT 集成合成一次交付。

2026-10-09 验收状态：第一批步骤 1–3 和第二批最小实现已落在 `examples/agent_memory_v1/`；固定依赖环境、配置解析、Mem0 本地 Qdrant 初始化、Python 编译/导入、无外部模型的 fake/stub 边界检查和第一轮真实模型/API 调用已通过。端到端整体效果和 DB-GPT 接入仍未验证。

## 实施说明 14：仍需实际确认的清单

| 待确认项 | 怎样确认 | 是否需要用户先补充 |
|---|---|---|
| 真实模型与 embedding 接口兼容 | 共同依赖、导入和一轮真实服务调用已检查；仍需扩大观察并确认不同配置下的稳定性。 | 否，属于工程适配。 |
| 第二批 Qdrant/provider metadata | 第一批初始化已检查；仍需按新格式保存、读取、清空可选时间、更新替代标记、搜索和删除。 | 否。 |
| LangMem 局部提示适配与工具调用一致 | 查看模型实际输入和 ID/对象输出，确认追加指令不再冲突。 | 否。 |
| 首版提示词是否控制重复和补充 | 运行链路后检查对象与真实写回，而非只读 prompt。 | 否，但不能事先宣称效果已验证。 |
| 模型是否把变化/纠正误 patch，以及检索是否漏候选 | 检查实际失败，优先调整已有 prompt/检索参数或复用更合适组件。 | 无需先补充；语义策略若改变再讨论。 |
| 生产部署、自动触发、跨进程恢复和删除范围 | 与 DB-GPT 接入时的运行要求结合决定。 | 后置，不阻止独立核心实现。 |

具体接口、schema 和提示词的起点已经在本文给出；后续改动应同步这里，避免聊天中的决定与代码/文档分叉。

## 实施说明 15：文档审核结果（2026-10-08）

本节保留当时的审核快照，包括当时尚未建立共同环境的状态；它不表示这些事项现在仍未完成。2026-10-09 的实际状态见实施说明 16，最新审核修正见 17.6。

**结论：修正下表缺口后，文档可以支持另一位 agent 开始首版实现。它是设计和实施交接材料，不是已完成系统的运行手册或效果证明。** 当前无需重新讨论整体架构；依赖适配与模型实际行为仍需在实现中确认。

本轮审核读取当前设计、推进计划、三份源码审查、历史对照、场景与探针记录，并按固定提交复核 Mem0 的 add/search/update 与 LangMem 的提示准备、输入和结果合并。没有运行记忆链路、重新执行组件探针或评测模型效果。

| 问题 | 对交接的影响 | 本轮处理/剩余边界 |
|---|---|---|
| 早期对照仍写“第一版使用 status、关系分类、多路检索” | 接手者可能实现已暂缓的设计。 | 对照文件明确标为历史候选；当前实施以本文为准。场景表是检查素材，不是逐项开发清单。 |
| 可选 metadata 清空时省略字段 | Mem0 update 合并旧 payload，旧时间可能残留。 | 更新编码明确传 `None`；来源/关联完整写入当前数组。后端 null 存取尚需运行确认。 |
| manager 的构造与调用不够具体 | 可能把 max_steps 传错位置，或原工厂仍追加覆盖旧信息的指令。 | 给出固定版本的局部继承起点与 ainvoke 输入；明确原工厂不会自动使用适配类。 |
| async 入口与同步示例并存 | 可能把同步 SDK 调用直接放进事件循环。 | 首版异步客户端优先 AsyncMemory；示例的同步参数说明与真实 await 调用区分。 |
| Qdrant 名称被误读为 BM25 已开启 | 核心安装并不保证可选编码器及关键词能力生效。 | 说明 fastembed 等实际条件；记录运行时能力，不默认安装整包 extras。 |
| 来源 ID 被误读为原文可回查保证 | 生成 ID 没有对应原文存储时，无法按 ID 取原文。 | 区分来源标识与原文读取能力，不新建原文库；真实原文引用由调用方提供。 |
| 探针只保留文字记录、临时环境已不存在 | 无法完整复现此前少量输出。 | 明确其证据级别和未保留材料；不把探针当质量排名、共同依赖证明或接手前置。 |
| 只固定两个主包，没有共同传递依赖锁文件 | 不同机器可能解析出不同 LangChain/Trustcall/Qdrant 依赖。 | 第一版共同环境生成独立锁文件；当前文档不声称依赖组合已经通过。 |
| 文档当前未被 Git 跟踪 | 新 clone 或新工作树可能看不到交接材料。 | 明确交接时需携带实际文件；本轮不代替用户提交或传送任务。 |

### 尚存的设计取舍

1. **同 ID patch 是否真的只是非矛盾补充，主要仍由模型判断。** 程序校验格式、范围和引用，但无法只凭 ID/文本差异证明模型没有把变化误写成补充。首版通过指令约束并观察失败，不增加另一套语义裁决模型。
2. **Top-k 可能漏掉旧记录或更正依据。** 无论写入整理还是回答召回，都不保证完整历史；模型无法使用未召回的证据。实际失败出现后再决定是否调整候选参数或复用其他检索方案。
3. **来源并集不是逐句证据匹配。** 补充记录保留新旧来源，但首版不精确标注正文每个片段由哪条消息支持；不能把 source_ids 存在等同于每句话已经证明。
4. **删除和恢复范围有限。** 明确 ID 的普通记忆删除和单实例串行属于首版范围；准确报告部分成功和未完成计划不等于自动重试或恢复。全副本擦除、完整历史恢复、跨进程幂等与生产接入另行处理。

这些取舍已在本文对应章节说明，属于首版有意采用的边界；没有实跑证据时，不能承诺错误率、改造工时或最终基座质量。若实际失败要求改变核心写入语义，应说明问题并更新本文。

## 实施说明 16：交接入口与接手任务

### 16.1 阅读顺序与文档优先级

1. 阅读仓库 `AGENTS.md`，确认 Python 3.11 和 uv 已存在。
2. 先读实施说明 17 的第二批增量，再按实施说明 2–13 的当前 schema、提示词、写回和召回规则编码；实施说明 15 是前一轮审核的背景记录。
3. 查看[推进计划](agent_long_term_memory_plan.md)的当前决策状态，区分累计工作和下一步。
4. 实现对应适配时查看 [Mem0 审查](agent_long_term_memory_mem0_audit.md)及 [LangMem 审查](agent_long_term_memory_langmem_audit.md)的源码索引，并核对固定版本安装文件。
5. [历史对照](agent_long_term_memory_comparison.md)、[阶段 4](agent_long_term_memory_phase4_evaluation.md)、[场景材料](agent_long_term_memory_scenarios.md)、[检索证据讨论](agent_long_term_memory_retrieval_evidence_draft.md)和 [探针记录](agent_long_term_memory_prototype_eval.md)用于理解取舍；有需要再读，不重新按它们的早期候选扩大首版。

用户的后续明确指示优先；代码的实际运行事实优先于关于运行结果的文字推测。本文规定目标行为，源码审查说明上游原生行为，二者不能互相冒充。实现改变时同步本文与计划，保留历史材料的阶段标签。

### 16.2 当前实际工作状态

| 项目 | 当前状态 |
|---|---|
| 三个项目关键链路的固定版本源码审查 | 已有审查文档与上游固定源码链接；不是完整仓库逐文件审计。 |
| 组件探针 | 已有此前少量运行的文字记录；原临时环境已不在，仓库未归档完整运行包。 |
| 当前首版设计和实施约定 | 本文已记录；实施说明 17 的最小实现已落实到代码。 |
| Mem0 + LangMem 共同运行环境、完整传递依赖锁文件 | 已在 `examples/agent_memory_v1/requirements.lock` 固定；独立 Python 3.11 环境已解析并通过 `uv pip check`。 |
| 本文所定义的第一批记忆模块、提示适配及写回代码 | 已实现于 `examples/agent_memory_v1/`；现有 DB-GPT 观察/执行记录模块不是这里的实现。 |
| 第二批替代写回、普通召回和遗忘 | 最小实现已完成；`superseded_by`、有依据的 `valid_to`、search/get/forget 和失败摘要已接入。真实模型语义仍需扩大观察，生产恢复仍未验证。 |
| 完整记忆链路与效果评估 | 最小写回已做接线检查；真实模型/API、完整召回和整体质量尚未验证。 |
| DB-GPT 主链路接入 | 已完成默认关闭的可选桥接；生产启用、依赖打包和跨进程部署仍未决定。 |

接手任务从实施说明 17 的第二批实现/验证开始，无需恢复整段聊天或再次做三家调研。不能把模型 schema 中的临时替代请求、落库的真实 ID 关系、无外部模型 smoke 和整体记忆质量混为一谈。

### 16.3 是否需要完整上游源码

**运行和复用无需把完整 Mem0/LangMem 仓库复制进 DB-GPT，也无需先 fork 两个框架。** 固定版本 Python 包已经提供可调用模块；实施时应读取安装版本的实际源码，尤其 LangMem 的局部提示适配依赖内部方法。源码可在本地查看，不能只凭 README 或浮动 latest 文档接线。

| 项目 | 运行包 | 源码锚点 | 优先核对的位置 |
|---|---|---|---|
| Mem0 | `mem0ai==2.2.1` | `94c3fe9f238f3dbf29c9ce98643bd71eb13077cd` | `mem0/memory/main.py` 的 raw add、search、get、update、delete；`configs/base.py`、`vector_stores/qdrant.py` 与 provider 配置。 |
| LangMem | `langmem==0.0.30` | `56d85939d80bb731bd5e237567148d817d7bfd16` | `src/langmem/knowledge/extraction.py` 的 MemoryManager、_prepare_messages、_prepare_existing、结果合并和 Store Manager 写回；`utils.py` 的输入格式。 |
| Graphiti | 本轮不作为运行依赖 | `eaa4128681bc53487138a4bbc22d58336ebe70d2` | 需要核对来源/时态思路时按审查索引阅读；无需为了首版启动图数据库。 |

需要更方便地浏览上游示例、说明和依赖时，可以获取两个固定提交的完整工作副本，放在 DB-GPT 之外。以下只是可选获取方式，**本轮没有执行完整 clone**；目录已存在时先检查，不覆盖已有源码工作：

```bash
memory_source_root="$HOME/agent-memory-upstream"
mkdir -p "$memory_source_root"
git clone --filter=blob:none --no-checkout https://github.com/mem0ai/mem0.git "$memory_source_root/mem0"
git -C "$memory_source_root/mem0" checkout --detach 94c3fe9f238f3dbf29c9ce98643bd71eb13077cd
git clone --filter=blob:none --no-checkout https://github.com/langchain-ai/langmem.git "$memory_source_root/langmem"
git -C "$memory_source_root/langmem" checkout --detach 56d85939d80bb731bd5e237567148d817d7bfd16
git -C "$memory_source_root/mem0" rev-parse HEAD
git -C "$memory_source_root/langmem" rev-parse HEAD
```

运行默认使用固定发布包，源码副本用于核对，不默认 editable 安装或修改上游目录。若复制上游实现片段，记录具体文件/提交并保留所需许可信息。LangMem 审查已记录发布 wheel 校验值；实施环境的实际文件仍应重新核对，不能假定临时审查目录永久存在。

### 16.4 第一个实现任务及工程起点

**首版核心的目标是独立的 add、search、get、forget；第一批写入链路和第二批替代写回、普通召回、遗忘入口均落在以下目录，具体约定见实施说明 17。**

```text
examples/agent_memory_v1/
  requirements.in        # 固定两个主包，必要的直接依赖明确声明
  requirements.lock      # 实际解析后生成的共同传递依赖锁文件
  .venv/                 # 独立环境；不作为交接源码提交
  memory_v1/
    __init__.py
    schema.py            # 输入、模型对象、返回上下文
    answer.py            # reference_time 与回答上下文薄适配
    prompts.py           # instructions、输入分区、回答指引
    manager.py           # 固定版本 MemoryManager 局部适配
    store.py             # Mem0 存取、metadata 编解码、scope 校验
    service.py           # add/search/get/forget 编排、结果比较及范围锁
    demo.py              # 可运行入口；不宣称它是质量评测集
```

沿用第一批的 `requirements.in` 和 `requirements.lock`，环境尚在时复用；换机器或环境缺失时，在独立目录用 uv venv / uv pip 按已有锁文件安装。不为第二批重新选型或无理由重新解析依赖，也不修改 DB-GPT 根依赖和整份 uv.lock。Python 使用已安装的 `/opt/homebrew/opt/python@3.11/bin/python3.11`。遇到具体兼容错误时记录版本约束再局部适配；旧临时环境路径不是永久可用的交接保证。

具体配置起点与 SDK 示例见实施说明 13.0/13.1：当前默认使用 deepseek-v4.1-flash / text-embedding-v3（1024 维）、Qdrant 本地持久化与独立 history_db_path；13.1 的 Qwen 示例仅用于复盘历史实验。第一批实现将模型密钥从环境或显式配置读取，不写进代码或运行结果；当前 DeepSeek 组合已完成静态配置切换，真实服务调用和整体稳定性仍需验证。OpenAI 兼容 embedding 客户端使用基础 URL，不能将 DB-GPT 的完整 `/embeddings` 请求地址原样当 base URL。

第一批的环境/配置、schema 编解码、manager 适配和最小写回已经实现；第二批 schema/instructions、替代写回、search/get/forget 与 agent 工具接法也已实现。真实模型/embedding 已完成第一轮写回和回答侧定向观察，带 `reference_time` 的薄回答上下文也已接上；下一步是扩大任务观察。LangMem 不接 Store Manager，Mem0 add 使用 infer=False，不叠加 Mem0 自动提取或另一轮语义冲突分类模型。

第一批已包含最小写回、锁文件、配置和 add 用法；第二批字段、search/get/forget 与自然语言遗忘接法已在同一目录实现。已有 fake/stub 工程检查和真实链路观察；DB-GPT 已完成默认关闭的可选桥接，整体质量判断、生产启用和部署方式仍待决定。

### 16.5 交接文件与工作区状态

本轮查看时，`docs/agent_long_term_memory*.md` 共 10 份文档均为 Git 未跟踪文件，`AGENTS.md` 也未被 Git 跟踪；工作区还有其他任务的代码改动。**同一工作区中的 agent 可直接读取；新 clone、新工作树或另一台机器不会仅因读取当前分支而自动拥有这些文件。** 交接时应携带实际文档和环境说明，按用户选择提交或复制；本轮没有自动提交、创建任务或传送文件。

最小阅读包是本文、AGENTS.md 和推进计划；实现依据包还应包含 Mem0/LangMem 审查。建议一并携带 10 份相关文档保留取舍背景，但不要求接手者全文重读。其他任务的代码修改需保留，不把它们视为记忆实现，也不在记忆任务中顺手清理或回退。

可直接给接手 agent 的任务说明：

```text
请阅读 AGENTS.md 与 docs/agent_long_term_memory_v1_design.md，
以实施说明 16、17 为交接入口，在第一批代码上完成第二批增量。
方向是 Mem0 2.2.1 存取/检索 + LangMem 0.0.30 无状态 manager + 薄适配层。
优先复用开源能力；普通整理只有跳过、新增、更新，遗忘走独立删除。
第一批代码位于 examples/agent_memory_v1/，依赖无需重新选型。
正文保持自包含，模型仅提出被替代旧 ID；程序 ADD 后取真实新 ID，写旧记录 superseded_by。
事实切换日期有依据才补 valid_from/to；未知日期不妨碍明确替代，不使用消息/入库时间补造边界。
补普通 search/get 上下文和范围校验的 forget；示例说明 agent 如何从自然语言请求查到并删除 ID。
避免 Mem0 二次提取，复用 LangMem patch；不新增完整状态机、版本快照、多路检索或图数据库。
第二批字段与入口已经实现；本次仍没有验证真实模型端到端效果。
保存依赖锁文件、运行命令、实际状态及问题；后续先观察链路，再接入 DB-GPT。
保留工作区其他任务的改动；不要重新进行大范围调研或为某个历史问答示例设计专门规则。
```

## 实施说明 17：第二批增量与编码约定（2026-10-09）

**状态：本节约定已在 `examples/agent_memory_v1/` 完成第二批最小实现。** 固定版本真实模型/embedding 端到端效果仍未验证。第一批遗留的 `related_memory_ids` 只做兼容读取，以下是薄适配层的工程约定，不是声称上游原样提供这些字段。

### 17.1 正文、替代关系和来源各保存什么

- `content` 自包含：独立表达主体、事实及必要的时间/限定。状态记忆可以是“项目 A 使用 PostgreSQL”，无需重复“从 SQLite 改为”或添加记忆管理说明。若迁移本身值得记忆，其真实过程仍可作为自包含事件保留，不机械删掉事实细节。
- `source_refs` 保留，记录信息的来源角色、ID 和消息时间；它与记忆之间的替代关系用途不同。
- 旧记录保存单一可空 `superseded_by`，值为真实新 Mem0 ID。`status="superseded"` 可从这个关系推导，不重复存储；不是完整版本系统。
- 停用模型通用 `related_memory_ids` 请求，首版暂不建设其他类型的关联关系；不能将已有通用关联键转换成替代事实。
- 明确纠正仍保留必要事实限定，例如有来源支持的“一直使用 PostgreSQL”；标记替代不保证旧内容曾经为真。若召回依据不足以区分纠正与真实状态变化，回答不能把旧说法当作历史事实。

### 17.2 模型如何请求替代，程序如何得到新 ID

编码起点为实施说明 4 的 `supersedes_ids`，它只存在于本次 manager 请求/结果，不持久化。模型在新对象上引用已经检索到的旧 ID；程序取得 ADD 返回的实际新 ID，再把它存入相应旧记录的 `superseded_by`。旧候选的已存替代标记通过输入 metadata 显示给模型，旧对象回带时临时请求字段为空。

```text
新对象 content/source_ids/valid_from/to + supersedes_ids=[old_id]
  → 校验整批，归并旧 valid_to patch、来源补充及替代请求，暂不写旧记录
  → ADD 新对象，保存返回的 actual_new_id
  → 再读 old_id，核对 scope 和记忆状态
  → 一次 UPDATE 旧 metadata：superseded_by + 有依据的 valid_to；旧来源保持不变
  → 旧正文、原来源、首次 recorded_at 保留
```

这个临时请求是适配 LangMem 工具 schema 的具体接法，不再另写一轮语义分类模型。模型负责判断是否同一主体/情境、是否真正替代；程序只能校验请求结构、ID、范围和可验证时间条件，不能宣称确定性代码证明语义判断正确。

执行时要求：

1. 非空请求只用于由新消息支持的明确已发生状态替代或明确纠正。不同情境、多值可以共存、原因不明的矛盾、计划和仅较晚入库，都不自动替代。历史回填不能因输入较晚就替代已知更新状态。
2. 目标只来自同 scope 的本次旧候选；已有对象不得携带非空 `supersedes_ids`。拒绝未知 ID、自己替代自己、同批多个不同新对象争抢同一旧目标等不明确请求。整批执行前检查已存替代目标，ADD 后再读旧对象；已有其他目标时不覆盖或重新激活。这些读取不是跨进程 compare-and-set，本批仍只保证同实例串行。
3. 非空替代请求的新对象必须作为 ADD 计划；不能因正文与某旧对象相同就把它改为同 ID 的 UPDATE/skip。无替代请求时保留第一批的确定性重复处理。本批新对象的正文、事实时间及替代目标集合相同，可归并来源并共享一个实际新 ID；目标不同且有重叠则拒绝。同一已有 ID 的重复返回仅在归一化后相同才合并，不同 patch 不能按返回顺序覆盖。
4. 被替代旧对象的正文和原来源必须保持原样；旧日期 patch 和 `superseded_by` 在对应新 ADD 成功后一次写回。替代消息属于新对象的来源，也可支持切换边界，但不能并入旧对象的 `source_ids`，否则会让新消息看起来重新证明旧事实。普通补充必须保留已有 `superseded_by`，但不能改写已替代记录的正文。新 ADD 未成功时不提前修改依赖它的旧日期、来源或标记；ADD 成功而旧更新失败时，保留真实新 ID 和未完成动作，定向核对/补写，不能为补关系重新提取或重复 ADD。SDK 报错且是否落库不明时单列结果未知；本批不要求自动重试器或持久恢复系统。
5. 有明确边界才填写 `valid_from/to`；可比较的时间必须校验顺序，不能对不同日期精度或含义直接作字符串排序。时间留空时，已确认的替代关系仍可保存。普通替代不等于 TTL，到期计划不按已发生变化处理。

第二批读取第一批数据时，为缺失 `superseded_by` 使用 null；已有 `related_memory_ids_json` 不参与模型 schema、差异比较或替代判断。新记录不再写这项通用关联字段，旧 payload 可原样保留它，无需批量迁移或清空。来源等仍按当前约定解析；缺少可靠来源的外部记录不能伪装为本原型生成的记录。

### 17.3 普通召回与遗忘的交付范围

- `search` 的 query 来自当前问题/任务，复用 Mem0 公开搜索和可信 scope 过滤。返回正文、来源、观察/记录时间、可用事实时间和替代目标；不全局剔除旧记录，也不自动选择时间最新者。
- `get` 在 scope 校验后返回真实 ID 对应记录，供 agent 按需读取未命中的替代目标；不默认递归展开整条关系链。替代目标缺失时明确报告，不猜当前值；没有标记也不代表候选覆盖完整或事实已经证实。
- `forget` 是内部接口，用户不提供 ID。agent 通过 search/get 确定用户要求的对象，再以 ID 调用范围校验的删除入口；只在对象/范围确实含糊时澄清。提供可运行的工具调用示例，展示自然语言请求到 ID 删除的接线，不提前接入 DB-GPT 主链路。
- 工具 scope 由应用绑定，同一实例中的写入和遗忘共用串行边界。底层按整批 ID 先核对范围；越界/格式错误不开始删除，不存在记为缺失，其余逐项删除并报告成功、失败或结果未知。不得用一次 Top-k 删除宣称清除了整个主题、所有派生记忆或 history/备份。
- `history(ID)` 优先用于正文的 ADD/UPDATE/DELETE 追踪，它保存 old/new 文本，不只是操作名；没有完整来源、有效期和替代 metadata 快照。多次替代可能自然形成 ID 关系，但本批不建设完整版本快照、回滚或通用历史查询服务。

### 17.4 来源依据与本方案适配

| 约定 | 可参考的开源依据 | 本方案新增或限制 |
|---|---|---|
| 自包含记忆文本 | [Mem0 V3 提示词](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/configs/prompts.py)要求自包含事实文本。 | 正文不附加管理关系；事实自身的迁移/纠正限定按价值和原意保留。 |
| 保留旧事实并标记失效 | [Graphiti 冲突写回](https://github.com/getzep/graphiti/blob/eaa4128681bc53487138a4bbc22d58336ebe70d2/graphiti_core/utils/maintenance/edge_operations.py#L538-L573)在冲突和时间条件下更新 invalid_at/expired_at。 | `superseded_by` 和临时 `supersedes_ids` 是我们的薄层字段，不是 Graphiti 原生字段；日期未知仍可记录明确替代，不编造事实结束日期。 |
| 一次整理产生新对象或 patch | [LangMem manager](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/extraction.py)。 | 复用新增、patch 和 schema 校验，实际新 ID 与旧关系写回由程序完成。 |
| 搜索取得 ID，再执行删除 | [LangMem 搜索/管理工具](https://github.com/langchain-ai/langmem/blob/56d85939d80bb731bd5e237567148d817d7bfd16/src/langmem/knowledge/tools.py)、[Mem0 delete](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py#L3562)。 | 底层仍按 ID；自然语言目标识别由 agent 完成，程序独立核对 scope，不要求用户知道 ID。 |
| 正文操作历史 | [Mem0 SQLite history](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/storage.py#L150-L254)。 | 复用已有能力，不宣传成完整 metadata 版本快照或全副本擦除。 |

### 17.5 实现顺序、验收与实际状态

在原 `examples/agent_memory_v1/` 内依次调整 schema/instructions 和 metadata → 替代/有效期写回 → search/get/forget 与工具示例 → 针对性的工程验收。固定依赖、独立环境和开源复用方向沿用第一批。原文档中的大对象模型、完整状态机、图数据库与自建多路检索不因此恢复为任务要求。

工程验收应核对：正文独立可理解；新 ID 来自 Mem0 实际 ADD；未知日期保留 null 但替代关系可用；有依据边界能更新旧有效期且不覆盖正文/来源；替代请求不被旧正文去重吞掉，重复/冲突输出先归并或拒绝；ADD 失败不提前改旧日期/来源/标记；普通补充不误失效；计划、含糊矛盾、不同情境和历史回填不按写入时间自动替代；越界 ID 被拒绝；只命中旧记录时输出替代标记；按需 get 的目标缺失可见；删除不重新激活旧状态；部分失败分别报告已完成、未尝试及结果未知的动作。工具示例应展示自然语言请求取得 ID 的步骤，不能只展示用户手工提供 ID。

用真实模型与 embedding 观察完整链路中的提取、重复、补充、变化、纠正、来源、时间、普通召回和遗忘。第一轮已完成，但工程检查不等于整体质量达标；若后续真实服务/密钥不可用，记录具体未验证项，不能把 fake-store 结果充当真实运行。实际失败再驱动检索或模型适配，不以某一个示例作为整体标准。

本轮第二批代码和 fake/stub 工程验收已执行；2026-10-09 又完成了第一轮真实模型/embedding 组合观察。结果和限制记录在[原型评估](agent_long_term_memory_prototype_eval.md)；更新本文、推进计划和已有 README 即可，不新增文档、不重新做三家调研。

### 17.6 本次文档审核修正（2026-10-09）

本次核对了现有设计/交接、计划、历史材料和两批实际代码，并读取固定版本安装包中的 Mem0 get/update/delete/history。没有运行真实 LLM/embedding；以下问题已在对应正文和代码中修正，不以本节代替算法约定。

| 审核发现 | 依据与影响 | 已修正的约定 |
|---|---|---|
| 写回顺序没有约束旧日期 patch | 原实施说明 9 先按返回对象 update 再 add，与 17 的新 ID 依赖不一致；新增失败可能留下提前结束的旧状态。 | 先计划、校验和新增；旧标记、结束日期及来源归并后写回。 |
| 第一批的去重不能原样用于替代 | `service.py::_validate_and_plan` 会把未知 ID 的相同正文对象转为旧 ID 更新/跳过。 | 非空替代请求单独形成 ADD 计划；明确本批重复/冲突输出的归并与拒绝。 |
| 重试与锁的能力写得过满 | 第一批同 batch_id 仍重新调用 manager，`MemoryWriteError` 没有恢复计划；锁保存在 service 实例内。 | batch_id 只做追踪；补准确部分失败摘要，不要求自动恢复；调用方复用单实例。 |
| 模型请求与持久记录的转换未写完整 | 第一批 `ExistingMemory` 无替代字段，模型对象和 provider metadata 不能直接等同。 | 程序字段独立保存、展示；临时请求不落库；旧通用关联键忽略并保留，无需额外迁移。 |
| 来源时间和过期语义可能误用 | 新来源可能只支持旧状态的结束日期；Mem0 原生 expiration_date 会影响普通搜索。 | 不取最大来源时间判断正文新旧；事实 valid_to 不映射到存储过期。 |
| 历史文档的下一步落后于实际进度 | 阶段 4、探针记录仍写第二批待编码。 | 同步至第一批接线和第二批最小实现完成；保留历史探针的证据边界。 |

修正后，当前材料可用于交接真实模型/embedding 观察和后续集成，无需重做架构调研或建立更多设计文档。2026-10-09 的真实运行已确认 manager 能在部分重复/变化场景中产生可写回结果，但也发现仅靠提示词不能保证事实时间不被消息 `observed_at` 污染，且普通向量召回会把已替代旧记录一起返回。仍需用更广的输入观察回答模型是否正确利用来源、日期和替代标记，并决定事实时间来源校验的最小实现。来源引用不是逐句证明，替代标记也不区分“旧事实曾成立”和“旧说法错误”；纠正限定仍须保存在自包含正文中，依据未召回时不能编造历史事实。以上是实际运行暴露的待修正限制，不是工程 smoke 已证明的效果。

### 17.7 真实模型/embedding 第一轮观察（2026-10-09）

第一轮真实运行使用当前固定版本和独立临时 Qdrant，未修改 DB-GPT 主链路。重复偏好能合并来源，状态变化能先 ADD 新条目再 patch 旧 `superseded_by`；审阅运行结果还发现替代写回不能把新状态的来源并入旧正文，否则会造成错误归属，现已改为旧记录保留原来源。早期替代样本中模型把消息 `observed_at` 复制到 `valid_from`/`valid_to`；收紧“现在/目前”提示后，定向复跑已保持这些字段为 `null`，明确日期和清楚的相对日期仍能被保留/换算。由于当前 schema 只有可空时间字符串，程序仍无法从字段本身证明该值是否有证据。

因此，`observed_at` 仍表示来源消息时间；“现在”默认只作为观察参照，不直接当作事实开始/结束时间。明确日期或可可靠换算的相对日期可以写入事实字段，但仍需扩大样本观察。普通 `search()` 也必须把 `superseded_by` 和时间 metadata 交给回答侧，并由程序同时提供 `reference_time`；不能把向量分数或插入先后当成当前事实裁决。带参考时刻的定向复跑已能在当前/历史问题中正确使用这组 metadata，当前已形成 `answer.py` 的薄上下文接口，但尚未形成完整回答链路或质量评测。

### 17.8 多类型失败修正与当前交接状态（2026-10-09）

用户授权先观察多类型样本，随后授权修正发现的失败。本轮继续使用 Mem0 2.2.1、LangMem 0.0.30、qwen-plus 和 text-embedding-v3/1024，修改范围限定为独立原型。当前没有新增持久化字段、通用分类器、额外语义判定模型或 DB-GPT 接线。下面的实际规则补充并覆盖前文草案中的对应细节。

**LangMem 与提示词：**

1. 第一轮就加入上游 `Done`。Trustcall 有旧对象时要求工具调用，LangMem 默认第二轮才提供 `Done`；原型只做一轮，需要从第一轮提供无变更出口。`Done` 由上游消耗，不落库；保留原对象的行为继续复用上游。
2. `MemoryContent` 新增独立对象；`PatchDoc` 只补充仍正确的对象或来源。同一旧 ID 的正文和来源变更合在一次 patch 中。独立属性分别提取，变化/明确更正新增而不覆盖，非矛盾建议/方案优先补充而不替代，旧纠错语义不能丢。
3. 通过调用输入 `new_source_ids` 构造本次专属的 `MemoryContent` 子 schema，要求所有被工具新增/patch 的结果至少引用一个本轮来源；遗漏时使用 Trustcall 自带的 schema 修复。未修改旧对象由框架回带，不受这个工具输出校验影响。每次构造独立 schema/manager，不修改共享实例的 schema；调用字段不持久化。写回层仍再次校验所有来源与范围。
4. 已有 `superseded_by` 的候选仍展示在消息 metadata 中，但不作为可 patch 的 `existing` 文档；metadata 明示 `editable_memory_ids`、`read_only_memory_ids`。这只限制写入整理的目标，普通 search 和回答上下文仍可返回旧记录。
5. 追加了无价值内容、补充、变化、纠正和保留既有检查/未知状态的工具示例；回答提示要求按原意使用偏好、区分纠正与真实变化、保留时间精度并避免展示冗长内部分析。**这些是提示约束，不是语义保证。**

**写回层的确定性协调：**

| manager 返回情况 | 当前程序行为 |
|---|---|
| 同 ID 的 UPDATE 加上正文和事实时间完全一致、无替代请求的 ADD | 先规划 UPDATE，再合并 ADD 来源，只写一条；不对近义改写做模糊去重。 |
| ADD 明确替代旧对象，同时返回原旧对象或表达同一结果的旧对象 patch | 保留原旧正文/来源；先 ADD，再一次写旧关系/可靠结束日期，不再执行覆盖旧正文的普通 patch。 |
| 替代旧对象，同时给它一个内容不同的 patch | 整批拒绝；不静默丢掉不同信息。 |
| 同一请求同时引用一个已替代历史目标和其可见的当前目标 | 只有沿本次候选可见关系找到终点，且该当前终点也被请求显式引用时，才省去冗余历史目标。只给历史目标、终点未召回或存在循环时拒绝，不猜目标。 |
| 新正文只与历史记录相同 | 不把已替代历史正文当成可更新的当前对象；仍按新对象处理。 |

以上不判断两个自然语言句子是否等价、是否确属同一属性，也不修复缺失的语义依据。非空替代请求仍必须 ADD；ADD 失败仍不提前改旧记录；来源/ID 不合法、不同新对象争抢目标仍在执行前拒绝。普通整理不删除旧记忆。

**验收与交接：**

- `tests/test_write_reconciliation.py`、`tests/test_manager_tools.py` 共 24 项回归通过，包括直接运行上游 Trustcall 的来源修复、Done、JSON patch 和多对象新增路径。
- 原六组样本主跑为 22 次写入/23 次回答，四组关键复跑为 13 次写入/11 次回答；最终两次运行均无调用/写入异常。385 项重复快照中的来源字段一致性检查通过；5 次替代均保留旧正文、来源和首次记录时间。不是 35 次写入或 34 次回答均语义正确。
- 可重复输入、手动 runner 和本轮结果保存在 `examples/agent_memory_v1/tests/`；runner 默认新建临时库，不动用户已有数据。报告见[原型评估](agent_long_term_memory_prototype_eval.md#多类型观察后的最小修正与复跑2026-10-09)。中间失败运行的结果没有被当作验收通过。
- **仍未解决：** 对“此前未知、后来确认”的当前状态，更新成确认后的自包含记忆是允许且通常更适合当前问答的；但当前实现仍不能可靠回答“在较早时间点系统当时知道什么”这类知识过程问题。相同补充仍可能扩散到多个不同主题对象；来源字段正确不等于每个来源都支持整个正文；模型仍可能把仅有日期的事实加上消息时刻，或在回答解释中补出无依据的历史边界。不能因此标记整体记忆效果达标，也不能宣称历史/来源问答已经全部解决。

随后将“明确历史回填/过去时间段/旧记录”提升为提示中的高优先级 ADD 规则，并用六组样本复跑：22 次写入、23 次回答均无调用/写入错误；2025 年 MySQL 独立新增，普通补充仍走 UPDATE。后续优先处理来源粒度和需要保留知识过程的明确场景，不把一次当前问答可接受的 UPDATE 强制改成冗长的历史摘要；当前旧/新召回方式、存储后端和主链路接入安排继续沿用前述约定。

2026-10-10 又运行 7 组措辞变体：3 组历史回填、2 组偏好补充、2 组状态变化，共 14 次写入和 12 次回答，无 API/写入/回答异常。历史回填均独立 ADD，偏好补充均 UPDATE，状态变化均新增当前记录并保留旧记录的 `superseded_by`。这是对当前规则的合成变体观察，不是准确率或完整长期记忆验收；详情见 `examples/agent_memory_v1/tests/observations/20261010_variants.json`。

同日的风险定向观察进一步确认：assistant 建议和工具观察的回答归属基本正确；无明确事实日期的“现在”消息仍可能被模型写入由 `observed_at` 推出的 `valid_from`，后来澄清的环境也不能支持审计型“较早时系统知道什么”查询。按 v1 范围，这两项是已知限制，不增加程序级时间字段校验或点时知识快照；证据见 `examples/agent_memory_v1/tests/observations/20261010_risk_observation.json`。随后转入实际 agent 链路接入，状态见下文。

### 实际 agent 链路接入（2026-10-10）

已在 `packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py` 的 ReAct 主链路增加可选桥接，具体实现位于 `packages/dbgpt-app/src/dbgpt_app/agent_memory/bridge.py`。桥接不复制 Mem0/LangMem 的提取和写回逻辑，只负责把 DB-GPT 的一轮请求映射到原型接口：

1. 回答前以当前 user 文本调用原型 `search()`，使用 `reference_time` 作为本轮服务器观察时间；结构化候选直接追加到当前 agent 的系统上下文。
2. 回答成功后以当前 user + assistant 两条来源消息调用原型 `add()`；已有事件记录中的最近十条 user/assistant 文本作为 `recent_messages`，tool 结果默认排除。
3. scope 有用户标识时使用 `user` scope；匿名请求使用带会话 ID 的 project scope，避免不同匿名用户共享记忆。
4. 记忆检索或写回失败只记录日志，不中断回答；桥接由 `DBGPT_AGENT_MEMORY_ENABLED=1` 显式开启，默认不加载 Mem0/LangMem。

这一步只验证真实入口、输入边界和失败隔离。Mem0/LangMem 依赖仍由 `examples/agent_memory_v1/requirements.lock` 的独立环境提供，没有修改 DB-GPT 根依赖。当前尚未宣称生产部署、跨进程锁、删除副本范围或长期记忆效果已经验收。

### 模型切换与工具调用兼容观察（2026-10-10）

当前配置已从 `qwen-plus` 切换到 DashScope 的 `qwen3.8-max`，embedding 仍为 `text-embedding-v3`/1024。独立记忆服务用新组合完成了偏好、状态替代和历史事实三类真实观察；这证明连接、结构化整理、Qdrant 写入和回答上下文可以跑通，不代表整体准确率达标。

DashScope 的 Qwen3 在思考模式下不接受 LangMem/Trustcall 编辑旧文档时使用的工具选择参数，因此 `MemoryStoreConfig.enable_thinking` 默认是 `False`，可由 `AGENT_MEMORY_ENABLE_THINKING` 显式覆盖。DB-GPT 的 Tongyi 客户端也只对“Qwen3 且本次请求带工具”自动补 `extra_body.enable_thinking=false`；普通无工具对话不改变 provider 默认行为，显式 `extra_body` 优先。一次真实原生工具调用已返回结构化 `echo` tool call。

开启桥接的真实 DB-GPT 请求已完成一次闭环复核。期间定位并修复了两个 DB-GPT 边界问题：代理转换函数会丢失 `tools`/`tool_choice`，以及 OpenAI 风格的 `role="user"` 在内部消息转换器中被过滤；现在分别有回归测试覆盖。修复后的新会话请求能把当前用户消息送到百炼并正确返回“收到”。随后用同一认证范围写入“用户偏好使用 PostgreSQL”后，Qdrant 实际落库 1 条记录；下一次回答前检索召回该记录，日志显示候选被注入系统上下文，模型正确回答“PostgreSQL”。

这证明可选桥接的写入、回答前召回和上下文注入已经在一个真实偏好场景闭环跑通，但不代表整体记忆准确率或生产部署已经验收。DB-GPT 路由会用认证用户 `001` 覆盖请求体中的 `user_name`；端到端测试必须按实际认证 scope 检查数据，不能用请求体用户名推断范围。后续应扩展偏好、状态变化、历史事实、重复、补充和来源/时间等多类型场景，再评估是否调整规则。

随后用同一认证范围做了四个请求的真实小观察：写入 PostgreSQL 偏好、查询偏好、切换到 MySQL、查询当前偏好。四次回答分别为“收到”“PostgreSQL”“收到”“MySQL”；最终有两条记录，PostgreSQL 记录的 `superseded_by` 指向 MySQL。新增的“纯查询/重复确认不产生新记忆”提示生效后，查询轮次没有把问题和 assistant 答案追加进 MySQL 正文。结果保存在 `examples/agent_memory_v1/tests/observations/20261010_dbgpt_e2e.json`。这仍只是一个 scope 的小样本观察，BM25 在该环境中未启用，不能替代整体准确率评测。

第二轮覆盖了历史事实、明确日期、历史补充、重复确认和未确定计划。当前/历史/计划的候选选择基本正确，但“2025 年曾经使用过 Oracle”最初被模型写成完整的 `2025-01-01` 至 `2025-12-31`，回答随之错误扩展为“2025 年全年”。补充“年份级曾经事实不等于全年持续”的提取和回答规则后，定向复跑保存为 `valid_from=valid_to=2025`，回答只保留“2025 年曾经使用过 Oracle”。这修复了本例的过度精确，不代表所有日期表达都已可靠。结果见 `examples/agent_memory_v1/tests/observations/20261010_dbgpt_e2e_round2.json`。
