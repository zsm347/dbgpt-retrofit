# DB-GPT Agent Rollout：会话记录与上下文恢复设计

> 状态：分阶段实施中。**当前工作树已把 `agent_event` append/read、历史投影、ReAct/knowledge Agent 主链路和同会话活动 turn 租约/fencing 接起来**：新请求先写 `turn_started + user_message`；每次模型响应先写 assistant 原始消息（含 native `tool_calls`）；每个工具返回分别写匹配 `tool_call_id` 的真实结果；模型输入从事件投影重建；正常/受控失败写轮次终态。rollout 路径明确使用 native function-call 协议，跳过重复注入的 Gpts ReAct Observation；断开流时会取消并 await Agent 生成任务、释放租约。活动租约放在既有 `chat_history` 行，不新增 `agent_turn`/`agent_stream` 表；每次模型调用、工具启动和事件 append 都校验 fencing token。以上是**代码已实现且已接线**，定向测试通过；临时 SQLite 上的并发和租约测试通过，但尚未用真实 provider endpoint、目标部署数据库或多 worker 环境做端到端验收，故不声称已在生产配置中“实际生效”。跨 worker 保证以相关服务实例共享同一元数据库为前提。本机测试只用临时 SQLite，不连接或修改本机运行库/生产库。旧会话不迁移/不清理；没有执行任何删除。仍待处理：目标数据库建表/事务/锁集成验收、UI 对未完成 turn 的状态呈现、动态 prompt/tool/附件上下文的历史快照策略、数据保留/脱敏/容量策略。未完成项与当前接线证据以 §11.10 为准；§11.8/§11.9 是之前阶段的时间点记录。源码基线：DB-GPT `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`；当前实现仍在该基线之上的未提交工作树；对照 Codex `7a0e974e08c798d1e8d59d407aeb6e24db1313af`。实现差距的逐场景证据另见[主链路评估清单附录 D.7](DB-GPT-主链路评估与改进清单.md#d7-会话记录实现落点审计第-4-轮2026-09-28)。

## 1. 目标与术语

本设计探究的是：为 DB-GPT 建立一份**数据库承载、按序追加、可重建模型会话上下文**的权威记录，语义上对标 Codex rollout。这里的“可恢复”第一阶段指：进程重启后可从已提交记录重建后续模型交互所需的会话历史；**不**表示恢复 Python 进程、工具执行现场或外部副作用。

- **Rollout / 会话事件记录**：保存用户输入、模型实际返回的消息、结构化工具调用及其结果、必要的轮次终态和上下文基线变化。
- **Agent trace**：记录 span、耗时、调用栈/错误和全量运行观测，服务于可观测性；与本设计的权威会话记录分开。
- **聊天 UI 历史**：供界面展示的用户/最终回答/步骤投影，可由权威记录派生或继续单独维护；不能反过来当作完整模型交互记录。
- **检查点 / 工具重放**：运行中断后从工具副作用处继续、自动重试或重放工具；属于后续阶段，本期不实现。

## 2. 已确定边界与第一阶段建议决策

### 已确定

1. 采用**一张有序事件表**作为候选权威记录；不新增 `agent_stream`、`agent_turn` 两张表。`turn_id` 可以是事件行上的分组字段，不因此成为单独表。
2. 目标是 Codex rollout 的记录语义与恢复能力，不照搬 JSONL 文件格式，也不把所有 SSE chunk、span、runtime callback 全塞进事件表。
3. UI 历史、Agent trace、会话事件日志是不同职责。不能将 `GptsMessages` 的部分 action_report 或 `StorageConversation` 的最终 UI payload 单独宣称为权威 rollout。
4. 第一阶段只重建已持久化的模型交互上下文；不做运行检查点，不自动重放任何工具。
5. 基线沿用 DB-GPT 配置的元数据数据库。JSON payload 本身不构成迁移 PostgreSQL 的充分理由；是否使用各方言原生 JSON 类型、是否需要 PostgreSQL JSONB，须由查询/索引需求和方言测试决定。
6. 旧会话不迁移到新事件日志；切换时从空的新会话开始，旧会话记录按明确的 cutover 范围清理。此处是目标策略，不代表本轮已删除本机或任何环境的数据。

### 本轮建议收敛的三项设计决策

| 决策 | 第一阶段建议 | 边界与理由 |
|---|---|---|
| 恢复精度 | **恢复有序的 Chat Completions 对话协议历史**：持久化用户消息、模型实际返回的 assistant 消息（包括原始 tool_calls）、逐调用工具结果/错误及轮次终态。新请求的 system/developer prompt、工具 schema 和动态资源仍按当时配置重建；第一阶段**不承诺逐字节复现过去每次 provider 请求**。 | 这是“可续聊/可恢复会话历史”，不是“精确请求审计/复现”。DB-GPT 当前会动态组装 prompt 和 tools（`base_agent.py:1380-1451`；`tool_calling_agent.py:458-485`）。若要精确复现，应另行批准保存完整请求快照及其隐私、保留和容量策略，不能把本阶段的恢复描述成精确重放。💭 |
| 未完成 tool_call | **不得因一个工具调用缺结果而跳过整个用户轮次。**恢复应保留该轮 user message、assistant 原始 `tool_calls` 和所有已持久化的真实 `tool_result`。对缺结果的单个 call，按 Codex 的思路在模型输入副本中补一条匹配 `tool_call_id` 的 prompt-only 文本 tool result；不写回权威事件，也不自动重跑旧工具。 | 这是逐调用历史补齐。Chat Completions 的 `role=tool` / `tool_call_id` / 文本 `content` 就是标准消息形状，OpenAI 官方 schema 和示例已有说明（[Chat API schema](https://developers.openai.com/api/reference/cli/resources/chat)、[Function calling](https://developers.openai.com/api/docs/guides/function-calling)）。当前纯投影选择 Codex 的 `aborted` 文本；它不表示硬崩溃后的真实副作用状态已知。第三方 provider 差异可做常规集成测试，不是开始实现的阻塞。💭 |
| 顺序与并发 | 对同一 `conv_id` 实行**单活动轮次**；相同会话的并发用户请求必须排队或明确拒绝。一个轮次内仍可并行执行多个工具，但每个完成结果都经事务序号分配器串行 append；`event_seq` 由唯一约束 `(conv_id, event_seq)` 兜底。 | 不允许无锁 `MAX(event_seq)+1`。事件分配器只串行化短暂的数据库 append，不应在模型/工具执行的长时间内持有数据库事务。序号分配和同会话轮次互斥的 SQL 实现必须覆盖 SQLite 及部署配置支持的元数据数据库；不为此新建 `agent_stream` 或 `agent_turn` 表。具体方言锁/原子更新方案留在实现设计和集成测试中验证。💭 |

上述为设计推荐，不代表已由用户批准具体 schema。实施前仍须评审数据保留/脱敏、payload 大小上限、访问控制、旧会话清理范围和具体方言锁实现；逐调用恢复投影用确定性单测验证即可进入实现，第三方 provider 行为列入常规集成覆盖。是否对副作用工具增加幂等/审批是工具执行策略问题，不是 tool-result 文本能否接入协议的问题。无论如何，恢复逻辑都不得跳过整轮用户请求。

## 3. 当前实现基线（已验证差距）

详细证据按五个场景列在清单 D.7；这里保留与设计直接相关的结论。以下代码行均指上述 DB-GPT commit。

| 场景 | 当前写入/读取行为 | 对恢复的影响 |
|---|---|---|
| 普通文本轮次 | `agentic_data_api.py:2332-2357` 先保存会话对象，再把本轮用户消息加到内存中的 `StorageConversation`；`history_steps` 也在内存收集，终止时才保存 history payload（`:2999-3003,3566-3581`；`core/interface/message.py:1342-1357`）。 | 中途进程中断可能丢失本轮用户消息和已流出的步骤；下一轮从 UI 历史提取旧 human/ai 与 `view.final_content`，不是中断现场（`agentic_data_api.py:2334-2356`）。 |
| 单个 native tool call | 原始 `tool_calls` 暂存在 Agent 实例（`tool_calling_agent.py:451-490`）；单工具结果只回填工具名，不保留 call_id/原始参数字符串（`tool_action.py:308-322`）。部分继续执行的中间回复在工具执行后才经 self-send 进入 GptsMessages；terminate 路径不走同一写入（`base_agent.py:740-756,993-1046`）。 | 工具执行前没有权威调用记录；不能原样恢复 native 调用协议。 |
| 并行工具 | `asyncio.gather` 等所有工具返回后构建一个聚合 ActionOutput（`tool_action.py:324-395`）。 | 已完成工具的结果可能等不到聚合提交；结果数组按输入次序，不表达真实完成次序。 |
| 工具错误/重试 | 异常变成失败 ActionOutput；运行时可将 observation 交给下一轮，但并非逐调用写入权威日志（`tool_action.py:169-251`；`base_agent.py:730-756`）。 | 进程中断后无法稳定重建“错误结果—模型重试”完整顺序。 |
| 副作用后、结果落库前崩溃 | 成功操作快照在动作之后的记忆写入阶段产生（`core/role.py:357-405,408-465`），不是调用前日志。 | 只能知道调用可能未决，无法证明副作用是否已发生；自动重放可能重复副作用。 |

现有测试覆盖 SSE 终态容错、工具聚合/部分失败和数据库文本类型；尚未发现“逐事件写入—模拟进程崩溃—新进程重建模型输入”的集成验收。参见 `test_agentic_data_api_lifecycle.py:18-44,87-141`、`test_parallel_tool_action.py:74-88,146-167`、`test_gpts_messages_db.py:8-19`。生产环境 tracer 后端是否额外持久化模型/工具数据，待核实；不能用 tracer 行为替代本设计的数据库证据。

## 4. 候选事件语义（不是最终 schema）

第一阶段建议按可恢复的模型协议交互而不是按 Agent 内部函数调用建模：

1. `user_message`：收到的用户输入，在发起模型请求前提交。
2. `assistant_message`：一次模型实际返回的 assistant 消息。若含 native function call，保存有序 `tool_calls`，包括 call ID、函数名和原始 arguments JSON 字符串；必须先提交，再执行工具。
3. `tool_result`：每个工具调用各一条，带匹配的 `tool_call_id`、结果内容和成功/错误标志。单工具返回即写；并行时每个完成项各自写，不等整批聚合。
4. 后续 `assistant_message`：保存模型收到工具结果后的下一次响应，直到回答或下一批工具调用。
5. 轮次生命周期：`turn_started`、`turn_completed`、`turn_aborted` 均在同一事件表中记录。它们用于从持久化事实判断一轮是否开始、正常完成或被应用明确中止；不是运行检查点，也不记录全量 span/tracer 事件。最终 assistant 消息先提交，随后提交 `turn_completed`。若进程突然退出而没有机会写终态，只能判为“未完成/终态未知”；不得伪造 `turn_aborted`，也不得据此认定未决工具没有产生副作用。

Chat Completions 的历史重建应符合它的消息关系：assistant 的 `tool_calls` 与每个 `role=tool` 消息通过 `tool_call_id` 配对；不把 DB-GPT 的聚合 ActionOutput 冒充 provider 原始响应。中断恢复投影产生的占位 tool message 必须在内存/模型输入副本中标记为 synthetic，不能落成声称真实执行成功/失败的权威 `tool_result`。Codex 本地源码同样将 `FunctionCall` 与 `FunctionCallOutput` 分开持久化，并按策略筛选 lifecycle 事件（Codex `protocol/src/models.rs:878-929`、`rollout/src/policy.rs:8-20,37-58,85-104`）。OpenAI 官方 Chat Completions schema/example 展示了 assistant call ID 与 `role=tool.tool_call_id` 的配对，且 result `content` 为字符串（[Chat API schema](https://developers.openai.com/api/reference/cli/resources/chat)、[Function calling](https://developers.openai.com/api/docs/guides/function-calling)）；因此标准协议映射明确，不能把第三方兼容端点尚未逐个实测误说成这个消息形状本身存疑。

### 候选逻辑字段

| 字段语义 | 用途 | 决策状态 |
|---|---|---|
| `event_id` | 行级主键/事件写入幂等键 | 已实现 UUID 字符串主键；调用者重试须复用同一 ID |
| `conv_id` | 会话关联/隔离键 | 已实现；读取仍须由上层执行用户/租户授权校验 |
| `event_seq` | 会话内严格追加顺序；唯一约束 `(conv_id, event_seq)` | 已实现；短事务分配；**不等价于同一会话 turn 互斥** |
| `turn_id` | 将用户请求、模型响应、工具结果归于一轮 | 已实现为事件行分组字段；不建 turn 表 |
| `event_type` | 事件类别 | 已实现首期六类白名单 |
| `payload` | 该事件的类型化内容，可序列化为 JSON | 已实现 JSON 文本；MySQL 映射 `LONGTEXT` |
| `schema_version` | payload 演进和历史读取兼容 | 已实现，当前写入版本为 1 |
| `created_at` | 排障/审计时间，不代替 `event_seq` 排序 | 已实现 UTC 默认时间 |

表名 `agent_event`、上述字段和 `(conv_id,event_seq)` 唯一约束已形成首版实现，不代表已通过生产 schema 发布评审。不要用时间戳或自增主键隐式代替会话内顺序；数据库写入顺序由明确序号和事务语义保证。DAO 支持复用同一事件 ID 重试；客户端重发整轮请求是否去重，仍需由 API request-idempotency 契约另行明确。

## 5. 第一阶段写入与恢复流程（候选）

### 写入顺序

```text
接受用户请求
  └─ append+commit turn_started → user_message（同一事务，按此顺序分配 event_seq）
       └─ 调用模型
            └─ append+commit assistant_message（含完整 tool_calls）
                 ├─ 无工具：append assistant final → append turn_completed
                 └─ 有工具：逐个执行
                      └─ 每个工具返回后 append+commit tool_result
                           └─ 下一次模型响应继续按上述顺序记录
```

写入与副作用不可能仅靠应用数据库事务原子化：工具调用和其结果之间仍可能崩溃。顺序要求是：用户输入提交失败则不调用模型；assistant tool_calls 提交失败则不执行工具；每个工具完成后立即提交真实结果，再允许下一次模型调用消费它。若工具副作用已发生但结果未能提交，该 call_id 在日志中保持未决。恢复时必须保留整轮及所有已记录事实；仅对缺结果的 call 在模型输入副本中按 call ID 补齐 prompt-only 文本 tool result，不写回权威事件，也不自动重放旧工具。标准 Chat Completions 消息形状可直接承载它；用确定性投影单测验证，不将第三方 provider 逐个预测作为实现门槛。

### 恢复顺序

1. 按 `conv_id` 读取已提交事件，按 `event_seq` 排序并校验序号/版本。
2. 将事件转换成 Chat Completions 可接受的规范化 messages；按 call ID 验证工具调用和结果配对。
3. **保留未完成轮次，不按 `turn_completed` 截掉整轮。**将该轮用户输入、原始 assistant tool_calls 和已提交的真实工具结果都投影到恢复上下文。若某个 call_id 没有真实结果，只在模型输入副本中为该 call 补齐带匹配 ID 的 synthetic 文本 tool result；不得写成真实 `tool_result`，不得补造 `turn_aborted`，也不得重跑旧工具。UI 同时标出该 call/轮次中断且结果未知。用确定性单测检查投影结构、call_id 配对和 synthetic 项不落库；第三方 provider 差异由常规集成测试覆盖。
4. UI 的**基础对话文本**可从 `user_message`、最终 `assistant_message` 投影；完整的旧 ReAct UI（steps、task_plan、generated_images、sub_agents、input_files、citations）目前不在 v1 协议事件 schema 内，不能宣称可无损重建。按当前切换决定，旧 UI 历史不迁移并在明确 cutover 范围内清理；新 UI 只展示新 rollout 能投影的内容。Agent trace 继续独立记录。要让事件日志成为完整 UI 展示的唯一来源，须另行评审有界的用户可见事件种类，不能默认把所有运行时 span 塞进 rollout。

**精度承诺**：恢复的是有序的对话协议历史，不是完整 wire-request 回放。当前 DB-GPT 在 `base_agent.py:1380-1451` 动态构造 system/user prompt、历史和压缩上下文，native tools 在 `tool_calling_agent.py:458-485` 动态构造；第一阶段新请求沿用当前 prompt/tool 构造配置。因此动态 system/developer 内容、工具 schema 和模型参数的历史版本不会由事件 transcript 自动复原。若验收要求“能证明当时模型收到的全部字节”，必须增加每次模型请求快照及相应隐私/容量策略，属于单独扩展，不得把两种保证混为一谈。

### 崩溃后恢复的是会话，不是原 turn（Codex 对照；第一阶段建议）

这里的“恢复”必须区分**恢复会话**与**恢复同一轮执行**。按本地 Codex 的实际行为，第一阶段建议采用前者：重开会话只重建 rollout 历史，不自动重发崩溃前的用户请求、不恢复工具进程，也不自动执行旧工具。用户之后提交的新输入才开启新 turn；若用户输入“继续”，这也是一个新 turn，只是它建立在包含未完成旧 turn 的历史上。

| 触发/事实 | 权威事件记录 | 派生状态与行为 |
|---|---|---|
| 正常运行 | `turn_started` 及本轮协议事件；最终 assistant message 后提交 `turn_completed`。 | 本轮为 `running`，完成后为 `completed`。 |
| 程序观察到受控取消/中止 | 若中止事实已成功提交，记录 `turn_aborted`；不把未观察到的情况写成事实。 | 本轮为 `aborted`/`interrupted`，按终态原因展示。 |
| 进程硬崩溃 | 已提交事件保持不变；若没有终态，就没有 `turn_completed` 或 `turn_aborted`。 | 只有确认旧执行者已不再活动后，读取投影才把该轮显示为 `incomplete`/`interrupted`；不伪造终态事件。 |
| 恢复同一会话 | 读取并重建所有已提交事件，包括未完成旧 turn 的 user、assistant tool_calls 和已有真实 tool_result。 | 恢复动作本身不重新提交旧 turn、不发起旧 turn 的模型采样、不运行旧工具；旧 turn 仍是未完成状态。 |
| 恢复后用户提交新输入 | 追加新 `turn_started` 和新 `user_message`，使用新的 `turn_id`；旧 turn 的历史行不改写。 | 新 turn 的模型输入包含旧 turn 的完整已知历史、对旧 turn 未决 call 的 prompt-only 配对项，以及新用户输入。旧工具不重放；模型之后若发出调用，那是新调用。 |

**重要边界**：硬崩溃后没有足够事实证明旧工具“没执行”或“已取消”；缺结果 call 的权威状态是 `unresolved/unknown`。Codex 的 prompt normalizer 用 `aborted` 文本补齐 Responses item；DB-GPT 当前纯投影器也采用匹配 `tool_call_id` 的 `role=tool` 文本 `aborted`。它只存在于返回的模型输入副本，不改变旧 turn 的事件日志状态，也不是工具结果的持久化记录（实现位置见 §11.8）。

#### 恢复占用与旧执行者隔离（DB-GPT 方案，不是 Codex 已有保证）

恢复新 turn 前，必须确认同一 `conv_id` 的旧执行者已退出，或已被可靠 fencing；不能只因“等了一段时间”就默认旧 worker 不会继续写入。当前工作树已在 `chat_history` 会话行实现租约和递增 fence：活动租约期间拒绝新 turn；过期后新执行者取得更高 fence；事件 append、租约续期、每次模型请求和工具启动均校验所有权。默认租约 90 秒、每 30 秒续期。SQLite 行为有测试；非 SQLite 目标库和实际多 worker 部署仍须集成验证，详见 §11.10。

数据库 fencing 只能阻止旧 worker 继续提交事件，不能撤销已经发往外部系统、且不识别该 fencing token 的工具副作用。第一阶段仍不自动重放旧工具；对存在外部副作用且执行结果可能未知的工具，若业务要求进一步保证，须另做工具级幂等、状态查询或人工确认设计。不得将“数据库锁已释放”表述为“外部副作用一定没有发生”。

## 6. 验收测试草案

- **普通文本**：请求被接受后立刻检查 user event 已提交；在模型响应前杀进程；新进程能恢复该输入而不是静默丢失。
- **单工具调用**：保存完整 assistant tool_calls；校验原始参数、call_id、工具结果及 `tool_call_id` 配对；新进程重建出的 Chat Completions 消息结构有效。
- **并行工具**：两个工具按相反完成顺序返回；每个结果分别落行，序号体现事件追加顺序，call ID 匹配正确；只完成一个工具后模拟崩溃，已完成结果仍存在。
- **未完成调用恢复**：模拟 assistant 返回两个 tool_calls、其中一个已有真实结果、另一个未决；重启后输入保留该轮用户问题、原始 assistant tool_calls 和真实结果，只为未决 call 生成带正确 `tool_call_id` 的 prompt-only 文本占位。断言没有整轮丢弃、占位不写入权威事件、旧工具不自动执行，UI 标明中断/未知。该确定性用例是实现验收；真实 provider 兼容属于常规集成测试。
- **会话恢复与 turn 状态转换**：T1 写入 `turn_started`、用户输入、assistant tool_calls 和部分真实结果后模拟硬崩溃；仅执行会话恢复时，不得重新提交 T1 的用户输入、采样模型或执行 T1 的旧工具，也不得给 T1 补造 `turn_aborted`。随后提交新用户输入，必须产生不同于 T1 的 T2 `turn_id`；T2 请求保留 T1 全部已知历史，只对缺结果的具体 call_id 做 prompt-only 配对，并包含新的用户输入；T1 在 UI/read projection 中显示未完成/中断。
- **旧执行者 fencing**：owner epoch 为 e1 的 worker 失去会话所有权后，e2 已取得该会话；验证 e1 不能再 append rollout 事件或续租。此测试只证明数据库写入 fencing，不声称可以撤销已经开始的外部工具副作用。
- **失败与重试**：错误结果先落盘，再记录后续 assistant 响应；重建后能看见失败和重试，不把工具错误抹成最终回答文本。
- **副作用窗口**：stub 工具先增加外部持久计数再模拟崩溃；重启后呈现未决调用且计数不再次增加，证明第一阶段没有自动重放。
- **权威写入失败**：用户输入 append 失败时确认 provider 未被调用；assistant tool_calls append 失败时确认工具未执行；工具结果 append 失败后不得把缺失结果当成功继续下一次模型调用。
- **终态/写入失败**：分别模拟模型错误、历史投影失败和事件 append 失败；验证权威事件完整性与 UI SSE 行为各按约定处理。
- **数据库并发/方言**：同一轮内并行工具的多个结果并发完成/append 时不产生重复 `event_seq`，每个结果都可按最终 append 序号恢复；同一 `conv_id` 的第二个并发用户轮次按约定排队或拒绝；在项目支持的元数据数据库上验证建表、JSON payload 序列化、事务回滚和升级兼容。

## 7. 实施阶段建议

- **阶段 1（已实现）**：`agent_event` 单表 append/read、会话内 `event_seq` 分配、幂等与失败回滚；有序事件到 Chat Completions messages 的投影；缺结果的具体 call 在模型输入副本中补 `tool_call_id` 对应的 `aborted` tool message。临时 SQLite 定向测试覆盖 DAO 和投影。
- **阶段 2（代码已接线，未做真实环境端到端验收）**：ReAct/knowledge API 在用户输入、assistant 响应、逐工具结果和轮次终态边界写事件；native function-call provider 输入从 rollout 重建；rollout 模式不再将 GptsMessages Observation 重复拼进 prompt；SSE 断开会取消并 await Agent 子任务。具体实现/测试证据见 §11.10。
- **阶段 3（同会话互斥已实现，SQLite 验证通过）**：在既有 `chat_history` 行保存 lease owner、过期时间与单调递增 fence；活动租约期间拒绝第二个用户 turn，worker 定期续租，过期后可接管；每次 provider 调用、工具启动和事件 append 校验 fence。lease/fencing 只防止失权 worker 继续推进/写事件，不能撤销已启动的外部工具副作用。具体证据见 §11.10。
- **当前剩余验收门**：MySQL/OceanBase 等目标部署方言未跑事务/锁/DDL 集成测试；尚无真实 provider 故障注入与整条 API 重启恢复验收；UI 还没有依据事件日志展示 incomplete/interrupted 状态；附件/动态 prompt/tool schema 不在历史请求快照内。上线前逐项补验，不把 SQLite 单测或“代码已接线”当作生产通过。
- **本期明确不做**：运行检查点、恢复 Python 进程、自动重放任何旧工具。硬崩溃留下的未完成 turn 保留在日志；用户发起新 turn 时仍能看到完整旧 turn 和逐 call 占位结果。
- **后续独立阶段**：如业务要求继续执行中断的副作用工具，再评估幂等键、执行对账、人工确认和工具租约，决定是否需要 runtime checkpoint；它不是 rollout 表自然附带的能力。

> **核心判断**：目标是数据库版 rollout，不是泛化 runtime event collector。权威表只保存协议事实；未完成轮次整体保留，只对具体缺结果的 call 在模型输入副本中配对，不自动重放工具。旧会话不导入；实际清理仍须另行限定范围并明确执行，本轮没有删除任何数据。当前实现与剩余验收项以 §11.10 为准。

## 8. 落地前兼容性核对（2026-09-28）

本节把“设计看起来可行”与“当前仓库/目标 provider 已验证可用”分开。源码证据均针对 DB-GPT `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`；这轮没有连生产数据库、没有发真实 provider 请求，也没有改代码。

### 8.1 数据库：沿用项目元数据库，不因 JSON payload 改用 PostgreSQL

- **项目现状（源码证据）**：默认元数据库是 `pilot/meta_data/dbgpt.db` SQLite；配置文字列出 SQLite、OceanBase、MySQL（`packages/dbgpt-app/src/dbgpt_app/config.py:332-340`）。仓库另有 PostgreSQL datasource connector，且初始化入口接受通用 `RDBMSDatasourceParameters`（`packages/dbgpt-ext/src/dbgpt_ext/datasource/rdbms/conn_postgresql.py:30-63`；`packages/dbgpt-app/src/dbgpt_app/base.py:65-88`），所以不能说“代码完全没有 PostgreSQL connector”。但 metadata 配置说明/错误信息只列 SQLite、MySQL、OceanBase；这不足以证明 PostgreSQL 元数据库端到端受支持。
- **项目现状（源码证据）**：SQLite 启动时执行 `db.create_all()` 和 Alembic 升级；非 SQLite 路径要求手工维护 schema，初始化代码仅为 MySQL/OceanBase 特判建库（`base.py:132-163,176-206`）。当前未找到 PostgreSQL 元数据库迁移/初始化验收；现有大文本方言测试对 PostgreSQL 只验证 SQLAlchemy 编译结果为 `TEXT`，不等同于完整 metadata DB 集成测试（`packages/dbgpt-serve/src/dbgpt_serve/agent/chat/tests/test_gpts_messages_db.py:8-19`）。OceanBase 的实际 schema 发布流程也须核实。
- ⚖️ **权衡取舍**：JSON 是 `payload` 的应用层序列化格式，不要求数据库必须原生存 JSONB。当前大文本列已采用 `Text().with_variant(LONGTEXT(), "mysql")`（`packages/dbgpt-serve/src/dbgpt_serve/agent/db/gpts_messages_db.py:18-26`）。候选实现可沿用“JSON 序列化到 TEXT；MySQL 用 LONGTEXT variant”的跨方言思路，把 `event_type`、`conv_id`、`event_seq` 等需要过滤/索引的字段单独建列。若将来确实要对 payload 内部字段做高频查询/索引，再以查询需求和各方言集成测试决定是否用原生 JSON 类型；目前没有仅为 JSON 而迁移 PostgreSQL 的证据或必要性。
- **实现状态更新（2026-09-29）**：首版 DAO 复用既有 `chat_history` 行作短 append 事务的序号互斥锚点，并在该行保存活动 turn 的租约/fence；SQLite 使用 `BEGIN IMMEDIATE`，其他 SQLAlchemy 方言走 `SELECT ... FOR UPDATE`。SQLite 临时库已测 append 并发、活动 turn 拒绝、到期接管、旧 fence 拒写与租约续期；MySQL/OceanBase 实库锁语义尚未集成验证。详见 §11.10；§11.9 保留的是实施前审计快照。

### 8.2 Chat Completions：标准消息映射已明确，provider 实测属于集成覆盖

- **已验证的本地实现与接线路径**：`ModelMessage` 有 `tool_calls` 与 `tool_call_id` 字段（`packages/dbgpt-core/src/dbgpt/core/interface/message.py:184-204`）；`to_common_messages()` 将 assistant 调用转成 `role=assistant, tool_calls=[...]`，将工具结果转成 `role=tool, tool_call_id=...`（同文件 `:508-534`）。OpenAI client 确实调用 `chat.completions.create(messages=...)`（`packages/dbgpt-core/src/dbgpt/model/proxy/llms/chatgpt.py:345-372`）；LiteLLM 路径也将消息转换后交给 `litellm.acompletion`（`packages/dbgpt-core/src/dbgpt/model/proxy/llms/litellm.py:231-266`）。这是代码可表达/已接线证据。
- **官方协议证据**：Chat Completions 的 tool message 字段是 `role`、`tool_call_id`、`content`；官方示例以普通字符串作为结果内容（[Chat API schema](https://developers.openai.com/api/reference/cli/resources/chat)、[OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling)）。Moonshot 和 Qwen 官方材料也展示常规的按 `tool_call_id` 回填（[Moonshot Tool Calls](https://moonshot-ai.gitbook.io/moonshot-ai/getting-started-guide/use-kimi-api-for-tool-calls)、[Qwen Function Calling](https://help.aliyun.com/en/model-studio/qwen-function-calling)）。因此 synthetic 文本占位在标准 Chat Completions 消息形状上没有特殊协议障碍。
- 🔴 **已验证差距（测试覆盖）**：既有 serializer 测试 30 passed，但没有覆盖 call/result 配对恢复；本轮也没有向真实 endpoint 发恢复请求。后一项是运行集成尚未测，不代表标准消息形状不确定，也不构成开始实现的门槛。synthetic 项仅可作为 prompt projection，不能误写进权威日志；它也不能提供 exactly-once 副作用保证。
- 💭 **集成覆盖（非前置阻塞）**：仓库 profile 包含 OpenAI、Kimi、Qwen、MiniMax、GLM 和 custom OpenAI-compatible endpoint（`packages/dbgpt-core/src/dbgpt/cli/_profiles.py:87-165`）。各实际部署 provider 的具体方言差异可在常规集成测试中验证；标准 Chat Completions 消息是否有 `tool_call_id` 与文本 `content` 不必先按 provider 矩阵做 PoC。

### 8.3 并发：事件排序与轮次互斥是两个约束（实施前快照）

> 本小节的“尚未选定”记录截至 2026-09-28 的调查结论；租约/fencing 后续已实现，现状以 §11.10 为准。

- **数据库并发事实（官方文档）**：SQLite 同一时刻只允许一个写事务；官方文档说明 `BEGIN IMMEDIATE` 可提前取得写事务位置，但与其他 writer 冲突时会返回 `SQLITE_BUSY`（[SQLite Transactions](https://www.sqlite.org/lang_transaction.html)、[SQLite Isolation](https://www.sqlite.org/isolation.html)，访问于 2026-09-28）。MySQL InnoDB 可用短事务和 `SELECT ... FOR UPDATE` 对已索引行加锁（[MySQL InnoDB locking reads](https://dev.mysql.com/doc/refman/8.0/en/innodb-best-practices.html)，访问于 2026-09-28）。这说明序号方案必须按方言设计，不能假设一种锁 SQL 到处可用。
- ⚖️ **设计约束**：`UNIQUE(conv_id, event_seq)` 只能防止重复序号，不能单独阻止同一个 `conv_id` 的两个用户轮次交错。序号分配应在短数据库事务里完成；不能为了“轮次互斥”从调用模型前到工具结束都持有事务。一个轮次内工具仍可并行，只需每个已完成结果各自短事务 append。
- 💭 **当时尚未选定的实现方案**：可评估（A）在现有会话行增加活动轮次协调字段，（B）单独的 DB 锁/租约机制，或（C）明确限制部署拓扑并由上层队列串行化。后续已选择复用 `chat_history` 行保存 lease/fence，不新增事件表；其 SQLite 测试与非 SQLite 集成限制见 §11.10。

### 8.4 本轮结论与下一项决策

🔴 **已验证差距**：本地测试覆盖不到工具调用/结果配对后的跨进程恢复；真实 provider 的端到端恢复请求也未测。标准消息形状已有官方 schema/example 支持，缺少真实 endpoint 验收属于常规集成覆盖，不是恢复设计的前置阻塞。

**项目环境事实**：本机当前 profile 实际指向 Qwen Plus + SQLite；仓库支持 PostgreSQL datasource connector，但没有核实到 PostgreSQL 元数据库完整建表/迁移与验收链路。OceanBase 和非 SQLite schema 升级也仍待部署核实。

⚖️ **当前合理取舍**：第一阶段继续沿用 DB-GPT 已配置的元数据库，并用可移植的 JSON 文本 payload；把有序事件 transcript 与 Agent trace 分开，不新增执行检查点，也不自动重放工具。

**实现状态更新（2026-09-29）**：事件 schema/SQLite DAO、纯投影、Agent 主链路接线和同会话活动 turn 租约/fencing 已落工作树；payload 大小/脱敏/保留、目标方言和真实 provider/API 端到端恢复仍未完成。§8.1 的数据库支持描述是前一轮只读核对，不能代替新 DAO 的方言集成测试。当前权威实施状态以 §11.10 为准，恢复语义以 §11.6 为准。

## 9. 本机配置与同会话并发策略（第 6 轮，2026-09-28，实施前快照）

本节只记录当前开发环境与仓库调用路径；本机 profile 不代表生产部署拓扑。源码基线仍为 DB-GPT `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`。

### 9.1 已核实的本机配置

- `uv run dbgpt setup --show` 输出 active profile 为 `qwen`，配置位置为 `~/.dbgpt/configs/qwen.toml`。只读取非敏感字段后确认：元数据库 `type=sqlite`、路径 `pilot/meta_data/dbgpt.db`；模型 `qwen-plus`、provider `proxy/tongyi`、API base 为 DashScope compatible-mode。API key/password 没有读取或输出。
- 这说明当前本机设计验收应首先以 **SQLite + Qwen Chat Completions** 为基线；不能据此假设线上也用 SQLite、单实例或相同模型。
- Qwen 官方工具调用示例会把 assistant `tool_calls` 原消息和逐个 `role=tool` / `tool_call_id` 结果追加到 Chat Completions messages 后再请求模型（[Qwen Function Calling](https://help.aliyun.com/en/model-studio/qwen-function-calling)，访问于 2026-09-28）。这是常规真实工具结果的官方协议证据；synthetic/unknown 结果仍需要真实 Qwen endpoint 集成验收。

### 9.2 现有同会话并发行为

- 🔴 **已验证差距（主链路无整轮互斥）**：`/v1/chat/react-agent` 是 async streaming endpoint（`packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py:3835-3862`）；每个请求会创建 `run_agent()` task（`:2980-2990`）。入口没有按 `conv_id` 持有整轮锁；同时 `REACT_AGENT_MEMORY_CACHE` 是进程内字典，并按 `conv_id` 共享 `GptsMemory`（`:72,2310-2319`）。因此现有代码不能证明同一会话的并发请求会被排队或拒绝，可能同时读取旧历史并交错写入。这里指出的是控制措施缺失，不声称已经复现数据损坏。
- **现有协调锚点（源码证据）**：请求在模型任务启动前先创建并保存 `StorageConversation`（`agentic_data_api.py:2321-2333`）；`ConversationServe` 将其映射到 SQLAlchemy `chat_history`（`packages/dbgpt-serve/src/dbgpt_serve/conversation/serve.py:90-115`；`packages/dbgpt-core/src/dbgpt/storage/chat_history/storage_adapter.py:22-48`）。`chat_history.conv_uid` 有唯一约束（`packages/dbgpt-core/src/dbgpt/storage/chat_history/chat_history_db.py:25-40`）。所以已有会话行可作为技术协调锚点候选，但当前没有事件序号计数或活动轮次字段。
- **部署边界（源码证据）**：仓库内置 `uvicorn.run()` 调用未设置 `workers` 参数（`packages/dbgpt-app/src/dbgpt_app/dbgpt_server.py:258-276`）；本轮没有检查线上进程管理器、容器副本或是否显式多 worker。进程内 `asyncio.Lock` 只能保护单个进程，不能单独作为多 worker/多副本的互斥保证。

### 9.3 第一阶段的建议约束（尚待确认）

> **历史快照说明**：本小节记录租约实现前的决策建议；单活动 turn 与会话行 fencing 已按该方向落地，当前代码及测试状态以 §11.10 为准。

- **先区分两种并行**：本建议限制的是**同一 `conv_id` 的两个用户 turn 同时运行**；不是禁止一个 assistant turn 中并行执行多个 tool call。不同会话仍可并行，同一 turn 中的并行工具也不因此改变。
- ⚖️ **第一阶段建议语义**：默认一个会话同一时刻只处理一个用户 turn。两个请求若同时基于旧历史独立生成，结果没有天然的先后/合并语义，可能让工具操作和回复交错。若产品以后确实需要同会话并行，应另行定义分支/合并语义，而不是只给事件排号。
- 💭 **冲突请求处理建议**：优先在 UI 禁用该会话的重复提交；服务端再做权威保护。遇到活动 turn 时建议立即返回冲突（例如 HTTP 409），而非静默排队；具体 API 响应形式仍是产品契约，可调整。
- 💭 **可继续推进的互斥方案**：以“所有承载同一会话的服务实例共享同一个元数据库”为设计前提，使用数据库原子条件更新/行级锁领取 `chat_history` 中该 `conv_uid` 的活动 turn 状态；这比进程内 `asyncio.Lock` 更适合跨 worker。事件事实仍只写 `agent_event`，`chat_history` 上的锁/序号字段只是协调元数据，不是第二份 rollout。若实际部署不是共享元数据库（例如每个副本各有独立 SQLite 文件），则数据库锁也无法跨副本协调；这属于部署验收，不阻塞当前逻辑设计。
- 💭 **事件序号建议**：可复用已有 `chat_history` 行增加 `event_seq_head` 作为分配器（它只负责计数，事件事实仍只写 `agent_event`）。每次 append 在一个短事务内原子递增该值并插入 `agent_event`，两步一起提交/回滚；`UNIQUE(conv_id,event_seq)` 做最后防线。SQLite writer 会串行化写事务；官方资料说明 `BEGIN IMMEDIATE` 可先取得写位置但可能遇到 `SQLITE_BUSY`（[SQLite Transaction](https://www.sqlite.org/lang_transaction.html)、[SQLite Isolation](https://www.sqlite.org/isolation.html)，访问 2026-09-28）。MySQL 可在同一会话行使用 `SELECT ... FOR UPDATE` 后递增并插事件（[MySQL InnoDB Best Practices](https://dev.mysql.com/doc/refman/8.0/en/innodb-best-practices.html)，访问 2026-09-28）；OceanBase 需按实际兼容模式补测。
- 💭 **中断后的占用清理仍须单独定义**：`turn_started` 无终态可从 rollout 判断为不完整，但这不自动证明持有占用的 worker 已死亡。单 worker 重启、多个 worker 滚动重启和网络分区的处理不同；不应为此自动重放工具。当前先推进互斥/记录语义，具体 stale-owner 清理策略在目标数据库集成设计中验证。

> **本轮判断**：本机 SQLite + Qwen 只作为跑通 PoC 的测试配置，不作为生产架构假设。可以按“一个会话一个活动用户 turn、并行工具调用仍允许、共享元数据库负责互斥和序号分配”继续细化单表 rollout；线上是否共享数据库以及 stale-owner 清理属于部署/集成验收，不是当前逻辑设计的阻塞项。

## 10. `agent_event` 单表评审规格（第 7 轮）

> **状态**：这是供评审的逻辑规格，不是已批准 DDL，也没有修改代码或数据库。目标只对标 Codex rollout 的**有序、可恢复会话记录**；不把 Agent trace、运行检查点或工具重放塞进来。源码基线：DB-GPT `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`；Codex `7a0e974e08c798d1e8d59d407aeb6e24db1313af`。当前缺口证据见总清单 D.7；Codex 生命周期事件持久化证据见 D.7 中的 `rollout/policy.rs:8-20,37-58,85-104`。

### 10.1 一行代表什么

`agent_event` 的**一行代表一个已提交、不可原地改写的会话记录事实**。事实只包括模型交互协议所需内容和轮次生命周期标记；不是“每个函数调用一行”、不是每个 SSE chunk 一行，也不是所有运行时事件的收集桶。UI 历史可从这些记录投影；可观测性 trace 继续走独立系统。

### 10.2 逻辑字段与约束

| 字段 | 逻辑类型/约束 | 用途与规则 |
|---|---|---|
| `event_id` | UUID 类稳定 ID；主键 | 每个逻辑事件在首次写入前生成；数据库重试沿用同一 ID。若冲突，校验既有行内容一致后才能视为幂等成功，不能无条件忽略冲突。 |
| `conv_id` | 会话键，非空 | 所有读取先做用户/租户授权校验。它与既有 `chat_history.conv_uid` 的映射、外键和删除策略须在 DDL 评审中确定。 |
| `event_seq` | 会话内单调递增整数，非空 | 唯一权威排序；唯一约束 `(conv_id,event_seq)`。不要求序号无空洞；禁止用时间戳排序或无并发保护的 `MAX+1`。 |
| `turn_id` | UUID 类分组键，非空 | 将一次用户请求及其模型响应、工具结果、终态归到同一轮；它是普通列，不是 `agent_turn` 表。 |
| `event_type` | 应用层封闭枚举，非空 | v1 候选值：`turn_started`、`user_message`、`assistant_message`、`tool_result`、`turn_completed`、`turn_aborted`。扩展事件须同步定义 payload 和重建语义。 |
| `schema_version` | 正整数，非空 | 标识该事件 payload 的结构版本；历史读取器须按版本解析，不能静默把未知版本当空消息。 |
| `payload` | JSON 对象；跨方言可用 JSON 文本序列化 | 只存该事件类型的业务载荷；不用 payload 内字段承担排序/会话过滤。原生 JSON/JSONB 不是必要前提。 |
| `created_at` | UTC 时间戳，建议非空 | 供排障/审计；它不是会话顺序依据，也不能替代 `event_seq`。 |

**最小索引**：主键 `event_id`；唯一索引 `(conv_id,event_seq)`。是否另建 `(conv_id,turn_id,event_seq)`，须由实际查询和目标数据库计划验证后决定，避免先行堆索引。第一阶段不加 `event_type` payload 内部索引。`request_id`（整轮 API 重试去重）暂不列为必需字段：事件写入幂等和客户端重发整轮去重是两件事；后者须先定义 API 契约。

### 10.3 v1 事件与 payload 边界

| 事件 | payload 最少内容 | 写入时点 / 能回答的问题 |
|---|---|---|
| `turn_started` | 可选的请求来源/非敏感关联信息；不放完整 prompt | 服务端接受该用户轮次时。告诉读取者该轮进入处理；它不是执行检查点。 |
| `user_message` | 实际用户消息内容及恢复所需的附件引用/结构 | **模型调用前**提交；回答“这一轮用户实际提出什么”。附件只记稳定引用和必要元数据，不复制文件正文，具体安全策略待评审。 |
| `assistant_message` | 模型实际返回的规范化 assistant message；保留 `content` 及完整 `tool_calls`：call ID、name、原始 arguments JSON 字符串；若接口提供，可记录 `finish_reason` | **任何工具执行前**提交；回答“模型实际返回了什么”。不把它改写成 DB-GPT 的聚合 ActionOutput。 |
| `tool_result` | `tool_call_id`、工具名、实际回给模型的结果内容、成功/失败状态 | 每个工具返回后分别提交；失败也是结果。并行工具按各自完成并成功落库的顺序追加，不等聚合对象。 |
| `turn_completed` | 可选的最终 assistant 事件 ID/结束原因；不重复复制最终回答正文 | 最终 assistant message 已先提交之后写入；明确终态，便于恢复器和 UI 判断这一轮正常结束。 |
| `turn_aborted` | 应用已知的中止原因类别及未决 call ID（如可确定）；不塞堆栈/全量 trace | 仅在服务确实观察到取消或失败、并成功持久化终态时写入。硬崩溃没有机会写它，不能在恢复时伪造。 |

**刻意不设的事件**：v1 不单列 `tool_call_started` / `tool_call_finished`（调用及其结果已由 assistant `tool_calls` 和 `tool_result` 表达；缺结果即未决），不把 `action_output_created` 聚合对象重复写成权威事实，不记录 token chunk、span、SQL 执行栈或通用 Agent callback。后几类属于 trace/运行观测，不属于本次 rollout 的恢复边界。Codex rollout 也是按策略持久化协议 item 与部分生命周期事件，而非无限量采集所有 UI/runtime 消息（Codex commit `7a0e974e08c798d1e8d59d407aeb6e24db1313af`；总清单 D.7 的源码位置）。

### 10.4 写入不变量与事务边界

1. **接受请求先落日志**：在同一事务内先追加 `turn_started`、再追加 `user_message`，发模型请求前一并提交；失败则不调用模型。
2. **调用先于副作用持久化**：模型返回含 `tool_calls` 的 assistant message 后，先完整 append 并提交，再允许执行任何工具；该写入失败则不执行工具。
3. **逐结果落库再续模型**：一个工具结果返回并提交后，才允许下一次模型请求消费它；多工具并行时逐项提交，不以整批聚合写入替代逐项事件。
4. **终态最后提交**：最终 assistant message 先落库，`turn_completed` 后落；受控取消/错误仅在能确证时落 `turn_aborted`。进程硬退出留下“开始但无终态”是合法可观察状态，表示 incomplete/unknown。
5. **单会话顺序**：同一 `conv_id` 的用户 turn 不交错；一个 turn 内工具仍可并行；不同会话可并行。每次事件 append 只持有短数据库事务，不能把模型/工具的长耗时包在 DB 事务里。
6. **事件与序号原子提交**：分配 `event_seq` 与插入 `agent_event` 在同一事务提交/回滚。DAO 锁既有 `chat_history` 会话行后读取 `MAX(event_seq)+1` 并 append；SQLite 以 `BEGIN IMMEDIATE` 获取短写锁，其他方言用行锁。该行也保存单活动 turn 的 lease/fence，事件 append 会校验当前 owner token；跨 worker 保证仍以服务实例共享同一元数据库为前提，且非 SQLite 方言尚未集成测试。不能把 SQLite 单测表述成生产多实例验收，也不能用进程内锁冒充跨 worker 保证。
7. **append 重试幂等**：同一个事件的存储重试复用 `event_id`；只有发现已有事件内容完全一致才算成功。一次整轮 HTTP 重试是否复用 `turn_id`，由未定的 request-idempotency 契约解决，不能靠 `event_id` 自动推导。

### 10.5 恢复器与投影的责任

- 恢复器按授权后的 `conv_id` 读取**已提交**事件，按 `event_seq` 校验版本并重建 Chat Completions messages；assistant tool call 与 tool result 按 `tool_call_id` 配对。恢复的是已记录的协议历史，不是历史 system/developer prompt、tools schema、模型参数或完整 HTTP 请求快照。
- `assistant_message.tool_calls` 中没有对应 `tool_result` 的调用属于未决。当前按 §11.6 的纠正方向：保留整轮，在模型输入副本中只为缺结果的具体 call 补匹配 `tool_call_id` 的文本 `role=tool` 消息；不写回权威事件，不自动重放旧工具。该消息形状符合 Chat Completions 协议；provider endpoint 差异是常规集成测试。Codex 的 prompt normalization 同样逐 call 补 `aborted`，但不提供模型后续新调用的通用 exactly-once 防护。
- 事件日志可投影基础 user/assistant transcript，但当前 v1 事件没有覆盖旧 UI payload 中的 steps、task_plan、generated_images、sub_agents、input_files、citations，不能声称可无损重建完整 UI。旧 UI 历史在会话切换时按用户选择清理，不要求为 rollout 保留或迁移。Agent trace 独立记录耗时、span、栈/指标；不能用 trace 替代事件日志。旧 `GptsMessages` 工作记忆不迁入 `agent_event`，其清理/隔离见 §11.2。
- payload 会承载用户输入、工具参数及数据库结果，可能含敏感数据；访问控制、保留/删除、脱敏、体积上限和审计要求必须在 DDL/上线评审前定，不以“append-only”为由绕过删除合规。

### 10.6 第一阶段验收矩阵

| 场景 | 必须验证的结果 |
|---|---|
| 普通文本 + 两轮续聊 | 每条输入/assistant 输出按序提交；新 DB session 从事件重建出相同 role/content 顺序。 |
| 单次 native tool call | assistant 原始 call ID/name/arguments 在工具执行前已存；工具结果以匹配 ID 独立存；恢复后的 Chat Completions message 结构配对正确。 |
| 同轮并行工具 | 结果反序完成时仍逐个落库；中途崩溃只保留已提交结果；不存在重复 `event_seq` 或整批 ActionOutput 冒充原始结果。 |
| 崩溃注入 | 分别在用户事件提交后、assistant call 提交后、工具副作用后/结果提交前、结果提交后/下一次模型请求前、最终回答提交后/终态提交前退出；重启只重建记录，不自动执行工具。 |
| 写入失败 fail-closed | user event 写失败→provider 未调用；assistant call 写失败→工具未执行；tool result 写失败→下一次模型请求不消费未持久化结果。 |
| 生命周期 | 受控完成有 `turn_completed`；受控中止可有 `turn_aborted`；硬崩溃无终态被判 incomplete/unknown，不补造事件。 |
| 同会话并发 | 两个用户 turn 同时到达时按约定拒绝/排队，不能交错；不同 conv 并行；同轮并行工具仍有效。分别在目标元数据库/多实例共享 DB 条件下测。 |
| 恢复投影 | 确定性测试验证缺失 call 只补一条匹配 `tool_call_id` 的 prompt-only 文本 tool message，整轮及其他真实结果保留、synthetic 不落库、不重放旧工具；目标 provider 的 endpoint 差异作为常规集成测试。 |
| 旧历史边界 | 不导入 legacy transcript；新 rollout 会话从空历史开始。明确 cutover 时清理哪些旧会话行及关联 GptsMessages，并验证新 conv 不会读到旧记录；本设计文档不执行删除。 |
| UI 投影 | 切换后 UI 展示新事件日志覆盖的内容；旧 rich UI 历史按已选策略清理，因此不承诺迁移后仍可展示旧 steps/附件/引用。 |

### 10.7 分类结论与评审阻塞项

- 🔴 **已验证差距**：当前 checkout 没有 `agent_event` 权威表，也没有“事件逐条写入—进程中断—新 session 重建模型输入”的集成测试（DB-GPT commit `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`；总清单 D.7）。
- ⚖️ **权衡取舍**：用一个有序会话日志恢复协议历史；UI 历史是投影，Agent trace 独立；v1 不保存完整 provider 请求快照、不收集所有 runtime span、不实现执行 checkpoint 和工具自动重放。沿用项目元数据库、payload 用 JSON 文本，是跨方言方案，不表示 PostgreSQL 不可用或已验证。
- 💭 **设计建议（待评审）**：`agent_event` 作为唯一事件事实表；同一会话单活动用户 turn，轮内并行工具可用；在既有会话行协调序号与活动 turn；恢复必须保留整个未完成用户 turn，缺结果按单个 `tool_call_id` 处理，不得截掉整轮；工具不自动重放。
- **可先开始的无副作用切片**：实现“事件序列 → Chat Completions messages”的纯投影和确定性测试，包括本节定义的硬崩溃恢复状态转换；它不依赖最终 DDL、生产部署拓扑或真实 provider。
- **接数据库与 Agent 主链路前的原始阻塞项（历史计划）**：① 对 v1 事件/payload、逐调用写入点和 fail-closed 行为完成评审；② 选定验收数据库上的 DDL、事务序号分配、同会话互斥及旧 owner fencing；③ 明确 rollout 路径禁用/隔离 GptsMessages 回填；④ 定义真实持久化数据的权限、大小限制、脱敏和保留规则。当前完成/未完成状态已不同，按 §11.10 验收；真实 provider 行为仍须集成覆盖。
- **上线/cutover 门槛（不阻止纯投影或本地测试）**：生产元数据库方言/迁移、多实例共享存储和 owner fencing 的集成验收；旧会话清理的精确范围、时点、备份/回滚；生产数据访问控制及删除/保留策略。旧会话不迁移的产品决定已明确，但执行清理前仍须明确实际数据范围。

> **本轮范围**：只收敛设计文档与总清单的证据索引；没有改代码、数据库、配置或测试，也没有批准任何 DDL。

### 10.8 Codex rollout 源码对照与 DB-GPT 适配决策（2026-09-28）

> **对照基线与方法**：DB-GPT commit `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`；本地 Codex `codex-rs` commit `7a0e974e08c798d1e8d59d407aeb6e24db1313af`。本节只核对这两个 checkout 的代码路径，不代表其他 Codex 版本或线上运行遥测；本轮未运行 Codex/DB-GPT 测试。表中“源码事实”与“DB-GPT 建议”分开，建议不等于已批准 schema。

| 待决设计点 | Codex 源码实际做法（证据） | DB-GPT 的适配结论 |
|---|---|---|
| **记录边界：rollout 是不是全量运行时事件？** | 不是。`RolloutItem` 区分模型 `ResponseItem`、协议/界面 `EventMsg`、`Compacted`、`TurnContext`、`WorldState`、`SessionMeta` 等（Codex `protocol/src/protocol.rs:3211-3226`）。持久化策略对 ResponseItem 和 EventMsg 分别筛选：工具调用/结果等协议 item 可保存，`TurnStarted/TurnComplete/TurnAborted` 被选中，但大量 delta、warning、runtime callback 明确不持久化；LocalThreadStore 的 append 路径确实调用该策略（Codex `rollout/src/policy.rs:8-20,37-58,85-182`；`thread-store/src/local/live_writer.rs:280-308`）。 | 💭 **建议**：维持一张 `agent_event` 作为会话协议事实表，只记录 v1 中已列明的 user/assistant/tool/lifecycle 事件；不要把所有 SSE、trace span、Agent 内部状态收进来。Codex 的 item 类型不是 DB-GPT 必须复制的事件枚举。 |
| **工具调用和结果何时记录？** | 模型产出的 function call 先经 `record_completed_response_item()` 进入会话记录，再创建/排入工具 future（Codex `core/src/stream_events_utils.rs:296-326`）。每个已完成的工具 future 随后由 `drain_in_flight()` 转成 ResponseItem 并写回历史（`core/src/session/turn.rs:2096-2113`）。本地 thread store 对 append 先套持久化策略，再调用 recorder 写入和 flush（`thread-store/src/local/live_writer.rs:280-335`；JSONL 单条序列化、写入并调用文件 `flush`：`rollout/src/recorder.rs:1902-1927`）。 | 💭 **建议**：采用相同的因果顺序：完整 assistant tool call 先提交，之后才执行工具；每个真实工具结果单独提交，之后才允许下一次模型请求消费它。Chat Completions 中 assistant 的 `tool_calls` 是一条 assistant message 内的数组，结果则逐条以 `tool_call_id` 配对；不要把 DB-GPT 聚合 `ActionOutput` 当作原始工具响应。 |
| **写入失败是否 fail-closed？** | **不能据此声称有 fail-closed 保证。** 虽然调用顺序是先 append/flush 再排工具，`Session::persist_rollout_items()` 对 append 错误只记录日志、不向调用方返回错误；`record_conversation_items()` 也不返回持久化状态（Codex `core/src/session/mod.rs:2993-3020,3311-3318,3628-3635`）。因此源码证明“尝试并等待该写路径”，不能证明“若落盘失败工具一定不执行/模型一定不继续”。文件 `flush()` 也不等价于本设计尚未定义的数据库事务/硬件同步承诺。 | 💭 **建议（DB-GPT 应更严格）**：把事件库作为权威来源时，数据库 commit 错误必须传回控制流：user append 失败不调模型；assistant tool-call append 失败不执行工具；tool_result append 失败不发下一次模型请求。这里是 DB-GPT 的目标可靠性约束，不应写成“Codex 已证明如此”。 |
| **顺序、序号和并发写者如何处理？** | JSONL 记录行带时间戳；新 paginated history 还带单调 ordinal，legacy 模式可没有 ordinal（Codex `protocol/src/protocol.rs:3406-3413`；`rollout/src/ordinal.rs:24-52`）。live recorder 通过单一后台 writer channel 排队；本地 thread store 另有按 thread 的文件 writer lock（`rollout/src/recorder.rs:926-937,1774-1804`；`thread-store/src/local/writer_lock.rs:39-86`）。该文件锁保护同一 rollout 文件的 writer 所有权，**不能等同于 DB-GPT 的同会话用户 turn 互斥，也不能直接证明多主机部署的数据库锁语义**。 | 💭 **建议**：DB-GPT 使用数据库短事务为 `(conv_id,event_seq)` 分配顺序并 append；`event_seq` 表示已提交事件的规范顺序，不用时间戳或工具完成时间代替。单会话活动用户 turn 互斥与轮内并行工具结果 append 是两个问题；轮内仍允许并行工具，结果按成功提交顺序编号。具体 SQLite/目标方言的原子分配策略仍须设计和测试，Codex 的本地文件锁不能替我们选 SQL。 |
| **轮次开始、完成、中止事件的用途？** | `TurnStarted/TurnComplete/TurnAborted` 在策略中被选为持久事件（Codex `rollout/src/policy.rs:98-104`）；`send_event_raw_with_persistence()` 将 EventMsg 包成 RolloutItem 交给持久化路径，再向客户端投递（`core/src/session/mod.rs:2061-2088`）。RegularTask 发起始事件，任务结束路径发完成/受控中止事件；中断路径还显式尝试 flush（`core/src/tasks/regular.rs:45-56`；`core/src/tasks/mod.rs:781-809,911-950`）。它们是轮次状态事实，不是模型消息，也不是工具执行检查点；硬崩溃可能来不及写终态。 | 💭 **建议**：DB-GPT 可以把必要 lifecycle marker 作为同一 `agent_event` 表中的非模型可见事件；其用途是识别已开始/正常完成/受控中止及 UI 状态，不新增 `agent_turn` 表。硬崩溃留下无终态的 turn 应保持 incomplete/unknown，不伪造 `turn_aborted`。 |
| **重启时遇到缺失的工具结果怎么办？** | Codex 从 rollout 重建协议历史，并以最近的 `Compacted.replacement_history` 为基线重放其后的 suffix（Codex `core/src/session/rollout_reconstruction.rs:139-185,317-373`）。发模型请求时使用 `clone_history().for_prompt()` 的副本；规范化器为缺失 output 的 function/custom call 补文本为 `aborted` 的合成 output，既不写回 rollout，也不启动旧工具执行（`core/src/session/turn.rs:334-340`；`core/src/context_manager/history.rs:141-146,325-343`；`core/src/context_manager/normalize.rs:20-95`）。 | ⚖️ **DB-GPT 边界**：绝不因一个未决 call 跳过整轮；保留该轮输入、原始 tool_calls 和真实结果。对缺少结果的单个 call，在模型输入副本中补一条匹配 `tool_call_id` 的文本 tool result，不写回权威事件、不自动重跑。协议结构明确；用确定性测试验收投影，provider 方言差异放在常规集成测试。模型可能再次生成同类新调用不属于 Codex normalization 已解决的 exactly-once 问题。 |
| **上下文压缩记录是不是运行检查点？** | Codex 的 `CompactedItem` 可保存 `replacement_history` 及 context-window 链接信息；重建器用它重建模型历史基线，再处理后续 rollout 项（Codex `protocol/src/protocol.rs:3246-3263`；`core/src/session/rollout_reconstruction.rs:139-185,317-362`）。这是**模型上下文历史基线**，不保存工具进程/外部副作用现场，不能称为工具执行 checkpoint。 | 💭 **建议**：一期 `agent_event` 仍以原始协议事件为权威，不因 prompt 压缩而改写/删除历史事实；新请求可在重建副本上运行当前上下文预算策略，但不承诺复原过去完全相同的 prompt。持久化 `replacement_history` 属于后续的上下文压缩 checkpoint 决策；若产品要求节省重建成本或稳定压缩基线，再单独设计其来源、版本和恢复语义。 |
| **UI 投影、权威历史和 Agent trace 是否一份数据？** | 本地 Codex 把 rollout JSONL 定义为可脱离 SQLite 读取/修复的 durable replay format，SQLite state DB 是可查询索引/投影；工具运行 trace 另由 `rollout_thread_trace` 路径产生（Codex `thread-store/src/local/mod.rs:88-100`；`rollout-trace/src/thread.rs:216-248`）。 | ⚖️ **职责取舍**：DB-GPT 的 `agent_event` 对标权威 rollout；UI 可由它生成基础对话投影，Agent trace 仍单独做耗时/span/错误观测。数据库表布局无需复制 Codex 的 JSONL + SQLite 双存储。 |

#### 本轮对照后仍未决的事项

- **已由源码澄清**：Codex 选择性持久化协议 item 与部分 lifecycle event；工具 call/output 分开记录；其上下文压缩基线不是工具执行 checkpoint；本地 rollout writer 的锁不是 DB-GPT 的多方言并发答案。
- **💭 仍待评审的通用写入/并发建议**：事件 append 必须 fail-closed；`event_seq` 代表数据库成功提交顺序；同一会话只允许一个活动用户 turn，轮内工具并发保留。恢复语义不再列为未决：§11.6 已确定整轮保留及逐 call 未决结果约束；§11.7 记录了 Codex 的会话恢复行为，并给出 DB-GPT 新 turn 继续的第一阶段建议。新 turn 的 turn-id、旧 turn 派生状态与 owner fencing 仍须按实现契约验收。
- **尚需形成具体实现规格**：assistant/user/tool payload 的最终 JSON schema 与大小/脱敏规则；SQLite 测试下的序号分配事务以及同会话冲突 API 行为；事件 append 错误如何映射到 SSE/HTTP；新 rollout 路径如何确保不再从 GptsMessages 读取重复 ReAct memory。旧数据精确清理范围、生产方言与部署验收是上线门槛，不是本地 stub 测试的事实，也不应冒充 Codex 源码已解决的问题。

> **结论**：Codex 对照能把“该借什么/不该照搬什么”讲清楚，但不能替 DB-GPT 决定数据库事务与数据治理。本矩阵收敛了源码证据和适配建议，没有批准最终 DDL、没有改代码、数据库、配置或测试；进入实现仍须把上列 DB-GPT 决策写成明确契约。

## 11. 旧历史切换与中断工具调用的方案比较（第 8 轮）

> **范围与版本**：只读核查旧聊天历史的实际读取/写入、ReAct 工作记忆恢复和工具结果协议边界，并比较迁移及中断恢复方案。DB-GPT commit ca9f014cb3ead157ca2ee6ce645658f5fb55138c；不代表线上部署状态。本节是建议稿，不是已批准迁移合同。

### 11.1 代码事实：旧历史里实际有什么

| 观察 | 已核实行为与证据 | 对新 rollout 的含义 |
|---|---|---|
| 续聊读取入口 | ReAct 请求用 dialogue.conv_uid 创建 StorageConversation，构造时从 storage 加载既有消息；随后遍历 get_history_message()，将 human/ai 内容纳入历史，并从 view JSON 提取 final_content；将序列传给 generate_reply（agentic_data_api.py:2296-2333,2334-2357,2980-2986；message.py:1095-1116,1317-1318）。 | 当前 ReAct 续聊看到的是“用户文本 + AI 最终文本”的投影，不是原始 provider request/tool-call transcript。 |
| 旧存储形态 | chat_history 保存 conv 元数据和 message_ids；chat_history_message 保存带 index/round_index/message_detail 的 JSON 消息（chat_history_db.py:25-55,63-90）。adapter 仍兼容旧版内嵌 chat_history.messages，并恢复其中消息对象（storage_adapter.py:27-73,95-134,137-155）；StorageConversation 按 message IDs 加载后恢复 index/round（message.py:1342-1357,1369-1402）。 | 这是当前旧存储的事实形态；按已选策略不导入这些记录，切换清理范围需显式涵盖相关旧表。 |
| 历史角色 | API 按存储消息类型收集 human/ai/view，但生成模型消息时当前 BaseAgent 又按序号奇偶分配 HUMAN/AI（agentic_data_api.py:2338-2356；base_agent.py:1413-1425）。 | 新事件必须显式写 role；旧会话不迁移，因此无需为切换设计 legacy 序列角色猜测。 |
| Rich UI payload | 成功/错误终态的 view payload 含 final_content/citations/steps/task_plan/generated_images/sub_agents/input_files（agentic_data_api.py:996-1028）；终态保存失败时仍会发送 SSE final/done（agentic_data_api.py:1046-1062），测试明确覆盖该行为（test_agentic_data_api_lifecycle.py:18-44）。 | v1 协议事件本来也不足以重建完整旧 UI；按已选策略旧历史直接清理，不承诺迁移后恢复旧步骤/附件/引用。 |
| GptsMessages 工作记忆 | Agent 初始化按 conv/role 查询记录；`GptsMemory.get_agent_history_memory()` 每两行配成一组，只取第二行的 `action_report`（base_agent.py:245-259；gpts_memory.py:152-179）。`ActionOutput.memory_fragments` 被恢复为短期记忆片段，ReActAgent 再把它们转成 Question/Thought/Action/Observation 消息并拼入模型输入（role.py:472-484；react_agent.py:274-364；base_agent.py:1346-1351,1427-1428）。 | 它是 ReAct 的派生工作记忆，不是原始 provider transcript，也不是 Agent trace；内容会与 tool result 在语义上重叠，但不能替代带 call ID/arguments 的 `tool_result`。更关键的是，目标 API 的记录子集和“两行一组”恢复假设不匹配，详见 §11.5。 |

### 11.2 旧会话切换策略：不迁移，切换时清空

用户已明确选择：**旧会话不导入新 rollout；切换后从空的新会话开始，旧会话记录清理即可。** 因而上一版提出的 lazy bootstrap、`history_baseline_imported`、legacy transcript 事件及 UI 旧历史保留方案均不再是目标方案。

| 范围 | 切换策略 | 边界 |
|---|---|---|
| 新 rollout 会话 | 使用新的会话身份，从空 transcript 开始；所有后续输入、模型消息和工具协议结果只写入新 `agent_event`。 | 不读旧 `StorageConversation` 来补模型历史，不制造 `legacy_projection` 事件。 |
| 旧聊天历史 | cutover 时清理选定范围内的 `chat_history` 与关联 `chat_history_message`，不做逐会话 bootstrap 或全库回填。 | 旧 UI 历史及其 steps/附件/引用不再承诺可恢复。此处是目标切换策略；实际删除前仍须限定环境、conv 范围、备份/回滚和执行窗口。本轮没有执行删除。 |
| 旧 ReAct 工作记忆 | `GptsMessages` 不迁入 `agent_event`。若“清空旧会话”要求彻底清除旧对话数据，清理任务还应涵盖这些旧 conv 对应的 GptsMessages；否则至少保证新 conv 不会按旧 conv_id 读取它们。 | 它与聊天 UI 历史是不同存储，不能因清理 chat_history 就假定其已被删除。rollout 首期建议关闭其跨请求回填；活动 turn 的工具结果由协议 transcript 提供，不额外注入重复的 `Observation:` memory。旧库回填与实时协议历史是两回事，详见 §11.5。 |
| 切换验证 | 清理后创建全新 conv，验证 UI 历史和模型输入均为空；提交第一条新消息后只从 `agent_event` 重建。 | 旧 conv 不自动续聊；用户若要保留旧内容，须在切换前自行另行保存，不由 rollout migration 代办。 |

**对方案的影响**：无需处理旧 transcript 的角色猜测、坏 JSON bootstrap、基线水位与迁移幂等；但“清理”是有破坏性的部署操作，必须由明确的 cutover 执行计划限定目标。本设计只记录用户选择，不会因此直接删除本机数据库或其他环境的数据。

### 11.3 Codex 如何恢复中断的工具调用

> **对照版本**：本地 Codex `codex-rs` commit `7a0e974e08c798d1e8d59d407aeb6e24db1313af`。这是源码 checkout 的实现审计，不是对所有 Codex 产品运行时内部行为的独立观测。

1. **先持久化模型工具调用，再执行。** Codex 收到模型 `FunctionCall` 后，先把该 response item 写入 rollout，然后才排入工具执行（`core/src/stream_events_utils.rs:296-326`）。工具 future 完成后，结果 item 才写进会话历史/rollout（`core/src/session/turn.rs:2096-2113`）。因此进程若恰好在两者之间崩溃，rollout 会留下 call、没有真实 output；它没有保存 Python/OS 工具的执行现场或副作用提交凭证。
2. **受控取消与进程崩溃不是一回事。** 用户中断时，工具运行时走 cancellation 分支；在可控路径会收尾/取消执行，构造 `aborted by user` 工具响应，再由 in-flight drain 持久化响应（`core/src/tools/parallel.rs:178-217`；`core/src/session/turn.rs:2096-2113`）。正常的 turn lifecycle 也有独立 `TurnAborted` 事件类型。硬崩溃来不及运行这段逻辑，可能没有工具结果、也没有 `TurnAborted`。
3. **停止的 thread 从 rollout 重建模型历史，不重建工具进程。** resume 读取存储 rollout，构造 `InitialHistory::Resumed`（`core/src/agent/control/spawn.rs:930-952`），新的 Session 再调用 rollout reconstruction 恢复 `ResponseItem` 历史与部分状态（`core/src/session/mod.rs:1321-1335`；`core/src/session/rollout_reconstruction.rs:317-340`）。如果同一进程里旧 thread 仍在运行，resume 会复用该内存 thread；若已停止则移出管理器并创建新 Session（`core/src/thread_manager.rs:1642-1661`）。前者是继续仍活着的进程，不是 rollout 断点恢复。
4. **缺少工具结果时，在下一次模型输入的副本里补 `aborted`。** turn 构造 provider 输入时，对 `clone_history()` 调用 `for_prompt()`（`core/src/session/turn.rs:334-340`）。`for_prompt()` 会规范化模型输入，给缺 output 的 function/custom tool call 插入匹配 call ID 的 `FunctionCallOutput`，文本为 `aborted`（`core/src/context_manager/history.rs:141-146,329-336`；`core/src/context_manager/normalize.rs:20-61,81-95`）；单元测试明确断言此行为（`core/src/context_manager/history_tests.rs:1420-1453`）。因为这是历史副本的 prompt normalization，占位结果不会被写回 rollout，不能当成“工具已执行并真实返回 aborted”的持久事实。

**所以 Codex 的语义是**：rollout 留下可恢复的模型交互轨迹；缺失结果在发起后续模型请求时用 prompt-only `aborted` 占位补齐协议结构；它不自动重新执行旧工具，也没有通用 runtime checkpoint。模型看到占位结果后仍可能在后续响应中发出一个新的工具调用——这是模型驱动的新调用，不是恢复器重放旧 call；占位文字也不证明外部副作用没有发生。

**纠正：不得把恢复策略扩大为跳过整个未完成 turn。** Codex 的源码行为是保留历史，在模型输入副本中逐个补齐缺少 output 的工具调用；它不是只把最后完整 turn 前缀交给模型。DB-GPT 也必须保留该 turn 的 user input、assistant 原始 `tool_calls` 和已经真实写入的工具结果。Chat Completions 的对应形状直接是原 assistant `tool_calls` 后，为缺结果的 call_id 增加一条 `role: "tool"`、`tool_call_id: <原 id>`、文本 `content` 的消息；OpenAI 官方文档给出的 tool result 本来就是字符串，格式可由应用决定（[Function calling](https://developers.openai.com/api/docs/guides/function-calling)，[Chat API schema](https://developers.openai.com/api/reference/cli/resources/chat)）。所以这不是协议层面的特殊或可疑结构，provider 接受性不应成为实现前置门槛；第三方 provider 的兼容性属于常规集成测试。合成项不得写入权威 `agent_event`，不得自动重跑旧工具。Codex 使用 Responses API 的 `ResponsesApiRequest` 与 `ResponseItem`（Codex `core/src/client.rs:838-848`），DB-GPT 使用 Chat Completions，二者是不同 wire format、相同的逐调用补齐思路。

**第一期恢复约束**：未完成 turn 的全部原始事件留在 `agent_event`，模型输入保留整轮；恢复器只对缺结果的具体工具调用补一个按 `tool_call_id` 配对的 prompt-only `role: "tool"` 文本结果，其他真实结果照常保留。当前纯投影切片采用与 Codex 一致的占位文本 `aborted`，并把 synthetic call ID 与消息副本一同返回，便于调用方识别；该文案描述的是模型输入协议补齐，不是对外部副作用状态的事实判断。占位绝不写入权威事件日志，也不自动重跑旧工具。真实 provider 的差异留在常规集成覆盖，不作为纯逻辑投影的先行阻塞。

**交叉约束：不要给 rollout transcript 叠加第二份 ReAct memory prompt。** 当前代码会把 `ActionOutput.observations` 格式化成 `Observation: ...` 记忆片段；这是一种旧 ReAct prompt 表示，不是未来协议的硬性要求（`role.py:320-335,390-405`；`react_agent.py:84-93`）。新方案的实时多步交互应由有序 `assistant.tool_calls` 与 `tool_result` 消息让模型看到工具结果；进程中断后仍保留整轮，只对缺结果的具体 call_id 做待决处理（§10、§11.6），不以截掉整轮来规避协议配对。因此 rollout 分支建议既关闭 GptsMessages 跨请求回填，也不默认把同一工具结果再转成 `Observation:` HUMAN 消息；只有后续证明压缩/摘要确有必要、且内容不是协议历史的重复时，才评估一个可追溯的派生摘要。旧 conv 清理无法解决新会话中的双重 prompt 来源。

### 11.4 本轮结论与剩余决定

- 🔴 **已验证差距**：当前续聊依赖 StorageConversation 中 human/ai/view-final 投影，不是原始 tool transcript；模型角色仍有奇偶推断；UI 终态落库失败仍会返回 final/done；rich UI 字段超过 v1 transcript schema。GptsMessages 查询/回填没有 turn-complete 过滤；并且目标 API 写入的是循环内部分 self-send，恢复却按相邻两条配对，不能作为有序、可恢复的轮次历史（DB-GPT commit `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`，详见 §11.5）。
- ⚖️ **权衡取舍**：旧会话不迁移，切换时清理旧会话并接受旧 UI 历史不可从新 rollout 展示；新 rollout 仍只作为模型交互协议记录，Agent trace 与 UI 富展示保持职责区分。
- 💭 **建议**：恢复必须保留整轮，只处理缺结果的具体工具调用；不自动重放工具。Codex 的 prompt-only `aborted` 是已核实的逐调用协议补齐手段，不是 runtime checkpoint；DB-GPT 的恢复投影直接映射为 Chat Completions `role=tool` 文本消息，见 §11.6。
- **实现前须确认**：①旧会话清理的环境/conv 范围、备份与回滚计划；②rollout 分支以 `tool_result` 作为实时工具结果上下文、不重复注入旧 ReAct memory 的接线位置；③中断尾轮的 UI 提示与用户继续方式；④同 conv 锁/序号和 crash 后 stale owner fencing。provider endpoint 兼容作为常规集成覆盖；模型可能生成新的同类调用是工具执行层风险，不作为协议投影 PoC 阻塞项。生产拓扑不作为这轮逻辑分析前置条件。

> **本轮范围**：只读源码并更新设计文档；没有改代码、数据库、配置或测试。源码声明、注释和测试行为交叉核对；报告“实际 UI final 已发送”只基于测试，不据此推断它已入库。

### 11.5 GptsMessages 与 rollout 边界专项核对（第 10 轮）

> **范围与版本**：DB-GPT commit `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`。只读核对目标 `/agent/stream` ReAct API 的 Agent 初始化、GptsMessages 写入/恢复与模型输入组装；未运行数据库或真实 provider 集成，也未修改代码。

#### 事实链：保存的是什么，如何重新进入 prompt

1. **新 Agent 初始化时会查数据库恢复工作记忆。** `ConversableAgent.build()` 从 `conv_id` 解析 `real_conv_id`，再调用 `gpts_memory.get_agent_history_memory(real_conv_id, role)`，将返回的 `ActionOutput` 交给 `recovering_memory()`（`packages/dbgpt-core/src/dbgpt/agent/core/base_agent.py:245-259`）。当前目标 API 给 `GptsMemory` 配置的是 `MetaDbGptsMessageMemory`（`packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py:2310-2319`）。
2. **数据库行是 Agent 消息包，不是 provider 的原始 tool 协议事件。** `GptsMessage` 有 `content/context/action_report/resource_info/rounds/sender/receiver` 等字段（`packages/dbgpt-core/src/dbgpt/agent/core/memory/gpts/base.py:52-73`）；DAO 将这些字段逐行写入 `gpts_messages`，以自增 `id` 标识数据库行（`packages/dbgpt-serve/src/dbgpt_serve/agent/db/gpts_messages_db.py:29-92,95-143`）。这张表没有 `turn_id/event_seq`，也没有结构化的 `tool_call_id/arguments/tool_result` 配对列。
3. **`action_report` 中可能携带 ReAct 派生 memory fragment。** `write_memories()` 从 `ActionOutput` 提取 thought/action/action_input/observation 等，渲染为 `Question/Thought/Action/Action Input/Observation`，并把 fragment 附在 `action_output.memory_fragments`；只有之后该 `ActionOutput` 随 AgentMessage 经过 `_a_append_message()` 时，才会被序列化到 `GptsMessage.action_report`（字段定义 `packages/dbgpt-core/src/dbgpt/agent/core/action/base.py:40-71,93-95`；写入 `packages/dbgpt-core/src/dbgpt/agent/core/role.py:303-349,390-405`；模板 `packages/dbgpt-core/src/dbgpt/agent/expand/react_agent.py:84-93`；持久化 `base_agent.py:993-1046`）。它有助于模型记住此前做过什么；其中 observation 常与工具结果语义重叠，但这是整理后的 ReAct 工作记忆，不是可原样重发的 Chat Completions tool message，也不代表每个 action 都已持久化。
4. **恢复采用“每两条为一对”，不是按 turn 或消息关联键配对。** `get_agent_history_memory()` 以列表索引 `0,2,4...` 遍历，读取每对第二条的 `action_report` 并提取 memory fragments（`packages/dbgpt-core/src/dbgpt/agent/core/memory/gpts/gpts_memory.py:152-179`）。DB 实现按 `conv_id LIKE %real_conv_id%` 和 sender/receiver 过滤，再按 conv 后缀及 `rounds` 排序；并非按自增 `id` 或唯一时间序排序（`packages/dbgpt-serve/src/dbgpt_serve/agent/db/gpts_messages_db.py:145-173`）。因此源码不能证明两行必然对应同一个 user/assistant turn；重复 `rounds` 的稳定先后也未由该查询保证。
5. **目标 ReAct API 写入的不是完整轮次 transcript。** API 构造 `received=AgentMessage(user_input)` 后直接调用 `agent.generate_reply(...)`，没有先经 `receive()`/`_a_process_received_message()` 将初始用户输入写入 GptsMessages（`packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py:2960-2988`；`packages/dbgpt-core/src/dbgpt/agent/core/base_agent.py:405-434,1088-1096`）。循环继续时，Agent 会 self-send 部分 retry message 或中间 `reply_message`，这些才经 `_a_process_received_message()` 写入 GptsMessages（`base_agent.py:538-566,751-757,993-1046,1088-1096`）。直接返回 API 的最终结果不经过这个 self-send 写入路径。故该表对这条主链路是“部分运行期消息/工作记忆载体”，不能据此视为一份完整对话日志。
6. **成功恢复的片段如何进模型。** `recovering_memory()` 将 fragment 写入默认 `ShortTermMemory(buffer_size=5)`；该 memory 的 `read()` 返回当前 buffer（不按 query 做相关性筛选）（`packages/dbgpt-core/src/dbgpt/agent/core/role.py:472-484`；`packages/dbgpt-core/src/dbgpt/agent/core/memory/agent_memory.py:283-304`；`packages/dbgpt-core/src/dbgpt/agent/core/memory/base.py:705-765`）。ReActAgent 将结构片段变成 HUMAN/AI 消息（`packages/dbgpt-core/src/dbgpt/agent/expand/react_agent.py:274-364`）；`_load_thinking_messages()` 把它们附加在历史对话之后、当前输入之前（`packages/dbgpt-core/src/dbgpt/agent/core/base_agent.py:1413-1449`）。所以它确实是第二条模型上下文输入路径，但不是协议历史的可靠副本。

#### 旧 ReAct “Observation” 与新协议 `tool_result` 的区别

- 当前实现里的 `observation` 是 `ActionOutput.observations` 字段，经 memory template 变成字面文本 `Observation: ...`；`ReActAgent.read_memories()` 又把它作为 HUMAN 消息拼回 prompt（`packages/dbgpt-core/src/dbgpt/agent/core/role.py:320-335,390-405`；`packages/dbgpt-core/src/dbgpt/agent/expand/react_agent.py:84-93,335-355`）。这是当前 ReAct 循环的表达方式。
- rollout 目标协议则记录 assistant 发出的 `tool_calls` 和工具返回的 `tool_result`；实时轮内下一次模型请求通过 Chat Completions 的 assistant/tool 消息序列看到结果，不需要再复制成 `Observation:` 文本（设计文档 §2、§10）。这里“观察到工具结果”仍是概念，但协议/事件名称是 `tool_result`，不是独立的 observation memory。
- 两个边界不能混为一谈：正在执行的 turn 中，模型需要先前工具的 `tool_result` 才能决定下一步；进程中断后启动新请求，仍要保留这整轮以及其中已完成的调用/结果，只对缺结果的具体 call_id 做恢复处理，不重放工具。不得用 `turn_completed` 把未完成轮次整体截掉；实时模型输入仍须包含活动轮里已经发生的有效协议内容。

#### 结论分类与方案边界

- 🔴 **已验证差距**：`GptsMessages` 没有 turn/event 级稳定关联；恢复依赖两行配对和 `rounds` 排序；目标 API 只持久化内部 self-send 的部分消息，不包括 API 直接接收的初始问题和直接返回的最终答复。因此它不满足 Codex rollout 式有序、可恢复会话记录的要求。调用点和查询逻辑能证实上述结构，但本轮没有接真实 DB 跑多 turn 故障注入。
- 🔴 **已验证的旁路风险**：如果某条 `action_report` 被上述恢复逻辑选中，其中的 memory fragment 会作为普通文本消息进入 prompt，当前没有 event `turn_completed` 过滤。完整-turn 截断 `agent_event` 本身无法约束这条独立读取路径。
- ⚖️ **职责区分**：`GptsMessages` 中的 ReAct 片段有 thought/action/observation 摘要，可在一个 Agent 请求的多步循环内帮助延续工作；这和 provider tool call/result 在内容上可能重叠，但语义、结构、持久化保证不同。不能把它并入权威 rollout 后称为 native tool result，也不能把 `trace_id` 外键/标识等同于完整 Agent trace。
- 💭 **第一期推荐（待评审）**：rollout 路径的模型消息统一由 `agent_event` 投影构造：实时轮内包含已发生的 assistant tool calls 与 `tool_result`；进程重启后仍投影未完成 turn 的完整已知内容，只为缺结果的具体 call_id 评估 prompt-only 未知结果配对项。严禁整轮截断，也不自动重放工具。关闭 GptsMessages 跨请求回填，也不把同一 `tool_result` 再注入为 ReAct `Observation:` memory message。可以保留非模型可见的运行期 bookkeeping；如需压缩摘要，须先证明它为模型提供了协议历史没有的信息，并记录来源 turn/event。此推荐不代表当前已实现。
- **待核实/验收**：①真实 SQLite/目标 DB 方言下，构造单步终态、多步工具、retry、连续两个 turn，检查 GptsMessages 行序/恢复结果；②验证实时轮内每个后续 Chat Completions 请求都包含正确 call_id 配对的 `tool_result`，但不额外重复注入 ReAct observation；③故障注入后验证恢复输入保留整个 user turn、原始 assistant tool_calls 和同轮真实结果，只对缺失 call_id 做处理且不重放任何旧工具；④真实目标 provider 请求作为常规集成覆盖。当前没有覆盖 `get_agent_history_memory()` / `recovering_memory()` 的定向测试（只检索了相关测试路径；未运行测试）；Codex normalization 本身也不保证模型不会生成新的同类调用。

### 11.6 恢复边界纠正：保留整轮，只处理未决工具调用（2026-09-28）

> **纠正原因**：此前“只恢复最后完整 turn 前缀”的建议把“缺少某个工具结果”扩大成了“整轮不进入模型上下文”。这会连同该轮用户问题、assistant 发出的原始工具调用，以及同轮已经成功落库的其他工具结果一起丢弃。用户明确指出该语义不可接受；本节取代 §11.3、§11.4、§11.5 中所有把整轮截掉作为恢复策略的建议。历史调查记录保留，但不再是当前有效设计。

- 🔴 **已定约束**：权威事件日志原样保留未完成 turn 的全部已提交事件；恢复/续聊不得跳过整轮。user input、assistant tool_calls、同轮真实 tool_result 全部保留，不能因一个 call_id 缺结果而丢弃其他内容。
- 🔴 **已定约束**：只把缺失结果的具体工具调用标为 unresolved/outcome unknown。恢复时不得伪称它已经成功或确定失败，不得把 prompt 合成项写回权威事件日志，也不得自动重跑旧调用（外部副作用可能已经发生）。
- 🔴 **协议映射已明确**：在“保留原始 assistant `tool_calls` 与其他真实结果”的基础上，针对每个缺失 `tool_call_id` 构造 prompt-only 的 `role: "tool"` 配对消息，`content` 是文本。Chat Completions 的官方 schema 正是 `content`、`role`、`tool_call_id`，官方示例用字符串作为 tool result（[Chat API schema](https://developers.openai.com/api/reference/cli/resources/chat)、[Function calling](https://developers.openai.com/api/docs/guides/function-calling)）；无需把这当成高风险 provider PoC。当前纯投影选择 `aborted`，以对齐 Codex；这只是模型输入中的占位措辞，不声称硬崩溃后的外部副作用确定失败。若产品要求更直白地提示 outcome unknown，可在后续评审替换文本，不改变协议结构。
- 🔴 **Codex 对照事实**：上述“按具体调用补齐协议”而非“丢弃未完成整轮”，与本地 Codex 源码的 prompt normalization 形状一致：`clone_history().for_prompt()` 对缺 output 的 function/custom call 按 call ID 插入 prompt-only `aborted` output；源码与测试见 commit `7a0e974e08c798d1e8d59d407aeb6e24db1313af` 的 `core/src/session/turn.rs:334-340`、`core/src/context_manager/history.rs:141-146,329-336`、`core/src/context_manager/normalize.rs:20-61,81-95`、`core/src/context_manager/history_tests.rs:1420-1453`。Codex 的占位用语是 `aborted`，DB-GPT 建议的 `unknown` 是为硬崩溃下无法确定副作用状态而提出的语义，不能说成 Codex 已采用 unknown。
- ⚖️ **残余行为风险，不是协议接入门槛**：Codex 源码显示的是补齐历史协议，不是“防止模型再次提出同类工具调用”的 exactly-once 机制；如果模型后续生成一个新的 tool_call，那是新调用而非恢复器重放。DB-GPT 也不应把“绝不会再次调用”写成保证。对有外部副作用的工具，按其风险单独使用确认/幂等键/状态查询；这与是否能将文本 tool result 发给 Chat Completions 是两个问题。
- **验收重点**：构造同轮两个并行工具调用：call A 有真实结果、call B 在执行期间崩溃无结果。恢复投影单测必须保留用户问题、原 assistant 两个 `tool_calls`、A 的真实结果；只为 B 生成按原 ID 配对的 prompt-only 文本结果；确认不重跑 A/B、不把 synthetic B 结果落库，也绝不能只恢复上一轮完整前缀。对单调用崩溃同样验证整轮保留。provider 真实调用属于部署兼容性覆盖，不是开始实现前的设计阻塞；Codex 本身也不提供对模型后续新调用的通用去重保证。

### 11.7 恢复会话与恢复 turn 的区别：Codex 行为及 DB-GPT 状态机（2026-09-28）

> **对照基线**：本地 Codex `codex-rs` commit `7a0e974e08c798d1e8d59d407aeb6e24db1313af`。以下是该 checkout 的源码行为，不代表所有 Codex 版本或线上遥测；Codex 使用 Responses API，DB-GPT 仍须映射到 Chat Completions。

#### Codex 已验证行为

1. **`thread/resume` 恢复 thread/session，不会自动重发 turn。**App Server 从已存历史构造 `InitialHistory::Resumed`，调用 `resume_thread_with_history()`，重建或复用 thread 并返回恢复响应；发起新模型交互的入口是之后单独的 `turn/start`，它提交新的 `Op::UserInput` 并取得该次提交的 turn ID（Codex `app-server/src/request_processors/thread_processor.rs:3057-3235,3402-3447`；`app-server/src/request_processors/turn_processor.rs:474-607`；`core/src/thread_manager.rs:871-925,1641-1661`）。因此 Codex 的“恢复会话”不等于“自动恢复同一 turn 的执行”。
2. **停止的 Session 从 rollout 重建历史，不重建工具执行现场。**`record_initial_history(Resumed)` 调用 rollout reconstruction 并安装重建后的 history；恢复路径没有自动提交旧用户输入来触发工具（Codex `core/src/session/mod.rs:1321-1335,1428-1459`）。新 turn 启动时有自己的 turn ID 和 `TurnStarted`（`app-server/src/request_processors/turn_processor.rs:557-607`；`core/src/tasks/regular.rs:45-56`）。
3. **未完成旧 turn 的状态可以在读取/UI 投影中显示为 interrupted，而非伪造一条历史终态。**thread 非 Active 且恢复出的旧 turn 仍是 InProgress 时，App Server 将返回视图中的 status 改成 `Interrupted`（Codex `app-server/src/request_processors/thread_processor.rs:3333-3337,4889-4920`；`app-server/src/request_processors/thread_lifecycle.rs:861-874`）。这段代码改的是 thread/turn 响应状态，不是追加一条 `TurnAborted` 到 rollout。
4. **只在模型输入副本中补齐缺失的工具结果。**采样前从 `clone_history()` 生成 `for_prompt()` 输入；normalizer 为缺失 output 的 function/custom call 插入匹配 call ID、文本为 `aborted` 的 output；原始 rollout/history 不因此变成“工具实际返回 aborted”，也不会由 resume 重新运行旧工具（Codex `core/src/session/turn.rs:334-340,1348-1367`；`core/src/context_manager/history.rs:141-146,325-336`；`core/src/context_manager/normalize.rs:20-95`；测试 `core/src/context_manager/history_tests.rs:1420-1453`）。
5. **受控中断与硬崩溃不同。**受控取消路径能够写入中断上下文标记、尝试 flush，再发出 `TurnAborted`；硬崩溃没有机会执行该清理路径。源码中的 `append_interrupted_boundary()` 用于“从中断快照 fork”，不能误当成普通 `thread/resume` 会自动补写中止事件（Codex `core/src/tasks/mod.rs:900-950`；`core/src/thread_manager.rs:1971-2044`）。

#### DB-GPT 第一阶段状态转换（事件语义已实现；UI 状态投影仍待做）

权威事件表不增加 `agent_turn` 表，也不因恢复改写旧事件。轮次状态由事件终态和会话 owner 状态派生：

```text
RUNNING(T1)
  ├─ 最终 assistant 已提交 + turn_completed 已提交 ───────> COMPLETED(T1)
  ├─ 服务观察到受控中止 + turn_aborted 已提交 ───────────> ABORTED(T1)
  └─ 硬崩溃：只留下已提交事件、没有终态 ────────────────> INCOMPLETE(T1，派生状态)

INCOMPLETE(T1) + 恢复会话
  └─ 重建历史；不重启 T1 的模型采样/工具；T1 仍未完成

INCOMPLETE(T1) + 租约已释放或过期可接管 + 用户提交新输入
  └─ 创建新 turn_id=T2；模型输入 = T1 全部已知历史
       + 对 T1 缺结果 call 的 prompt-only 配对项 + T2 新输入
       ├─ T2 正常完成 ───────────────────────────────────> COMPLETED(T2)
       └─ T2 受控中止 ───────────────────────────────────> ABORTED(T2)
```

- **不自动续跑 T1**：恢复端点只重建会话；不会自动重发 T1 的 user message，也不会自动重跑 T1 的 tool_calls。用户发送“继续”时，文本作为 T2 的新输入；模型可参考 T1 的完整已知历史作答。若产品要求无人值守地自动重发原请求，那是另一项重试/副作用语义，不是 Codex 当前 thread resume 行为。
- **T1 的终态不补造**：硬崩溃后 T1 保留为“有 `turn_started`、无终态”的日志事实；读取侧在确认旧 owner 不再活动后将其展示为 `incomplete/interrupted`。创建 T2 不把 T1 改成 `turn_aborted`，也不声称未决工具一定失败。
- **模型输入只修复协议副本**：T1 的全部 user/assistant/tool 事实都保留；对无真实结果的 call 按 `tool_call_id` 补 prompt-only tool message。真实结果和合成结果不得混淆或落成同一种权威事件。T2 的新用户输入随后进入同一恢复后上下文。
- **恢复前先处理旧 owner**：新 owner 只有在确认旧执行者退出，或以原子方式取得更高 fencing epoch 后，才能接受 T2。旧 epoch 的续租与事件 append 必须失败。fencing 不能撤销旧 worker 已经发出的外部副作用；对这类工具不自动重放，额外保证须由工具幂等/对账机制提供。
- **已实现但尚未生产集成验收**：owner lease/fence 存在既有 `chat_history` 行；活动租约默认 90 秒、每 30 秒续期，过期后新 turn 可取得更高 fence；每次模型调用、工具启动和 append 校验旧 worker 是否仍持有租约。SQLite 单测已覆盖，MySQL/OceanBase 与多实例共享数据库仍需实际验收。Codex 本地源码不提供 DB-GPT 所需的数据库锁/fencing 答案。
- **仍待实现**：T1 的 UI/read-model status 尚未由事件投影显示为 `incomplete/interrupted`；数据库 lease 过期只能作为接管条件，不能证明旧 worker 已停止其外部副作用。当前纯投影使用 `aborted`；该词仅是模型输入协议补齐文本，不是“工具实际失败”的持久事实。

### 11.8 首版事件持久化层（2026-09-29）

**阶段快照**：本节记录 DAO 切片完成、Agent 主链路接入前的状态；它不是当前整体状态。之后主链路接线进度见 §11.10。代码位于基线 commit `ca9f014cb3ead157ca2ee6ce645658f5fb55138c` 之上的工作树。

#### 已实现

- `packages/dbgpt-serve/src/dbgpt_serve/agent/db/agent_event_db.py:30-37,80-95` 定义一张 `agent_event` 表，首版允许六种事件：`turn_started`、`user_message`、`assistant_message`、`tool_result`、`turn_completed`、`turn_aborted`。字段为 `event_id` UUID 主键、`conv_id`、`event_seq`、`turn_id`、`event_type`、`schema_version`、JSON 文本 `payload`、`created_at`；唯一约束是 `(conv_id,event_seq)`。没有新增 `agent_stream` 或 `agent_turn` 表。
- 同文件 `:107-193` 实现 `append_event(s)` 和 `list_by_conv_id()`：整批写入在一个短事务中提交；序号按 `conv_id` 的已提交最大序号递增；完全相同的整批重试幂等，事件 ID 冲突或部分重试拒绝；读取按 `event_seq` 排序并支持游标/条数上限。payload 在应用层规范化 JSON 编码，MySQL 使用 `LONGTEXT` variant（`:29,245-267`）。
- 同文件 `:223-243` 以已存在的 `chat_history` 会话行为追加序号的协调点：SQLite 先执行短事务 `BEGIN IMMEDIATE`，其他方言执行 `SELECT ... FOR UPDATE`；随后在同一事务内读取最大序号并插入。这只保证**事件 append 分配序号不冲突**，不保证两个请求不会把同一会话的两个 turn 交错，也不是跨 worker 的 turn lease/fencing。
- ORM 已加入 `packages/dbgpt-app/src/dbgpt_app/initialization/db_model_initialization.py:13,27-45` 的模型注册清单；手工建表脚本 `assets/schema/dbgpt.sql:122-134` 增加对应 DDL。SQLite 临时库的测试会由 ORM metadata 建表；没有在真实 MySQL/OceanBase/生产数据库执行 DDL。
- 测试 `packages/dbgpt-serve/src/dbgpt_serve/agent/db/tests/test_agent_event_db.py` 覆盖批量顺序与游标、幂等重试、部分重试/ID 冲突拒绝、JSON/事件类型拒绝、缺失会话拒绝、SQLite 两线程序号唯一、失败批次整体回滚和 MySQL `LONGTEXT` 编译。与纯投影测试一起执行：`uv run pytest -q packages/dbgpt-serve/src/dbgpt_serve/agent/db/tests/test_agent_event_db.py packages/dbgpt-core/src/dbgpt/agent/core/tests/test_rollout.py`，结果 **18 passed**；pytest 有两条仓库依赖弃用警告，不是本次测试失败。

#### 当时尚未接线（历史状态，已由 §11.10 推进）

- 🔴 **尚未接到请求主链路**：`agentic_data_api.py:2322-2357` 当前仍创建/保存 `StorageConversation`，从 UI 历史只取 human/ai/view 最终回答构造 `historical_dialogues`，再把本轮输入加入它；它没有调用 `AgentEventDao` 或 `project_rollout_events()`。因此 event 表就算建出，目前 ReAct 请求也不会写入或从中恢复。
- 🔴 **模型原始协议边界尚未接 recorder**：`packages/dbgpt-core/src/dbgpt/agent/expand/tool_calling_agent.py:451-491` 从 provider 响应读出 `tool_calls`，只暂存在 Agent 实例 `_pending_native_tool_calls`；还没有先 append `assistant_message` 再允许 action 执行的可靠性门闩。
- 🔴 **并行结果目前只是聚合对象**：`packages/dbgpt-core/src/dbgpt/agent/expand/actions/tool_action.py:280-395` 的批处理并行执行后聚合成一个 `ActionOutput`。rollout 接线需在每个工具实际完成处 append 对应 call_id 的真实结果，并确保任何 append 失败都会阻止后续模型请求；不能等聚合后的 `ActionOutput` 才伪装成一组原始 provider 事件。
- 🔴 **恢复输入类型目前不匹配**：纯投影器输出 `ModelMessage`（`packages/dbgpt-core/src/dbgpt/agent/core/rollout.py:49-177`），其中 `assistant.tool_calls` 和 `role=tool/tool_call_id` 可转换为 Chat Completions 格式（`packages/dbgpt-core/src/dbgpt/core/interface/message.py:184-204,457-534`）；但当前 Agent 的 `historical_dialogues` 是只承载文本的 `AgentMessage`。主链路接线必须在 native Chat Completions 路径改为从事件投影构造 provider messages，不能把结构化 tool history 压扁成 `Observation:` 文本，也不能同时再从旧 `GptsMessages` 重复注入同一观察结果。
- ⚖️ **合理边界**：事件表只记录会话协议事实，不代替现有 UI `history_payload`，也不等于 Agent trace；本阶段不删除/迁移旧会话、不重跑工具、不恢复 Python 进程。读取端接入前必须由 API 的会话 owner/tenant 鉴权保护；DAO 本身只按 `conv_id` 查询，没有替调用层做授权。payload 脱敏、大小限制、保留/删除策略也仍需在接线与上线前补齐。
- 💭 **方言待验证**：SQLite 的并发和原子回滚已有真实临时文件测试；MySQL 行锁只按 SQLAlchemy API 实现，未对目标 MySQL/OceanBase 部署做集成测试。`assets/schema/dbgpt.sql` 是手工 SQL 提供，不代表所有非 SQLite 安装流程已经自动发布该表。

#### 当时规划的下一阶段（现已开始，验收见 §11.10）

先把事件 reader/projector 接到 native Chat Completions 输入；在模型响应边界提交原始 assistant 消息，在每个工具完成边界提交匹配 call_id 的真实结果，在工具运行前/新模型请求前落实 fail-closed；最终 assistant 和轮次终态写入同一 rollout。随后隔离 legacy `StorageConversation/GptsMessages` 对模型输入的旁路，并用故障注入覆盖“事件写失败不调用模型/工具”“中断恢复保留整轮且不重跑工具”“新请求从日志恢复”。实施时要先查清会话鉴权与旧会话切换，不能让空 rollout 静默覆盖仍在使用的 legacy transcript。

### 11.9 首个纯历史投影切片（2026-09-28，先行纯逻辑切片）

**目标与边界**：先验证持久化事件恢复为模型消息的纯逻辑，不碰数据库和 Agent 请求主链路。实现接收调用方已按权威 `event_seq` 排序的 `RolloutEvent` 序列，输出项目现有 `ModelMessage` 类型；本模块不负责读库、分配序号、持久化、恢复 session/turn、调用 provider 或执行/重放工具。

- **实现**：`packages/dbgpt-core/src/dbgpt/agent/core/rollout.py:49-177` 的 `project_rollout_events()` 保持 user/assistant/tool 消息顺序和原始 assistant `tool_calls`；`turn_started`、`turn_completed`、`turn_aborted` 不投影成模型消息。缺失 call 结果在遇到后续消息/lifecycle 边界或输入结束时，按原 call ID 在内存副本中补 `role=tool`、`content="aborted"`；真实结果保留其事件顺序，缺失项按原 tool_calls 顺序补齐。返回对象另带 `synthetic_tool_call_ids`，标记这些非权威占位消息。非法/未知事件、无对应 pending call 的 tool result 会报 `ValueError`，不静默丢弃或错误配对。
- **确定性测试**：`packages/dbgpt-core/src/dbgpt/agent/core/tests/test_rollout.py:18-137` 覆盖生命周期标记过滤、文本 turn、两个并行 tool call 中只缺一个结果、并行结果逆序、跨消息边界补齐、输入未被修改、畸形/未知事件及重复结果拒绝。运行命令：`uv run pytest -q packages/dbgpt-core/src/dbgpt/agent/core/tests/test_rollout.py`；本轮结果 **9 passed**。
- **“实现/接线/生效”区分**：在本节对应的纯逻辑切片提交阶段，投影函数和单测已实现但尚未由 API 调用；该历史状态已在 §11.10 推进。真实 provider endpoint 是否接受并稳定处理合成 `role=tool` 输入仍未验证。
- **该阶段快照的边界**：当时投影器已接入 `_thinking_native()`，但 turn lease 尚未实现。后续租约/fencing 进度见 §11.10；目标方言测试和真实 provider 集成覆盖仍未完成。

### 11.10 Agent 主链路接入与当前验收状态（2026-09-29）

以下是本轮在 DB-GPT `ca9f014cb3ead157ca2ee6ce645658f5fb55138c` 基线上新增的**未提交工作树实现**。这里按“实现了 / 已接线 / 实际生效”区分，不把定向单测表述为生产环境验收。

#### 代码路径与事件边界

1. **请求入口与会话保护**：`packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py:2421-2446` 先校验会话 owner，再读取该 `conv_id` 的 rollout；有 UI 旧历史但无事件的会话会被明确要求新建，避免假装恢复成功。随后领取租约并写 `turn_started + user_message`；`:2484-2493` 设置 `enable_native_function_calling=True`、`enable_rollout_history=True`。该路径没有迁移或删除旧记录。
2. **模型历史恢复与协议一致性**：`packages/dbgpt-core/src/dbgpt/agent/expand/tool_calling_agent.py:499-564` 每次 native Chat Completions 调用前加载事件投影，把当前 system instructions 与结构化历史合并；不再把 assistant `tool_calls` / `role=tool` 压成 ReAct Observation 文本。API 通过 `agentic_data_api.py:3067-3071` 将旧 `## ReAct Output Format` 改写为 native function-call 指令，并更正 MCP connector 工具说明。rollout 模式若模型返回文本 `Action:` 调用，`tool_calling_agent.py:230-269` 会拒绝执行；普通 assistant 文本作为最终答复。
3. **模型响应先记后执行**：`tool_calling_agent.py:551-564` 在从 provider 得到响应后，先持久化 assistant 的内容、原始 `tool_calls` 和 `finish_reason`，成功后才把调用交给 action 执行。事件 append 异常会从控制流向上抛，Agent 不会继续执行该批调用。
4. **逐工具结果**：`packages/dbgpt-core/src/dbgpt/agent/expand/actions/tool_action.py:288-433,435-457` 在每个工具完成处分别提交 `{tool_call_id,name,content,success}`；并发工具各自 append，不等聚合 ActionOutput。任一结果 append 失败，不会形成可供下一轮模型调用的成功聚合返回。前置 lease guard 失败会 fail-closed；并行工具可能已经同时启动，数据库 fencing 不能撤销外部副作用。
5. **终态与 UI 分离**：`agentic_data_api.py:3628-3764` 正常答复写 `turn_completed`，Agent 失败写 `turn_aborted`；最终 UI history 仍通过 `StorageConversation` 单独写。客户端断开发生在终态提交前时可能没有终态事件，读取投影仍保留该完整未完成轮次。`agentic_data_api.py:1242-1283` 显式关闭内层异步流、取消并 await Agent task，随后清理租约；它不恢复工具进程，也不自动回放工具。
6. **活动 turn 租约与 fencing**：`packages/dbgpt-core/src/dbgpt/storage/chat_history/chat_history_db.py:57-67` 在既有 `chat_history` 行增加 `active_agent_turn_id`、`active_agent_turn_lease_until`、单调 `active_agent_turn_fence`，不新增 `agent_turn` 表；`packages/dbgpt-serve/src/dbgpt_serve/agent/db/agent_event_db.py:225-304,377-403,526-558` 实现原子领取、90 秒租约、续租、释放、过期接管和 fence 校验。API 在 `agentic_data_api.py:2443-2483,3092-3114` 开始 turn 并每 30 秒续租，在每次 provider 调用、工具启动和事件 append 前检查 lease；另一个请求遇到活动 lease 会被明确拒绝（`:2445-2454`）。SSE 关闭时先取消并 await Agent task，再释放 lease（`:1242-1283`）。

#### 当前验证与结果

- **实现并已接线**：`agent_event` 的 DAO/projector 与 API/Agent/native tool path 已连通；rollout 投影包括整个未完成 turn，未决调用的 `aborted` 只存在于模型输入副本，未写入事件表，未决旧工具不会被执行器自动重跑（投影规则见 `packages/dbgpt-core/src/dbgpt/agent/core/rollout.py:49-177`）。
- **定向测试实际结果**：2026-09-29 执行命令 `uv run pytest -q packages/dbgpt-core/src/dbgpt/agent/expand/actions/tests/test_tool_calling_agent.py packages/dbgpt-core/src/dbgpt/agent/expand/actions/tests/test_parallel_tool_action.py packages/dbgpt-core/src/dbgpt/agent/core/tests/test_rollout.py packages/dbgpt-serve/src/dbgpt_serve/agent/db/tests/test_agent_event_db.py packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/tests/test_agentic_data_api_lifecycle.py`，结果 **54 passed**。覆盖临时 SQLite DAO/租约并发与旧 fence 拒写、旧 `chat_history` 表的租约列增量升级、模拟旧 turn 崩溃后由新 turn 恢复完整历史并仅补缺失 call 的模型输入占位、DB event store → native tool call → 真实结果 append → 下一次模型输入重建的闭环、投影、assistant/tool 事件写失败阻断、模型/工具前置 guard 失败即停止、文本伪调用 fail-closed、提示改写、SSE 断开清理和租约 cleanup；不是启动真实 HTTP 服务并连接真实模型/目标生产数据库的端到端测试。对本轮修改 Python 文件执行 `uv run ruff check ...`，结果 **All checks passed**；`git diff --check` 通过。
- **故障边界**：assistant event 写入失败在 action 执行前阻断；工具结果事件写入失败阻断后续模型轮次；已并发启动的其他工具不能因此被回滚。API 在最终 rollout 终态写入失败时返回明确提示，可能存在“工具已执行但无法提交最终记录”，需要人工核对，不能安全重试造成副作用的操作。

#### 尚未完成，不能对外宣称已生产可用

- 🔴 **目标部署数据库的租约 / DDL / 事务语义未集成验证**：SQLite 临时库已覆盖两个并发领取仅一个获胜、活动租约拒绝、过期后高 fence 接管、旧 worker 无法 append/释放新租约、续租与过期拒绝，以及旧 `chat_history` 表通过 `ensure_column` 补租约列。持久化恢复模拟验证 T1 的原始 assistant 调用和真实部分结果保留、T2 从同库恢复且只为未决 call 生成 prompt-only 配对项、旧 T1 fence 不能追加；native Agent 闭环测试验证下一次 Chat Completions messages 实际包含已记录的 assistant tool call/result。MySQL/OceanBase 的行锁、目标 schema 升级路径、跨进程共享同一数据库的行为尚未实际集成测试；如果服务副本使用彼此独立的 SQLite 文件，租约不能跨副本互斥。
- 🔴 **目标数据库方言未集成验证**：append/read/事务/DDL 当前只用临时 SQLite 实测；MySQL `LONGTEXT` 仅有 SQL 编译测试，`SELECT FOR UPDATE` 未连 MySQL/OceanBase 实测。`assets/schema/dbgpt.sql` 有手工 DDL，ORM metadata 注册已接线；已存在数据库的建表/发布升级路径仍需集成验收。
- 🔴 **真实 provider/API 崩溃恢复未端到端验收**：当前测试未启动完整 HTTP 服务、真实 Chat Completions provider 和目标数据库并做进程中止/重启；代码投影测试不等同于运行时端到端证明。
- 🔴 **UI 不展示 rollout 派生的 interrupted/incomplete 状态**：模型续聊投影会保留未完成 turn，但 UI 仍从 `StorageConversation` 的展示历史读状态；还没有把无终态事件的旧 turn 显示成 interrupted，也没有给缺结果 call 单独的未知状态标识。
- 💭 **动态上下文历史快照未覆盖**：事件表恢复模型对话协议历史，但不会恢复某次旧请求的完整 system prompt、工具 schema、动态数据库/知识资源、附件解析内容或模型参数；新请求按当前配置重新组装。这个边界符合本期“续聊会话”目标，不等于精确重放原始 provider request。
- ⚖️ **旧会话策略**：有旧 UI history 且没有 rollout events 的会话被拒绝续聊，用户应开启新会话；本实现不迁移、不自动清空、不删除旧会话数据。若要切换时清理，必须另行限定环境和精确数据范围。
- ⚖️ **trace 与 checkpoint 边界**：`agent_event` 仅是按序会话协议记录，不是全量 Agent trace，不是运行检查点；不记录所有 SSE/token/span，也不恢复 Python 内存、进程或副作用工具。

因此当前结论是：**首期有序事件日志、Chat Completions 历史投影、Agent 主链路恢复接线和单活动 turn 的租约/fencing 已实现；54 项定向测试通过。**但这代表“代码实现/本地 SQLite 验证”，不代表真实 provider、目标 DB 方言或生产多 worker 已实际生效。继续下一阶段应先做目标部署数据库集成与完整 API 重启故障注入，再评估 UI 状态投影和数据治理，不新增 runtime checkpoint 或自动重放旧工具。
