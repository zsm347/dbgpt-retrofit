# DB-GPT 主链路评估与改进清单（个人复盘）

> - **评估时间**：2026-09-21
> - **评估对象**：本地 DB-GPT clone（`packages/` 多包结构，react-agent 主链路）
> - **评估方法**：代码走查（每条附 `file:line` 证据）+ 业界对标调研（来源见附录 A）
> - **标签说明**：
>   - 🔴 已验证差距（有代码证据 + 业界明确对标，建议改）
>   - ⚖️ 权衡取舍（设计选择，不一定要改，避免误伤）
>   - 💭 主观判断（需要更多验证）

---

## 1. 主链路速览

**一句话**：前端 → `POST /api/v1/chat/react-agent`（SSE）→ `_react_agent_stream_impl` 编排 → `ToolCallingReActAgent` 主循环（thinking → act → verify，≤30 轮）→ 工具执行（技能引擎 / 沙箱 / SQL / 知识库 / MCP / 子代理）→ SSE 回推 + 历史落库。

**关键路径索引**：

```
启动        _cli.py:99 → dbgpt_server.py:279 run_webserver → mount_routers:55（:80 挂 agentic_data_api）
接入        agentic_data_api.py:3835（react-agent）/ :3879（knowledge-agent）
预检        _open_turn_attachments:1118（附件校验/物化/清单，400/404 前置）
编排        _react_agent_stream_impl:1221
  技能加载   :1478-1483（load_skills_from_dir → registry.list_skills）
  ToolPack   :2616/2622（skill 模式）· :2819/2826（非 skill 模式）
  Prompt     :2469（skill 简化版）· :2652（完整版）· :2945 包成 PromptTemplate
  Agent 构建 :2951-2960（ToolCallingReActAgent(max_retry=30).bind(...)）
  SSE 桥接   :2077 stream_queue + :3046 消费循环
Agent 循环  base_agent.py:472 generate_reply（:538 while 循环；thinking:774 / act:676 / verify:706）
工具执行    tool_action.py:162 run_tool / :280 run_tools_batch
模型调用    llm_client.py AIWrapper → model/cluster/client.py:38 DefaultLLMClient
收尾        :3564 FinalAnswerAssembler → :1046 _react_terminal_events（落库 → final → done）
前端        web/hooks/use-react-agent.ts:71（fetch POST + ReadableStream）
```

---

## 2. 总体结论（TL;DR）

1. **功能覆盖不差，形态不专业**：上下文管理 8 个维度覆盖了 6-7 个（持久化、重建、运行时写入、组装、压缩、可观测、治理都有），但**分层是隐式的、消息表示是文本化的、组装与运行时数据耦合**。
2. **工具协议落后业界共识**：业界已收敛到"原生 tool calling + `role:"tool"` 闭环"；本项目 native 调用已开（`enable_native_function_calling=True`），但**工具结果回填仍是 `"Observation:"` 文本**，双轨缝合不干净。
3. **上下文治理闭门造车**：自研四层压缩方向正确、深度不差（比多数框架透明），但未借力 provider 能力、记忆塞进协议而非外挂。
4. **有几处是合理权衡，不要误伤**：模型无关兜底、四层压缩、大输出落盘、附件"重解析而非回放"（详见 §4）。
5. **最该警惕的两件事**：① 双轨协议的缝合不一致（native 调用 + 文本结果）；② 手工维护的 prompt 清单已发生漂移（见 P0-2 的编号重复实证）。

> **术语澄清（重要）**：本文档中的"**记忆**"指 ReAct 的**轨迹片段存储**（`StructuredAgentMemoryFragment`，按 `conv_id` 隔离，作用是每轮重建工作上下文），**不是**平常所说的"agent 长期记忆"（跨会话记住用户偏好/事实；业界的对应物是 ChatGPT Memory / Anthropic Memory Tool / mem0 等，本项目主链路未实现——**详见 P1-22：仓库有完整的长期记忆实现（向量库+时间加权检索），但只接线在旧 Agent 路径，主链路回退纯内存 `ShortTermMemory(5)` 且溢出被丢弃**）。知识库属于外部知识，也不等同记忆。

---

## 3. 差距清单

### P0 —— 对齐业界共识，低成本高收益

| ID | 问题 | 证据 | 影响 | 建议 | 性质 |
|---|---|---|---|---|---|
| P0-1 | **native 模式工具结果不走标准协议**：调用走 `tool_calls`（标准），结果却回到 `"Observation:"` 文本，无 `tool_call_id` 配对 | 整个 agent 包无 `ModelMessageRoleType.TOOL` 使用；结果回填见 `expand/react_agent.py:335-356` | 调用与结果不闭环；并行调用场景靠顺序对应；无法做严格 trace/校验 | native 模式结果回填走 `role:"tool"` + `tool_call_id`；文本模式保留 | 🔴 |
| P0-2 | **prompt 工具清单手工维护，已编号漂移**：14-17 号出现两次 | `DB-GPT@ca9f014:packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py:2733-2788` 手写编号清单；`:2819-2850` 另行装配 ToolPack；`packages/dbgpt-core/src/dbgpt/agent/expand/tool_calling_agent.py:53-98` 的 native schema 则从 ToolPack 生成。重复编号是代码事实，清单与 ToolPack 是否逐项完全不一致需逐项比对。**【2026-10-10 复核 @ca9f014 工作区】重复仍在**：全工具分支编号 1-17 后再次出现 `14. todowrite / 15. dispatch_parallel_tasks / 16. question / 17. terminate`（`:2959-2976`，工作区另有未提交改动但该块出自已提交 commit）；blame：撞号由 codegraph 集成提交 `a0b49a160`（2026-07-04，插入 13.1-13.3 并重编 14-17 块时未重排后续编号）造成，子代理提交 `36a99b7ca`（2026-07-28，#3161）沿用 15。**部分修复**：kb/codegraph 工具的描述双源已动态对齐——`_has_code_graph()` 探测（`:1372-1392`，meta+vertex_count>0）→ 无图谱时 ToolPack 过滤 kb_codegraph（`:2190-2199`）且 prompt 的 `codegraph_tools_desc` 置空（`:1607-1614`）；技能预选分支（1-9）编号干净。根治（ToolPack 单一来源生成）未做 | 模型看到重复编号；清单与运行时工具集存在双源漂移风险 | 工具描述从 ToolPack 注册表**单一来源生成**，禁止手工维护 | 🔴 |
| P0-3 | **prompt 输出契约与已接线的 native 工具调用冲突**：系统提示词要求工具调用只能用文本 ReAct 格式，同时 agent 启用并向模型传递 native tools | `DB-GPT@ca9f014:packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py:2365,2806-2809`；native 分支条件、传递 `tools` / `tool_choice="auto"` 见 `packages/dbgpt-core/src/dbgpt/agent/expand/tool_calling_agent.py:425-432,472-490` | 两种调用契约并存；实际模型是否因此误选协议/降低成功率**待核实**（代码路径已接线，不等于所有 provider/model 都实际返回 native calls） | prompt 按模式生成；native 模式明确结构化调用优先级，文本格式只作为 fallback | 🔴 |
| P0-4 | **knowledge 模式复用通用 prompt，仍描述该模式未装配的多类工具** | `DB-GPT@ca9f014:packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py:2652-2810` 构造通用 prompt，`:2814-2850` knowledge 分支仅换 ToolPack，未重建 prompt；该分支实际是 `kb_tool_list + todowrite/question/terminate + business_tools + connector_tool_extras`，所以“只装 KB 工具”注释不准确，问题限定为 shell/skill/code 等多类描述与当前工具集不匹配 | 模型可能选择当前模式不可用的工具；无效轮次风险存在，频率**待核实** | prompt 与 ToolPack 同源生成，并按模式渲染 | 🔴 |
| P0-5 | **Python 工具无沙箱**：`code_interpreter` / `execute_skill_script_file` 走裸子进程（无内存限制、无黑名单、无目录白名单） | `tools/code_interpreter.py:39-56`（仅超时）；`agent/skill/manage.py:1029-1043`（同）；对比 `tools/shell_interpreter.py:34-69`（有轻量沙箱，但无文件系统隔离且绕过沙箱工厂，见 P0-9） | 模型生成的 Python 代码可在宿主机任意读写（同一 venv、同一文件系统、可见环境变量） | 接入 `SandboxRuntime`；至少统一加资源限制 + 目录白名单 | 🔴 |
| P0-6 | **沙箱 Python 黑名单过激 → 设计不自洽**：`import os` / `open(` / `import sys` / `socket` 全判"危险" | `dbgpt-sandbox/.../execution_layer/utils.py:200-218` | Python 永远无法走沙箱 → 沙箱实际只服务 bash（解释了 P0-5 的成因） | 重写校验策略（能力白名单 + 目录限制）或接 Docker runtime | 🔴 |
| P0-7 | **custom_mcp 连接器写操作无确认闸门**：`should_confirm` 只查 catalog 的 `confirm_actions`，custom_mcp 绕过 catalog | `connector/confirmation.py:67-78` + `connector/manager.py:372-374` | 用户自接 MCP 的破坏性操作无人工确认 | custom_mcp 创建时支持声明 confirm 清单 | 🔴 |
| P0-8 | **personal 技能执行默认开启**：只有显式设 `DBGPT_DISABLE_PERSONAL_SKILL_SCRIPT_EXECUTION` 才禁用 | `agent/skill/manage.py:548-553` | `skills/user/` 下用户上传的脚本默认可执行 | 生产默认禁用，显式开启 | 🔴 |
| P0-9 | **shell 工具硬编码 `LocalRuntime`，绕过沙箱工厂的安全默认**：仓库已有 Docker/Podman/Nerdctl 三种容器 runtime + `RuntimeFactory`（默认"容器优先，无容器则 fail closed"；本地执行需 `SANDBOX_ALLOW_LOCAL_RUNTIME=true` **显式 opt-in，默认 False**），但 `shell_interpreter` 直接实例化 `LocalRuntime` → **任何部署下 shell 都在宿主机执行**；且该"沙箱"实为轻量本地沙箱（30s 超时【有效】+ 内存检查【**仅启动后查一次**，后续增长不受控】+ 11 条 bash **子串**黑名单【易绕过，且只挡灾难性命令、不管越权读取】），**无文件系统隔离、无 rlimit**（`cwd` 仅为相对路径基准；`env=dict(os.environ)` 全量继承宿主机环境变量） | `tools/shell_interpreter.py:34-39,59`（硬编码 LocalRuntime）· `execution_layer/local_runtime.py:205-244`（无 preexec_fn/rlimit；内存检查见 229-239）· `execution_layer/utils.py:89-97`（`ensure_safe_path` 全仓**零调用**=死代码）· `runtime_factory.py:18-78` + `config.py:48`（安全默认被绕过；对照正门用法 `control_layer.py:19`） | 任意 shell 命令可读写宿主机该用户可访问的一切（密钥、其他项目、落盘结果）；"256MB 内存限制"实际失效；黑名单可绕；**多用户部署下用户间无隔离**（A 的 agent 可读 B 的数据源/凭证/落盘结果，见 A.7 定位修正） | 改一行用 `RuntimeFactory.create()`（默认走容器）；为 LocalRuntime 补 rlimit + 路径约束 + 持续内存监控；容器不可用时按 factory 语义 fail closed；补策略层：网络默认关（`SessionConfig.network_disabled` 现默认 False）、可写根内的提权路径强制只读（参考 Codex `WritableRoot`：`.git/hooks`、`.codex` 等）；**接线时改隔离粒度**：现为"每次工具调用新建 session"，换容器后会成"每条命令一次 `docker run`"（秒级）→ 改为会话/任务级复用容器或预热池 | 🔴 |

### P1 —— 架构收敛（需要一定改造）

| ID | 问题 | 证据 | 影响 | 建议 | 性质 |
|---|---|---|---|---|---|
| P1-9 | **轨迹记忆"存-读-还原"有损往返**（"记忆"= 每轮 ReAct 轨迹片段，非长期记忆）：`GptsMessage` 会持久化 `message.content`（`base_agent.py:993-1045`），但恢复查询最终只返回配对记录中的 `ActionOutput`（`gpts_memory.py:152-179`）；随后 `recovering_memory` 仅回填记忆片段（`role.py:472-484`），由 `react_agent.py:274-365` 拼成新的 `AgentMessage`。片段保存 `{thought, phase, action_intention, action_reason, action, action_input, observation, persisted_path}`（`role.py:278-350`；`question` 仅第 0 轮存），**`action_intention`/`action_reason` 写了但模型历史还原未读回**；原始内容虽有持久副本，却没有作为原消息序列恢复给模型。`GptsMemory` 有持久轨迹，不等于原样保存/回放模型请求 | 写入 `core/base_agent.py:993-1045`、`core/role.py:278-350`；按 `conv_id` 读取 `memory/gpts/gpts_memory.py:152-179`、恢复 `core/base_agent.py:245-259`、`core/role.py:472-484`；消息重建 `expand/react_agent.py:274-365`；落地前兼容性见 [D.8](#d8-会话记录落地前兼容性核对第-5-轮2026-09-28)，并发边界见 [D.9](#d9-本机配置与同会话并发核对第-6-轮2026-09-28)，单表评审规格见[会话记录设计文档 §10](DB-GPT-Agent-Rollout会话记录与上下文恢复设计.md#10-agent_event-单表评审规格第-7-轮) | 轨迹可持久化和续接，但恢复成模型输入时有损；不能把“数据库里有 trace”当成“原始对话消息可逐字回放” | 直接 append 标准消息（assistant.tool_calls + tool 结果原样入列）；将审计轨迹与模型续聊消息明确分开。第一阶段只恢复协议历史，不做执行检查点/工具重放；实施规格与未决项见[会话记录设计文档 §8-10](DB-GPT-Agent-Rollout会话记录与上下文恢复设计.md#10-agent_event-单表评审规格第-7-轮) | 🔴 |
| P1-10 | **历史重建靠奇偶配对**：`i % 2 == 0 → HUMAN`，假设严格交替 | `core/base_agent.py:1417-1425` | 任一环缺失即角色错位 | 显式存 role，不用位置约定 | 🔴 |
| P1-11 | **system prompt 混入运行时数据**（文件清单/表结构/知识库描述；**12 段完整内容清单见附录 C**） | `DB-GPT@ca9f014:packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py:2790-2792,2943-2957`（动态块进入同一 `workflow_prompt`，再绑定为 agent prompt）；OpenAI 官方文档说明缓存复用依赖相同 prompt 前缀，见 [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)（访问 2026-09-24）。动态块之后的内容无法继续与值不同的请求形成相同前缀；**实际缓存命中率/成本影响未测**，且各 provider 行为不同 | 可复用前缀在首个变化块处结束；当前没有命中率证据，不能断言缓存命中率低 | 保持稳定指令和工具定义在前；将变化数据放到稳定前缀之后的独立消息/片段；按 provider 记录 cache-read 指标验证收益 | 🔴 |
| P1-12 | **压缩 Layer3 同步调 LLM 做摘要**：在 agent 循环内阻塞 | `core/context/manager.py:9-14, 49-52` | 长任务额外延迟尖刺 | 优先级：① 调阈值让确定性裁剪（L1/L2）先行、L3 罕触发；② 摘要移出关键路径（回合边界/后台）或用小模型。**注意：业界并无"异步预压缩"标准，属优化项**——Claude Code 的 auto-compact 也是阻塞式（带进度提示），故本条为权衡而非缺陷 | ⚖️ |
| P1-13 | **单文件 3900+ 行**：prompt 拼接、SSE、工具定义、HTTP API 全在 `agentic_data_api.py`；且残留 `.bak` | 文件本身 + `agentic_data_api.py.bak` | 可维护性差、review 困难 | 拆分为 prompt_builder / sse / tools / api 模块 | 💭 |
| P1-14 | **吞异常成风**：关键路径大量 `except Exception: pass  # graceful degradation` | `:1494-1495`（business tools）· `:2434-2435`（connector tools）· `:2939-2940`（connector prompt） | 故障静默，排障困难 | 至少记 warning + 埋点指标 | 🔴 |
| P1-21 | **shell/code 执行无审批层**：行业做法是"沙箱 + 审批"两道**互补**防线（Codex：4 档 `SandboxPolicy` + approval policy；Hermes：确认机制），本项目只有 MCP connector 有确认闸门（P0-7），`shell_interpreter`/`code_interpreter` 直接执行高危操作（删文件、出网、装包） | `tools/shell_interpreter.py`（无确认）· `tools/code_interpreter.py`（无确认）· `connector/confirmation.py`（仅连接器）；对照 `codex-rs/protocol/src/protocol.rs:1004-1045` | 无沙箱时（P0-9）风险叠加；用户对高危操作无介入点 | 引入分级审批（只读自动 / 写确认 / 网络确认 / 危险命令确认），与沙箱策略分离 | 🔴 |
| P1-19 | **工具结果"三层防御"移植不完整**（hermes-agent → DB-GPT，见 D.2）：第三层（单轮聚合预算）与窗口自适应**均无调用者**——`enforce_turn_budget` 全仓仅定义、`tool_result_budget_for_context_window` 仅导出；`ContextBudgetConfig.tool_result_threshold` 恒为 100K 字符 | `core/context/storage.py:382`（无调用者）· `:150`（无调用者）· `core/context/budget.py:58` + `base_agent.py:140`（固定取值，未按窗口缩放）；对照 Hermes `agent/tool_executor.py:1500/2263/2326`（三处调用）+ `_budget_for_agent()`（实时缩放） | 小窗口模型（32K/65K）阈值不收缩，单个结果仍可按 100K 字符（≈25K token）进上下文（Hermes #23767 修的正是此问题）；"多个中等结果累加爆窗口"无防护 | 接线即可（低成本）：① `ContextBudgetConfig` 构造时用 `tool_result_budget_for_context_window(max_context_tokens)` 派生阈值；② 每轮工具结果收集完后调一次 `enforce_turn_budget` | 🔴 |

| P1-22 | **长期记忆：语义记忆系统存在，但主链路未接入**。须与 ReAct 轨迹区分：主链路创建 `AgentMemory(gpts_memory=...)`，其 `memory=None` 才回退 `ShortTermMemory(buffer_size=5)`；该 buffer 的 `read()` 返回当前片段、不按 observation 检索。与此同时，`GptsMemory` 另将 action trace 持久化到 DB，agent build 时按 `conv_id` 读取并把片段恢复到短期 buffer（见 P1-9）——所以“短期记忆 buffer 是内存态”**不等于**“ReAct 轨迹没有持久化”。`_task_progress` 仅是 Agent 对象内的非序列化列表，供同一执行过程的 retry rounds 使用；不能据此说跨请求恢复了进度。**真正的长期语义记忆实现存在于**：`memory/long_term.py`（LongTermMemory + 时间加权检索）、`memory/hybrid.py`（sensory+short_term+long_term；Chroma 落盘 `agent_memory_long_term`；溢出写入长期层）——本主链路未将 HybridMemory 接入；旧 Agent 接线见 `dbgpt_serve/agent/agents/controller.py:153`（另有独立 API `agent/chat/api/endpoints.py`） | 主链路构造与恢复 `agentic_data_api.py:2310-2319`、`core/base_agent.py:245-259`；buffer 默认与读取 `memory/agent_memory.py:303-312`、`memory/base.py:705-765`；持久 trace 读取/回填 `memory/gpts/gpts_memory.py:122-139,152-179`、`core/role.py:472-484`；`_task_progress` 进度 `core/role.py:59-62,212-240,351-356`；Hybrid/旧路径 `memory/long_term.py:24,181`、`memory/hybrid.py:113-144,229-231`、`dbgpt_serve/agent/agents/controller.py:153` | 主链路缺少跨会话偏好/事实的长期语义召回；已存在的对话历史和 ReAct trace 是另一类持久化，不应误称长期记忆。恢复出的轨迹再转成消息仍有损（P1-9）；buffer 仅保留有限片段 | 若产品需要跨会话记忆，再接 `HybridMemory`——但**“接线成本低 ≠ 可用成本低”（详见 D.4）**：默认 `memory_importance_scorer`/`memory_insight_extractor` 为 None 且无覆盖（洞察永不生成、重要性退化为常量）、写入路径 importance 缺失即抛错、`clear()`/reflect/forget 均为 TODO（无删除→合规缺口）、`_k=4` 硬编码、decay 0.01（半衰期≈2.9 天）；先修 `ShortTermMemory.transfer_to_long_term` 切片 bug（`base.py:776-783`）再谈接入 | ⚖️ |
| P1-23 | **read_file 的 pinned inf 移植丢了安全前提**：第二层对 read_file **永不落盘**（`storage.py:93-95`，防 persist→read→persist 死循环——原样搬自 Hermes），但 Hermes 里该前提安全是因为工具自带双限制：schema `limit` "default: 2000, **max: 2000**" + 内部 "~100K-character budget" 帽 + next_offset 续读（`file_tools.py:2167`）。DB-GPT read_file（全文 154 行，无任何 MAX/字符帽）`limit` **无 schema 上限、无内部字符帽**（`end=min(start+limit, total_lines)`，默认 100 但可传任意大）→ 大 limit 整文件进上下文且**不落盘**，当轮 LLM 调用前无任何自动防线，只剩下游压缩层善后 | `tools/read_file.py:74-83,133-145`（无帽）· `storage.py:93-95`（pinned inf）· 对照 Hermes `tools/file_tools.py:2167` | 小窗口模型上单次 read_file 即可撑爆上下文（叠加预算缩放未接线 = #23767 场景）；触发后靠 L4 兜底重试，代价是一次失败调用 | ① read_file 加 schema `maximum` + 内部字符帽（对齐 Hermes）；或 ② 解除 pinned 改高阈值；③ 顺手补 per-tool registry 阈值档（Hermes 的 resolve_threshold 有 registry 层且 capped at default，DB-GPT 只有 pinned→overrides(空)→统一 100k） | 🔴 |
| P1-24 | **死循环防护只有硬预算，缺语义层检测与优雅终止**。已有：`max_retry_count`（主链路 30，`base_agent.py:538` while 循环）、墙钟 `max_timeout=600s`（:744，超时按失败处理进入重试）、压缩断路器 3 次、L4 单次重试。缺：① 无同 `(tool, args_hash)` 重复检测（与去重护栏同源，见 D.6 补充 3-D）；② 无失败连击升级（同类错误连续 N 次仍在换措辞重试）；③ 无进展检测（`_task_progress` 里 `status/observation_tokens` 数据现成，连续 failed/零产出即无进展）；④ 耗尽后 = 最后一条失败观察直接作为回复（:752-759），不是"已完成+剩余+阻塞原因"的总结 | `core/base_agent.py:50-51,536-538,744,752-759` | 30 步 × 每步一个失败工具调用 = 大量浪费的 LLM/工具开销；写操作类失败重试有副作用风险；终止体验差 | 升级阶梯：重复/连击检测（1 次→注入"已试过+结果"反馈，2 次→强制换策略，3 次→终止）+ 终止时用 `_task_progress` 生成部分成果总结；多智能体加传递计数 | 🔴 |
| P1-25 | **四层压缩的 L1/L2/L3 在新 rollout 结构下全部不触发；压缩结果未接入事件流**（复核详见 D.15）。主链路已切 rollout（`enable_rollout_history=True`），消息为原生 tool 结构（`role=tool` / assistant `tool_calls`）；而 L1/L2/L3 的门控依赖 `_is_observation_message`（HUMAN + `Observation:` 前缀）与 `_detect_round_boundaries`（以 Observation 闭合 round）——无 Observation 时全部消息成 1 个 round：L1 扫而无获、L2（`len(rounds)≤min_keep_recent_rounds(3)`）、**L3（`keep_rounds=min(3,1)≥len(rounds)=1`）**均直接返回 → 三层 no-op，只剩 L4。另：`agent_event` 事件类型为**封闭集合**（无 compaction 类事件）、压缩不写回事件流（每步 `load_projection` 全量重放）；rollout 分支 `_thinking_native` 由投影重建请求，传入列表只贡献 system + 末条用户兜底（代码阅读结论） | `core/context/compact.py:41-47,55-79,87-137,145-206,214-238` · `core/base_agent.py:1330-1363,1448` · `expand/tool_calling_agent.py:512-545` · `dbgpt_serve/agent/db/agent_event_db.py:35-44,409-410` · `agentic_data_api.py:2504,2553,3156-3173`；对照 D.5 修正与 D.13 待核实③ | rollout 链路上上下文膨胀没有主动防线（只剩 L4 失败后的兜底重试）；D.5/P1-12/P1-23 的"压缩层善后"前提在 rollout 链路失效；压缩每步重算、结果不落事件流（重复计算 + 不可审计） | ① 重写 L1/L2/L3 门控以匹配原生 tool 消息（按 tool_call↔tool_result 配对与消息计数分 round）；② 压缩结果接入事件流（新事件类型 + 投影应用 + token 重算，即 D.6 补充 3-E 的落地）；③ 旧文本路径保留时两套检测共存并回归 | 🔴 |

### P2 —— 长期演进

| ID | 方向 | 理由 | 性质 |
|---|---|---|---|
| P2-15 | **协议适配层**：Chat Completions 为默认，Responses / Anthropic 作为可选适配器 | 未来接入 Responses 生态（Codex 兼容等）不必重写；但当前不必强推 | ⚖️ |
| P2-16 | **上下文策略可插拔**：把四层压缩抽象成接口，允许接第三方（provider 能力 / LangGraph 式 checkpointer） | 现在绑定在 `ContextManager` 内部，无法替换 | 💭 |
| P2-17 | **上下文分层显式化**：把上下文管理固化成显式层对象/接口（`instructions / tools / items / state / compaction / memory` 为对各 API 表面形态的归纳，**非官方标准**，仅作命名参考） | 降低耦合，便于替换与测试 | 💭 |
| P2-18 | **工程卫生**：清理 `.bak`、`[CTX-DEBUG]` 调试日志（`core/context/manager.py:115`）、重复的 `DEFAULT_SKILLS_DIR` 定义（`agentic_data_api.py:74` 与 `subagent/react_tools.py:42`） | 低风险清理 | 🔴 |
| P2-20 | **自截断工具的结果不可恢复**：①（工具内自截断）是唯一有损层——被截内容框架从未见过，②③与 `read_file` 均无法找回；且截断说明不含"如何取回剩余"指引（`sql_query` 仅报 "truncated at 20000 chars. Total rows: N"，未提示可 `LIMIT/OFFSET` 翻页） | `tools/sql_query.py:87-93` · `tools/shell_interpreter.py:95-99` · `tools/knowledge_retrieve.py:40-41` · `tools/load_file.py:17-18`；对照 Hermes `search_files` 的 `limit/offset` 分页形态（见 D.2） | 大结果永久丢失（shell 输出类不可重放）；模型不知道可重查/翻页 | 截断说明附"取回指引"（分页参数），或把硬截断改为分页参数（同 `kb_cat` 的 `start_line/end_line`）；另：① `sql_query` 的①是"取数后切片"（SQL 原样执行 × 不省查询成本），可改为注入/要求 `LIMIT`（把限制推到数据源）；② `read_file` 缺 `pattern` 参数——落盘文件无法 grep 定位只能盲翻页，可加 `pattern` 或让 grep 类工具覆盖 `persisted_results/` 目录；③ `kb_cat` 的 `end_line` 默认 0（到文件尾）→ 默认无界，应给默认值或加① | 💭 |

---

## 4. 合理权衡（避免误伤，这些设计是对的）

| 设计 | 为什么合理 |
|---|---|
| ⚖️ **文本 ReAct 作为兜底协议** | 平台要对接几十家供应商 + 支持本地 vLLM 部署；作为"兼容底座"保留有合理性——问题只在于**不该同时把结果回填也文本化**（P0-1） |
| ⚖️ **自研四层压缩** | Chat Completions 路线下拿不到 provider 的 truncation/context editing；客户端压缩是**可移植**的选择，且深度不差（有预算追踪 + 状态上报） |
| ⚖️ **大输出落盘 + `read_file` 按需读回** | 移植自 hermes-agent 的成熟做法（三层防御设计，比"直接截断"强）；**但移植不完整**——只接上了第二层，第三层与窗口自适应未接线（见 P1-19 / D.2） |
| ⚖️ **附件"重解析而非回放"**（v2 契约：later turns resolve files fresh from the registry） | 避免历史脏数据；同时只存公开元数据，安全上更干净 |
| ⚖️ **继续使用 Chat Completions** | 生态兼容优先；provider 专有能力（truncation/context editing）性价比低，不必接入——**学做法、不绑协议**即可 |

---

## 5. 演进路线图建议

**阶段一（P0，约 1-2 周）**：收敛双轨缝合 + 修复漂移 + 补齐安全闸门
- P0-1 native 结果回填标准 tool 消息（与调用闭环）
- P0-2/P0-3/P0-4 prompt 与 ToolPack 同源生成，按模式产出
- P0-5/P0-6 统一沙箱：Python 工具接入 SandboxRuntime（先加资源限制）
- P0-7/P0-8 安全默认值调整（custom_mcp confirm、personal skill 禁用）

**阶段二（P1，一个季度）**：形态标准化
- P1-9/P1-10 将可续聊消息改为 append-only + 显式 role；审计 trace 单独持久化，勿把 trace 恢复误当成原始消息回放；语义记忆外挂化（是否接入 HybridMemory 另作产品决策）
  - **P1-9 实施门槛（D.7/D.11-D.14）**：模型交互事件按序 append，工具调用先存后执行，工具结果逐项记录。实时多步请求的模型上下文由 `assistant.tool_calls` 与 `tool_result` 组成；进程中断后的新请求必须保留未完成 turn 的全部已知内容，只对缺结果的具体 call_id 做未决处理；严禁跳过整轮、不重放旧工具。恢复投影对缺结果 call 按原 `tool_call_id` 补 prompt-only `role=tool` 文本结果，Chat Completions 的协议形状有官方 schema/example 支持；占位文案（Codex `aborted` 或明确的 outcome-unknown 文本）是提示语选择，合成项不写入权威事件日志。以确定性单测验证投影即可进入实现；第三方 provider 兼容属于常规集成测试，不是前置 PoC。旧会话不迁移，按明确 cutover 范围清理并从空的新会话开始。rollout 分支关闭 GptsMessages 跨请求回填，也不把同一工具结果重复注入成 `Observation:` ReAct memory。若未来确需摘要/跨 turn 派生记忆，必须有来源 turn/event，且证明不是协议历史的重复并遵守完整 turn 边界。实现前仍须评审事务序号分配/同会话单活动轮次（轮内工具仍可并行）、fail-closed、旧数据清理及敏感数据保留/脱敏策略。
  - **文档分工**：D.7 是实现现状与差距审计，D.10 是单表规格摘要，D.11-D.13 是此前调查记录（其中“只恢复最后完整 turn 前缀”的建议已被 D.14 明确取代），D.14 记录恢复边界纠正；详见[会话记录设计文档 §10-11](DB-GPT-Agent-Rollout会话记录与上下文恢复设计.md#11-旧历史切换与中断工具调用的方案比较第-8-轮)。仍是评审稿，不是已批准 DDL；没有执行数据删除或代码改造。
- P1-22（若产品决定实现用户级跨会话语义记忆）：先明确用户/租户作用域、记忆写入/更新/删除与权限策略；跨会话验收测试随功能实现一起加入（不同 `conv_id`、同一用户可召回；不同用户隔离；新事实覆盖旧事实）。当前源码已确认主链路缺少该能力，不必先单独运行测试重复验证。完整外部基准应在跨会话能力接通后再跑；此前可设计用例，但不报告长期记忆质量分数。
- P1-11 稳定 system 指令/工具定义前缀，把动态运行时数据放到其后；按 provider 采集 cache-read 指标后再评估收益（附录 D.3）
- P1-12 压缩优先级调优（确定性裁剪先行；摘要有条件时移出关键路径/用便宜模型）
- P1-14 异常治理

**阶段三（P2，长期）**：架构收敛
- P2-15/16/17 协议适配层 + 上下文策略可插拔 + 分层显式化

---

## 附录 A：业界对标依据（调研摘要，2026-09）

### A.1 协议现状

- **Chat Completions 仍是事实标准**：DeepSeek / Kimi / Qwen / GLM / vLLM / Ollama 等绝大多数厂商与推理栈对外暴露 OpenAI 兼容 Chat Completions；OpenAI 声明**无限期支持**
- **Responses API 是增量方向**：OpenAI 推荐新 Agent 项目优先使用；**Codex CLI 只支持 Responses**；阿里云百炼已提供 OpenAI 兼容 Responses；Ollama 2026 年补齐；第三方厂商大多仍只有 Chat Completions（因此出现 Responses↔Chat Completions 协议桥项目）
- 结论：**存量看 Chat Completions，增量看 Responses**；本项目选 Chat Completions 合理

### A.2 工具协议：字符串解析为何被业界淘汰（7 条）

1. 解析脆弱：格式漂移（Markdown 包裹、中英文冒号、换行变化），嵌套 JSON 易被正则切坏
2. 类型约束缺失：拿不到 enum/required/类型；原生可用 JSON Schema + 约束解码在生成期收敛
3. 并行调用困难：文本要多 Action 需自定义语法；原生 `tool_calls` 天然数组
4. 流式处理：文本要在增量 token 上做半截语法解析；原生有明确 delta 事件
5. 可观测性：结构化字段可直接 trace/校验/统计；文本是"事后猜测"
6. 成本：格式说明 + few-shot 常驻 prompt；原生放 `tools` 参数且可被 prompt caching 复用
7. 安全：文本伪造 Action 成本低；结构化 tool_calls 由 API 侧产出，伪造门槛高

### A.3 上下文管理维度对照（本项目 vs 业界）

> **说明**：下表 8 个维度是本文档**自建的分析框架**（生命周期视角），**并非 OpenAI / Anthropic 官方发布的分层模型**；业界有影响力的公开分解见 A.5，各家的层次划分并不统一，不存在"标准 8 层"。

| 维度 | 本项目 | 评价 |
|---|---|---|
| 会话持久化 | `StorageConversation` + 结构化 history_payload + `MetaDbGptsMessageMemory` | ✅ 覆盖 |
| 会话重建 | 历史 Q&A 还原；附件重解析（不扫历史） | ✅ 覆盖，附件策略合理 |
| 运行时写入 | 每轮 `write_memories` 序列化片段 | ⚠️ 有损重构 |
| 上下文组装 | `_load_thinking_messages` 多分支拼接 | ⚠️ 逻辑分散、边界微妙 |
| 分层 | 隐式（消息顺序约定） | ❌ 缺显式层模型 |
| 压缩 | 自研四层 + 预算 + 状态上报 | ✅ 方向对、深度不差 |
| 可观测 | `context.status` SSE + span metadata | ✅ 优于多数框架 |
| 安全治理 | 附件 URI/hash 隔离、分享脱敏、读白名单 | ✅ 有专门设计 |

### A.4 Anthropic 数据点（官方）

- Context Editing（自动清理过时工具调用）：单独使用性能 **+29%**
- 与 Memory Tool（文件式外部记忆）结合：**+39%**
- 100 轮评估中 token 消耗 **-84%**
- 启示：**策略可客户端实现**（清理过期工具结果 ≈ Layer1 升级；文件式记忆 ≈ `persisted_results/` + `read_file` 扩展），**不必绑定协议**

### A.5 业界公开的上下文分解（参考，非单一标准）

> 业界**不存在统一的分层模型**；以下是几份被广泛引用的公开框架——注意它们**大多是"策略/组件分类法"，而非"分层模型"**：

- **Anthropic《Effective context engineering for AI agents》（2025-09）**：上下文是有限资源，策略四件套——compaction（压缩）、structured note-taking（结构化笔记/外部记忆）、sub-agent architectures（子代理隔离）、just-in-time retrieval（按需检索）
- **Manus《Context Engineering for AI Agents》（2025-07）**：工程实践向——KV-cache 友好（稳定前缀）、append-only、文件系统当上下文、todo 复述目标、保留错误现场
- **LangChain《Context Engineering for Agents》（2025-07-02，已核对原文）**：核心是**三类上下文**（Instructions / Knowledge / Tools）× **四种策略**（Write / Select / Compress / Isolate），并映射到 LangGraph 原语（State / Checkpointer / Store / 子图隔离）；`episodic/semantic/procedural` 记忆三分法出现在 *Select* 小节，`scratchpad` 是 *Write* 下的机制——**并未提出分层模型**，只有 thread-scoped 短期 vs 跨会话长期的时间二分；另有 Karpathy 的 CPU/RAM/OS 隐喻
- **API 表面形态**（OpenAI Responses / Anthropic Messages）：**两家均未发布分层模型**，只定义**请求字段**——OpenAI：`instructions` / `input` items（message、function_call、function_call_output、reasoning）/ `tools` / `previous_response_id` / `truncation` / `context_management`；Anthropic：`system` / `messages` content blocks（text、tool_use、tool_result、thinking）/ `tools` / `context_management`（如 `clear_tool_uses_20250919`，beta）。属"接口能力清单 + 服务端状态/压缩能力"，不是认知模型；大家"感知到的分层"多来自 harness 层（CLAUDE.md/AGENTS.md、记忆文件、todo、压缩摘要）

> 对本项目的启示：**层次怎么分不是关键，关键是把三类内容分开管理**——"稳定/可缓存"（指令+工具）、"结构化可回放"（对话与工具调用记录）、"可外部化"（记忆与中间产物）。

### A.6 MCP 工具的接入方式（业界 vs 本项目）

- **两种集成模式**：
  - **客户端侧（主流）**：宿主应用做 MCP 握手 + `list_tools`，把工具注册进本地工具表并命名空间化——`mcp__server__tool`（Claude Code 与本项目同约定）
  - **平台侧**：API 平台直连 MCP server、动态发现工具——OpenAI Responses 的原生工具类型 `{"type":"mcp","server_label":...,"server_url":...,"require_approval":"never"}`；Anthropic MCP connector（beta）
- **共同点**：模型只面对"工具"（`{name, description, parameters}`），**对 MCP 协议机制（握手 / JSON-RPC / 鉴权 / server 地址）无感知**——这正是 MCP 的设计目标（协议是 host↔server 之间的事）
- **"无感知"的精确边界**：工具名通常带来源痕迹（`mcp__` 前缀 / `server_label` 前缀）、平台侧有 `mcp_call` / `mcp_approval_request` 等 item 类型——这些痕迹是宿主/平台**故意留的**，用于路由、权限、UI 展示
- **本项目的差异**：注册方式正确（当普通工具 + 命名空间），但**额外在 prompt 里手写了工具名/描述/参数 schema**——与 tools schema 双份维护（同 P0-2 病根）；其中只有两条是 schema 给不了的策略信息：① 路由（"直接调用、不走 `execute_tool`"）② 预期管理（"写操作需确认"），可压缩成一两句保留。业界对这两件事的做法是**机制层控制**（Claude Code 的工具级 allow/deny；OpenAI 的 `require_approval` 字段），而非提示词重复描述

---

### A.7 沙箱技术选型（2026-09 调研）

**桌面 coding agent → OS 原语**（内核自带、毫秒启动、按路径细控）：
- **Codex**：macOS **Seatbelt** / Linux **Landlock + seccomp**（本机源码 `core/src/sandboxing/`；`protocol.rs:1004` 四档 `SandboxPolicy`；含 `WritableRoot` 提权防护）
- **Claude Code**：sandboxed bash tool = Linux **bubblewrap** / macOS **Seatbelt**，提供文件系统 + 网络隔离（官方文档 `code.claude.com/docs/en/sandboxing`）
- **Qwen Code**：macOS Seatbelt（6 种 profile）+ Docker/Podman 选项（**默认不启用**；其仓库文档列出的改进方向：补 Linux bubblewrap/Landlock、默认启用、网络默认阻断）
- **原因**：不能强迫用户装 Docker；sandboxed bash 每条命令都过一遍（容器启动太慢）；需细粒度路径授权。macOS 无 Linux namespace，跑 Linux 容器需 VM → 桌面在 Mac 上只能靠 Seatbelt
- **修正**：不少桌面工具**同时**提供容器选项（Qwen Code docker/podman、Codex `ExternalSandbox`、Hermes docker 后端），但默认路径是 OS 原语或"本地 + 审批"
- **Claude Code 官方文档细节**（可作"策略层"设计的参照）：默认"**只允许写工作目录 + 整机可读**"（凭据文件如 `~/.ssh`、`~/.aws/credentials` 需自行封堵）；**受保护路径**（即使在可写目录内也禁写）：`.claude` 设置/skills/hooks、`.mcp.json`、`.bashrc/.zshrc`、`.gitconfig`、**`.git` 内 hooks/config**、`~/.claude` + `.credentials.json`——与 Codex `WritableRoot` 同一思路；网络走沙箱外代理、零预允许域名、首次询问；依赖缺失默认**回退为非沙箱执行**（`failIfUnavailable` 可改硬失败）；官方明确"权限规则管'工具是否运行'（运行前、按命令字符串），沙箱管'进程能访问什么'（OS 级强制）"= **沙箱 + 审批两道互补防线**

**服务端 → 分两档，按"信任边界"选**（判据：**代码信任级别 + 逃逸后果**，而非"服务端 vs 桌面"）：
- **基线档（容器，Docker/Podman/Nerdctl/K8s）**：企业内/单租户自部署的默认选择。价值：镜像=环境一致 + 可丢弃；可编排单位=弹性扩缩。三者**隔离级别相同**（均共享内核），差别仅在工程形态（Docker 有 daemon、Podman daemonless/rootless、Nerdctl 靠 containerd/K8s）
- **强隔离档（换掉隔离边界）**：**microVM**（E2B=Firecracker——"沙箱即产品"的托管平台，公开资料：Perplexity/Manus 采用，预热快照 ~150ms；另有 Kata、Vercel Sandbox、Fly Machines）｜**Daytona**（托管沙箱：容器型 + **VM 型**（VM 型 `pause` 可保内存）；官方定位 "full composable computers — sandboxes — for AI agents"）｜**Modal**（官方称 "**secure containers**"；底层隔离技术**官方文档未披露**——第三方常记为 gVisor 但未获原文证实；另有 "VM Sandboxes (Beta)"）｜**gVisor**（Northflank 等采用；用户态内核拦截 syscall，官方立场 "**Containers are not a sandbox**"）——用于跑不可信用户代码/多租户：容器共享内核，**一次逃逸 = 宿主机 + 全部租户沦陷**；microVM 用独立内核、gVisor 直接换掉边界
- **第三档在兴起**：WASM / 进程级软隔离（"沙箱三条路：容器、microVM、WASM/进程级"）
- **托管沙箱服务（独立一档，与"OS 原语 / 自建容器"并列）**：**E2B / Modal / Daytona / Fly Machines**——API 创建沙箱、跑在他人基础设施、自带快照/编排/弹性/计费；是 agent 项目"免运维换强隔离"的捷径（对照：OS 原语=桌面自带；自建容器=本项目档位；托管服务=云产品）
- **dbgpt-sandbox 的"自研程度"定性（核实）**：**自研 = 管理外壳**（四层架构、多后端统一抽象、会话生命周期 create/destroy/list、TTL 逻辑、依赖安装、文件取回、5 个自建镜像定义）；**借用 = 隔离引擎**（Docker 用 `docker` Python SDK；Podman/Nerdctl 为 **CLI 封装**（`create_subprocess_exec`）；Local 为 Linux 子进程；基础镜像用官方 slim 镜像）；**缺失 = 高端能力**（全包 grep 无 `snapshot/pause/resume`；无编排/池化/自动扩缩——唯一 "`process_pool`" 是 PID 记录；无微VM档；`network_disabled` 默认 False；无路径策略）。即"**自研沙箱管理框架 ≠ 自研沙箱技术**"；与 E2B 的差距是**能力集不同**（快照/微VM/编排）而非实现优劣 → 内网场景接线自建容器档够用；要快照/弹性/微VM 则接托管服务远比自补现实
- **框架 vs 容器引擎的分工（"谁管什么"的原则）**：容器引擎（Docker/Podman/Nerdctl/内核）只提供**机制**——namespace（文件/网络/PID/权限的"视图"）、cgroup（资源封顶）、seccomp（内核过滤）、镜像构建/分发、容器生命周期原语（run/stop/rm）；**框架负责策略与编排**——建什么镜像、限额多少、挂什么卷、能否出网、何时回收、谁审批。对照：六个维度表中"执行"全在引擎、**框架只负责传参**；9 件事表**全部归框架**（引擎一律不管）→ 这就是"装好 Docker 只完成第一层"的确切含义；且本地后端（LocalRuntime）里六维度几乎全失守——框架**实现不了**内核级隔离，"用本地后端"≠"用沙箱"
- **dbgpt-sandbox 框架质量评估（"自研外壳"实现得怎么样，2026-09 核实）**：**架构亮、实现原型级**。亮点：分层清晰（execution/control/user/display）+ 统一抽象 + 多后端适配 + `RuntimeFactory` 安全默认 + per-task `asyncio.Lock`（`control_layer.py:43`）。问题：🔴 **服务无鉴权**（`service.py` 无 token/身份校验，`main.py` 默认绑 `0.0.0.0`——"执行任意代码的服务"裸奔）｜🔴 空闲回收未接线 + **无孤儿容器清理**｜🔴 `self.tasks`/`task_locks` 无清理（disconnect 只改 status 不删、`result` 常驻 → 内存增长，`control_layer.py:132-143`）｜🟡 LocalRuntime `os.environ.update()` 改**进程全局**环境（跨会话污染，`local_runtime.py:62-65`）｜🟡 `session_id` 直接拼容器名（无 sanitize）｜🟡 无 session 配额/并发上限｜🟡 `_handle_manual` 硬编码 URL（`:128`）
- **"同一套框架"的准确含义（外壳相同、能力不对齐）**：外壳（RuntimeFactory → 抽象 → ControlLayer → UserLayer）对所有后端一致；但各后端独立实现 `start/stop/execute/...`，**能力与保障强度并不对齐**：`install_dependencies` 仅容器后端有（local 走 base 默认"未实现"）；**`get_file_content` 仅 Docker 实现**（podman/nerdctl/local 调用即 AttributeError → 服务 `/get_file` 在这三个后端下失败）；危险命令黑名单**只在 local 生效**（容器后端以隔离代替）；`max_memory` 语义不同（local 一次性检查 vs 容器 cgroup 强制）→ "换后端"= 换能力集，非仅换引擎（抽象泄漏）
- **修正一个常见误判**：**"每条命令新起进程"不是缺陷**——Hermes 同为 spawn-per-call（`environments/base.py:1-7`："Unified spawn-per-call model: every command spawns a fresh `bash -c` process"），DB-GPT 文档亦把 "no persistent shell state between calls" 写成设计特性。真正的差别是**沙箱环境是否跨调用存活**：Hermes 环境**按 agent 会话存活**（`_resolve_container_task_id`，`terminal_tool.py:1274-1297`：顶层 agent 传 `task_id=None` → key=`"default"`；子代理 ID **故意折叠回 default** 以**共享**父级常驻容器——"one bash, one /workspace, one set of installed packages"；仅 RL/benchmark 经 `register_task_env_overrides` 按 rollout 隔离）+ 命令前重放会话状态（env/别名/函数/CWD）；另注 **task ≠ turn**（`turn_context.py:448-456` 同时生成 task_id/turn_id，turn=一次 LLM 回合）；DB-GPT 连环境也重建（每次新 session，状态靠共享工作目录兜着）。启示：接容器档时应保持"环境存活 + 命令无状态"（`DockerRuntime` 本就是常驻容器 + `exec_run` 设计）
- **设计意图与定性（"够用还是缺陷"）**：可见意图 = ①**零依赖开箱即用**（`usage.md`："本地运行时不做容器隔离，适合无容器环境的回退/开发调试"）②多后端可插拔（企业部署预留容器档）③服务化预留（独立 HTTP 服务 + "Agent applications → SDK/API/CLI" 访问层）④"命令间无 shell 状态"是有意的（对齐 spawn-per-call 惯例）。但落地只做最便宜的一档且强化件未接线。**定性分场景**：单机开发/试用（文档自述定位）→ ✅ 可接受；**内网多用户（本项目真实定位）→ ⚠️ 缺陷**（用户间无隔离 + 无配额）；对外 SaaS → 🔴 严重。**不依赖场景的一条**：🔴 **与自身宣传不符**——README "runs skills in **sandboxed environments**"/"execute tasks safely in sandboxed environments"、`shell-interpreter.md` "**enforces sandbox isolation**"，主链路实为无隔离本地子进程；另"每次新建 session"属**无理由的实现退化**（自有"可复用会话"设计未用）
- **术语与边界定义（消歧义）**：层级 = 一条用户消息（turn）→ 一次 agent loop（=一个 task，含多轮 LLM 调用 + N 次工具调用）→ 工具调用 → 每次可能一次 `execute()`。"**环境跨调用存活**"中的"调用" = 一次 `session.execute()`（= 一次工具调用/一条命令）；是否跨多轮对话**取决于 session 边界定义**（定在工具调用/任务/对话三种粒度，环境存活范围随之不同）。DB-GPT 现状：本地实现 = 每次 execute 起新子进程且**连环境也重建**；容器实现（DockerRuntime）= **常驻容器 + `exec_run`**，环境按 session 存活（"完整存在、未启用"的判断成立，但完整性有折扣：缺快照/暂停、回收未接线、`get_file_content` 仅 Docker 有、无配额无鉴权）
- **接入容器档后的缺陷解决矩阵（"能解决哪些"）**：✅ **隔离类大部分可解决**——每容器独立文件系统；Docker 后端只传显式 config 环境变量、**不继承宿主 `os.environ`**（凭证不泄漏）；⚠️ 但需收紧挂载（现挂 `{宿主 tempdir → /tmp, rw}`，`docker_runtime.py:55` 是口子）并重新设计**产物交换**（容器内产物需 `get_file_content` 类通道取出）。❌ **治理类不能解决**——配额（容器只限单环境资源，session 数量/并发仍无上限，一人可创建 1000 容器）、鉴权、审计需框架另补。⚠️ **宣传一致性仅部分解决**——shell 接容器后名副其实，但 `code_interpreter` 仍裸奔（P0-5）需一并接；且无容器环境时 factory 为 **fail closed**，须显式决定"工具报错 or 明确降级"，不可静默回退本地。✅ **"每次新建 session"退化可解决，但前提是同时改粒度**（只换 factory 会更糟 = 每条命令 `docker run`）
- **"可复用会话"的三层状态（澄清"是没实现还是没接线"）**：① 机制 = ✅ **已实现**（`SandboxSession` 抽象 + 常驻容器 + `ControlLayer` 按 key 复用 + 服务 `/connect`→多次 `/execute`→`/disconnect`；官方文档 "Session model" 明确声明"a session can be created once; multiple execution steps can run in the same session"）；② 服务形态 = ⚠️ 可用但**无消费者**（前端/主链路/compose 均不调）+ 无鉴权 + 回收未接线；③ 主链路 = ❌ **未接线**。→ 准确结论：**"实现了但没接线"**（非"没实现"）；且"换容器"不能自动解决——必须**同时**做①接 `RuntimeFactory` ②改 session 粒度与复用（`session_id = f"sandbox_{conv_id}"` + 常驻容器 + `exec_run` + 结束/空闲回收）
- **能力名词与需求判断（补充）**：① **快照（sandbox snapshot）** = 运行时状态冻结：FS 快照（新沙箱从快照秒起，用于预热）／FS+内存快照（精确恢复运行中进程）；**与镜像的区别**——镜像=构建期环境定义（改需重建），快照=运行时现场（装好的依赖/写好的文件/跑着的进程）。用途：冷启动摊销、暂停恢复、分叉（fork）、省成本；Hermes 不自研但**利用后端能力**（`managed_modal.py:163` 传 `"snapshotBeforeTerminate"`；singularity 用 overlay + 快照清单）② **暂停（pause）** = 冻结运行中沙箱（不占 CPU、状态保留、可 resume），区别于 stop（丢内存态）/delete（销毁）/kill（不可逆）；DB-GPT **两者皆无**（grep `snapshot|pause|resume` 零命中）③ **`get_file_content`** = 从沙箱取出文件的接口（产物交付通道——容器内文件宿主看不见；本项目**仅 Docker 实现**，podman/nerdctl 下 `/get_file` 失败；与"落盘+read_file 给模型读"是两码事，此接口是"把产物交给用户"）④ **自动弹性扩缩：本项目零实现**（队列/副本/调度器 grep 零命中；且 session 表在**进程内存**（`execution_layer/base.py:121`、`control_layer.py:20`）→ 重启即丢、多副本需 sticky 路由，**天然单副本组件**）⑤ **需求判断**：弹性扩缩是**部署形态**的需求而非"agent 类型"的需求——本地 agent 不需要（只需生命周期管理）；服务端**小规模自部署也不需要**，需要的是**配额 + 回收 + 隔离**；仅"多租户 SaaS/规模化"才需要（前置：session 状态外置/确定性路由 + 队列池化 + 编排层 + 服务无状态化）。agent 沙箱是**长任务占用**（分钟级），扩缩决策比 HTTP 慢且贵 → 队列+优先级或直接托管
- **文件系统隔离的三种机制（常见误区的澄清）**：① **策略过滤**（Landlock/Seatbelt，= Codex 的 Linux/macOS 实现）——**宿主同一份 FS**，内核逐次检查 `open()`；"看得见但碰不到"（`ls /` 仍可见目录结构；被拒报 `Permission denied`）② **视图构造**（bubblewrap = Claude Code Linux；容器 mount namespace）——用 bind mount 拼出可见子树，"没 bind 的不存在"（`No such file`）；Claude Code 默认"全盘只读 bind + 工作区可写 bind"故视图≈宿主全貌 ③ **容器全套**（Docker/Podman）= ② + **镜像 rootfs（另一棵树）+ cgroup/netns/seccomp**。关键区分：**桌面 agent（Codex/Claude Code）不是"独立文件系统"，而是"宿主 FS 的受限视图/路径策略"**（`SandboxPolicy::WorkspaceWrite` = "Same as ReadOnly but + 工作目录写权限"，ReadOnly = 整盘可读；Claude Code 官方"默认可读整台电脑"同理）——**且这不是"快照"**（快照=冻结/复制状态；此处不复制任何东西）。两条后果差异：**环境一致性**（容器=镜像可复现；桌面=宿主装了什么就是什么）与**错误语义/信息可见性**（策略过滤暴露目录结构）。共同边界：**三者都共享内核**，仅微 VM 有独立内核
- **对标 Hermes `tools/environments/`（同类框架，运维完备度高一个档次）**：**8 后端**（local/docker/singularity/modal/managed_modal/daytona/vercel_sandbox/ssh，Docker 仅其一）；**会话模型 = spawn-per-call + 会话快照重放**（init 抓取 env/functions/aliases 快照、逐命令重放；CWD 用 in-band 标记或临时文件持久化 → 不依赖常驻 shell，远程后端同样成立，`base.py:1-7`）；**回收已接线**：`_cleanup_inactive_envs(300s)` + **后台进程保护**（活跃进程则续期）+ **两阶段清理**（锁内收集/锁外停止，避免 10-15s teardown 阻塞，`terminal_tool.py:1770-1811`）+ `reap_orphan_containers`（标签过滤 + `2×lifetime` 宽限 + profile 隔离 + **多进程安全**，`docker.py:144-234`）；Docker 参数带 `--cpus/--memory/--pids-limit` + **宿主能力探测** + `/dev/shm` tmpfs 修正（Chromium bus error）+ CA 只读挂载 + egress 姿态标签 + label 值 sanitize（≤63 字符往返）。结论：**DB-GPT 架构相当、运维完备度低一档**（回收/清理/鉴权/配额），两者"发动机"（容器隔离）同为借用

**沙箱原理速记（用于理解"管住六个维度"）**：程序与外界交互只经内核 syscall → 沙箱手段有三：①**改视图**（namespace/微VM，"看不见就不存在"）②**加检查**（Landlock/Seatbelt/seccomp/cgroup，动作层拦截而非文本层——同一动作有无限文本表达，黑名单必漏）③**换机器**（远程沙箱）。六维度（文件/网络/资源/进程/权限/内核）= 程序影响系统的全部路径；沙箱 = **机制 + 策略**的组合（Docker 自带机制，策略要自己写）。

**沙箱生命周期（业界主流模式 = 会话/任务级复用）**：模式是"**创建一次 → 任务内多次执行复用 → 在生命周期边界销毁**"，而非"每次执行新建"：
- 样本：**E2B**（`create()` → 多次 `run_code()`/`commands.run()` → `kill()`；支持 pause/resume + 快照）；**Hermes**（`_active_environments[task_id]`——`terminal_tool.py:1869`，注释显式建模"持久后端跨轮次存活 / 非持久后端轮次结束拆除**防泄漏** / 空闲回收器按 `lifetime_seconds` 回收"）；**DB-GPT 自身设计**（`DockerRuntime` 常驻容器 + `exec_run` 多次命令；沙箱服务 `/connect` → 多次 `/execute_code` → `/disconnect`）；**Jupyter kernel** 为经典先例
- 为什么：① **状态保持是功能需求**（多步任务：装依赖→分析→出图→引用图表，每次新建会打断工作流）；② 启动成本摊销（一任务 10-20 次执行）；③ 任务=信任单元=生命周期单元（销毁即无泄漏）；④ 按"任务×时长"计费
- **创建时机（标注：设计建议，非文档背书）**：三个平台的创建均为**应用代码显式调用**（`Sandbox.create()`），官方文档不规定"何时创建"；"首次需要执行代码时懒创建（早建=白付冷启动）"属合理推理，**不应表述为"业界做法"**。文档明确支持的**复用**机制：Modal 官方文档提到"维护 **Sandbox 池**、任务到来时复用"+`from_id()` 重连 + Named Sandbox；E2B 支持"连接到运行中的沙箱"+列表；本项目主链路是 lazy 的例子（`LocalRuntime()` 在工具函数体内），只是懒到了"每次执行"
- **生命周期机制（2026-09 三平台官方文档核实，比"四种策略"更准确）**：
  - **绝对超时**：Modal `timeout` **默认 5 分钟**（上限 24h）；E2B 连续运行上限 Base 1h / Pro 24h（超出用 pause/resume）；Daytona `ttl_minutes`（墙钟，任何状态到点即销毁）
  - **空闲超时**：Daytona `auto_stop_interval` **默认 15 分钟空闲**→停止（文件系统保留可重启），`0`=禁用；Modal `idle_timeout` 需显式配置（无默认；"活动"=跑命令/写 stdin/开隧道）；E2B 无独立空闲概念 → 用"短超时 + **保活续期**"实现
  - **到期动作（可配置）**：E2B 超时**可 auto-pause**（状态无限期保留、pause 重置运行窗口），也可提前 `kill()`；Daytona `auto_pause`（VM 类默认 60 分钟，与 auto-stop 互斥）/`auto_archive`（容器停 7 天）/`auto_delete`（**默认不删**，`0`=停后即删）
  - **保活续期**：E2B `set_timeout` 是"**从现在重新计时**"（非累加），官方推荐用法 = "**每次用户交互调用一次**"（= 以续期实现会话级）；Daytona `refresh_activity()` 重置不活动计时
  - **回收是渐进的**：典型路径 = stop/pause（保状态）→ archive → delete（Daytona 默认不自动删除）——"彻底销毁"是最后一步而非常规第一步
  - **⚠️ 已知坑（Daytona 官方警告）**：后台内部进程**不算"活动"**——长任务（如 LLM 推理 > 15 分钟）无外部交互时，沙箱可能在**任务中途**被自动停止
- 对照：Hermes 非持久后端轮次结束即拆（"to prevent leakage"）+ 持久后端 `lifetime_seconds` + idle reaper（一手源码）；DB-GPT 服务设计 `cleanup_expired_sessions(3600)`（1 小时空闲，**未接线**）
- **粒度选择判据（三力平衡）**：状态共享需求（多步必须连续）× 隔离需求（信任边界要断开）× 资源成本（冷启动 vs 占用）；太小（每次执行）→ 状态断裂 + 反复冷启动（本项目现状）；太大（永不销毁）→ 跨任务污染 + 资源泄漏；刚好（任务/对话）→ 状态连续 + 边界清晰 + 可回收。注："**一次请求 = 一个任务 = 一个沙箱**"只在一次性任务场景成立；多轮对话下会话跨请求 → 沙箱跨请求复用
- 例外/变体：**每次执行级**（执行间独立无状态，如 OJ 评测 4 解法 4 个独立 VM）；**用户工作区级**（Daytona/devcontainer，跨会话存活）；**池化**（实现细节，非粒度）。桌面 agent 无会话级容器，但**策略/审批是会话级**的（承载物不同，模式相同）
- 对本项目的"会话边界"核实（三种定义并存）：① **沙箱 HTTP 服务（设计层）**：`session_id = f"{user_id}_{task_id}"`（`user_layer/service.py:37`），`/connect` → 多次 `/execute`（控制层按 `task_id` 映射同一 session，`control_layer.py:78`）→ `/disconnect` → **任务级**；且 `task_id` 语义**由调用方定义、DB-GPT 未规定**（它只提供"(user_id, task_id) 为 key 的会话容器"机制）；`/configure` 可在会话内装依赖（状态复用的证据）② **主链路 `shell_interpreter`（实际）**：`session_id = f"bash_{uuid4().hex[:12]}"` + 每次调用新建 `LocalRuntime()` + `finally` 销毁（`:58-90`）→ **单次执行级**（一个任务 10 条命令 = 10 个沙箱），未使用自己的会话机制 ③ 子代理工具同②（`subagent/react_tools.py:662-701`）
- **第 6 例"未接线"**：`cleanup_expired_sessions(max_idle_time=3600)` 在 4 个 runtime 均实现、**全仓零调用** → 服务形态下 session 仅靠显式 `/disconnect` 回收（对比 Hermes 的 idle reaper 已接线）
- **区分两个不同层级的东西（避免混淆）**：**(A) "真沙箱"** = `dbgpt-sandbox` 完整设计（容器后端 + RuntimeFactory + 四层架构 + 可复用会话），入口是**独立服务**（`scripts/start_api.sh` / `python -m dbgpt_sandbox.sandbox.main`；`initialize_sandbox` 仅被 `main.py` 调用，未挂进主服务）；**(B) 主链路集成** = `shell_interpreter` 硬编码 `LocalRuntime`（每次调用新建、无文件系统隔离）。**官方文档自述（`docs/docs/sandbox/index.md:185-188`）**："the repo currently contains both: a more complete `dbgpt-sandbox` design for reusable sandbox sessions; a practical app-side integration already using sandboxed execution for tools" → 项目自己承认二者并存；且文档对 B 的措辞（"process isolation, memory limits, security validation"）比实测乐观（内存仅查一次、黑名单 11 条子串、无文件系统隔离）
- **A 的会话语义与调用方（官方）**：`interfaces.md` 定义 `create_session(user_id, task_id, image_type)`（"user_id: 用户标识；task_id: **任务标识**"）、`disconnect_session(user_id, task_id)`（"停止并销毁指定**用户任务**的会话"）→ **会话 = 一个用户的一个任务**；文档 "Session model" 明示意图："a session can be created once; multiple execution steps can run in the same session; installed dependencies remain available; files produced in one step can be reused in the next → **a task is solved through multiple reasoning and execution rounds**"。**调用方**：文档设计的访问层是 `Agent applications → SDK/API/CLI`，但**仓库内无内置消费者**（前端不调、compose 无、主链路不调）→ 现状是"**设计完整、可独立启动、暂无内置消费者**"的组件，`task_id` 语义由调用方定义
- **主流会话语义（对标）**：① 一次 agent 任务/运行（E2B、Hermes、DB-GPT 文档）② 一段对话（Claude Code/Codex、conv_id 类产品）③ 一个用户工作区（Daytona/devcontainer）。**三条对齐原则**：会话 = **状态共享范围**（多步必须共享 → ≥ 任务）= **信任单元**（跨用户必须断开、同用户跨任务最好也断开）= **清理单元**（任务结束/空闲超时可安全销毁）；四者对齐比选哪个粒度更重要
- **修复建议（粒度对齐）**：工作目录已是会话级（`PILOT_PATH/tmp/{conv_id}`），沙箱却还是单次执行级 → 应改为 `session_id = f"sandbox_{conv_id}"`（一对话一沙箱）或等价方案：对话首次执行创建 → 后续命令 exec 复用 → 对话结束/空闲超时销毁；同时接上空闲回收器

**对本项目的定位修正（核实后，此前"自部署 + 单租户"的表述不准确）**：
- 实际是**开源自部署的「多用户」平台，认证靠外部 header（信任前置网关）**。证据：`get_user_from_headers` + `UserRequest.user_id` 贯通 API（`api_v1.py:195`、`python_upload_api.py:112`）；数据按 user_id 隔离（`add_db(db_config, user_token.user_id)`）；`tenant` 字段存在于 agent/plugin 模块（`agent/hub/model/model.py:21`）；认证为 header 注入式（`examples_api.py:129`：`user_id or "default"`）
- **信任边界由部署者决定** → 隔离需求分级：内网/团队（有前置认证）→ 用户**半可信**，但**用户之间仍需隔离**（当前无：A 的 agent 可读 B 的数据源/凭证/落盘结果）+ 审批；**对外 SaaS → 用户不可信 → 必须强隔离档（微 VM/gVisor）**。即"多用户"本身即构成隔离需求，不需要恶意用户（同事误操作也算越权）
- **修复分级**：内网多用户 → 容器档 + 用户/会话级隔离（独立容器或至少 workspace 绑定 + 路径限制）+ 审批；对外 SaaS → 上强隔离档
- **运行模型（核实）**：`uvicorn.run(app, host, port, log_level)` 无 `workers` 参数（`dbgpt_server.py:271`）→ **单进程 + asyncio 协程并发**（≠ 每请求一线程）；工具执行走服务进程的**短命子进程**（`local_runtime.py:210` `create_subprocess_exec`），与服务进程**同用户/同文件系统/同环境变量** → 用户代码与服务器之间无任何边界。要点：**线程/协程回答"怎么并发"，沙箱回答"能碰什么"，两者不在同一维度**；沙箱的容量需求 ≈ "同时在跑代码的任务数"（agent 大部分时间在等 LLM），而非总并发数
- **接容器的实操坑**：`shell_interpreter` 现为**每次工具调用新建 runtime+session**（`:59` 函数体内 `LocalRuntime()` + 每次新 uuid + `finally` destroy）→ 直接换容器将变成"**每条命令一次 `docker run`**"（秒级开销）→ 应改为**会话/任务级复用容器**（或预热池）；`DockerRuntime` 本就为"常驻容器 + `exec_run`"设计（`command="tail -f /dev/null"`），是调用方式不匹配
- **又一个"没接线"**：沙箱包自带 HTTP 服务形态（`sandbox/main.py` + `user_layer/service.py`：`/connect(user_id, task_id)`、`/execute_code`、`/get_file` 等，明显为多用户/多任务设计），但 **agent 主链路无任何调用、docker-compose 也无该服务** → 主链路仍走进程内 `LocalRuntime`（第 5 例"能力存在未接线"，见 P0-9）

## 附录 B：代码位置速查

| 主题 | 位置 |
|---|---|
| 主入口 | `packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py:3835 / 3879` |
| 编排核心 | 同文件 `:1221` `_react_agent_stream_impl` |
| Prompt 组装 | 同文件 `:2469`（skill）/ `:2652`（完整）/ `:2945` 绑定 |
| ToolPack 装配 | 同文件 `:2616 / 2622 / 2819 / 2826` |
| Agent 主循环 | `packages/dbgpt-core/src/dbgpt/agent/core/base_agent.py:472`（循环 `:538`） |
| thinking / 模型调用 | 同文件 `:774` → `:801` `llm_client.create` |
| 记忆写入 / 读取 | `core/role.py:278-350`（写）/ `agent/expand/react_agent.py:274-365`（读） |
| 上下文压缩 | `agent/core/context/manager.py` |
| 工具执行 | `agent/expand/actions/tool_action.py:162 / 280` |
| Python 执行 | `dbgpt_app/.../tools/code_interpreter.py:39-56` |
| 技能脚本执行 | `dbgpt-core/.../agent/skill/manage.py:941-1120`（wrapper `:990-1002`） |
| bash 沙箱 | `dbgpt_app/.../tools/shell_interpreter.py:34-69` + `dbgpt-sandbox/.../local_runtime.py` |
| 沙箱安全校验 | `dbgpt-sandbox/.../execution_layer/utils.py:171-229` |
| MCP 连接器 | `dbgpt-core/.../agent/resource/connector/{catalog,manager,confirmation}.py` |
| 确认机制 | `connector/confirmation.py:67-78` |
| 技能加载 | `agent/claude_skill/__init__.py:426-471`；`agentic_data_api.py:1478-1483` |
| SSE 桥接 | `agentic_data_api.py:2077 / 3046` |
| 收尾与落库 | `agentic_data_api.py:1046 / 3564` |
| 前端消费 | `web/hooks/use-react-agent.ts:71`；`web/utils/react-sse-parser.ts` |

---

## 附录 C：workflow_prompt（系统提示词）内容清单

> 记录"巨型 system prompt"到底塞了什么（对应 P1-11 / P0-2 / P0-3 / P0-4）。全部为代码走查实录。

### C.1 完整模式（未预选技能，`:2652` 起）

| # | 段落 | 内容 | 位置 |
|---|---|---|---|
| 1 | 角色声明 | "You are the DB-GPT intelligent assistant…" + 用用户语言回答 | `:2652-2655` |
| 2 | Autonomous Decision Principles | 6 条自主决策原则（含"强制：报告必须走 html_interpreter"） | `:2657-2671` |
| 3 | Task Management（todowrite 规则） | 3+ 步建计划、每次传全量、单一 in_progress、流转示例 | `:2673-2699` |
| 4 | Available Skills List | 技能清单（name+description；预选则只列 1 条，否则列全部） | `:2701-2702`（数据源 `:1595-1608`） |
| 5 | Skill Execution Norms | 技能使用规范（读 SKILL.md、资源使用时机、执行顺序、报告规则） | `:2704-2731` |
| 6 | Available Tools Description | **手工维护的 17 条工具说明**（编号已漂移：14-17 出现两次；codegraph 段条件注入） | `:2733-2788` |
| 7 | `{file_context}` | 附件清单（编号、file_id、名称、类型、大小、本轮物化路径）——**每轮变化** | `:2790`（数据来自附件预检） |
| 8 | `{knowledge_context}` | 知识库名/描述/可用 kb 工具——**按会话变化** | `:2791` |
| 9 | `{database_context}` | 数据库名/表名/表结构 DDL——**按会话变化** | `:2792` |
| 10 | ReAct Output Format | Thought / Action Intention / Action Reason / Action / Action Input + "禁止输出原生 tool-call 标记" | `:2794-2809` |
| 11 | DISPATCH_PROMPT_SECTION | 并行子代理规则（并行资格判断、todowrite→dispatch 两阶段、禁止递归） | `:2812`（源：`subagent/dispatcher.py:405-484`） |
| 12 | MCP Connector 段（动态追加） | 连接器名/类型/描述 + 每个工具 name/描述/参数 schema + "直接调用、写操作需确认" | `:2863-2938` |

### C.2 技能模式（预选技能，`:2469` 起）

| # | 段落 | 内容 | 位置 |
|---|---|---|---|
| 1 | 角色声明 | "executing the skill task selected by the user" | `:2470-2471` |
| 2 | Autonomous Decision Principles（简化版） | 5 条（严格按技能指令、html_interpreter 强制规则） | `:2473-2487` |
| 3 | `skill_prompt_context` | **SKILL.md 全文**（用户预选技能时直接注入） | `:2489`（数据 `:2381-2386`） |
| 4 | `execution_instruction` | 执行要求（必须按技能步骤、识别每步工具） | `:2490`（数据 `:2387-2393`） |
| 5 | Skill Execution Norms | 资源使用与顺序 | `:2492-2509` |
| 6 | Available Tools Description | 简化版 9 条工具说明 | `:2511-2571` |
| 7 | Task Management（todowrite 规则） | 同完整模式 | `:2573-2590` |
| 8 | file / knowledge / database context | 同完整模式 | `:2592-2594` |
| 9 | ReAct Output Format | 同完整模式 | `:2595-2610` |

### C.3 知识库模式的不一致（对应 P0-4）

`tool_mode == "knowledge"` 时**只替换 ToolPack**（`:2814-2824`），**不替换 prompt 文本**——模型仍会读到 skill/shell/sql/code 等未装配工具的说明。

### C.4 从清单得出的三个结论

1. 12 段中第 4/7/8/9/12 段是**运行时数据**（每轮/每会话变化），与第 1-3、5-6、10-11 段的稳定指令混在同一个 system prompt → **缓存前缀不稳定**（P1-11）
2. 第 6 段（工具说明）与 ToolPack 是**双份维护**，已出现编号漂移（P0-2）
3. 第 10 段（禁 native 标记）与运行时 `enable_native_function_calling=True` **互相矛盾**（P0-3）

---

## 附录 D：一手对标（Codex 源码，克隆于 2026-09-22）

> - 来源：`github.com/openai/codex`（本地克隆 `/Users/zsm/Documents/Nxin/codex`），Apache-2.0，commit `7a0e974`（2026-08-06），浅克隆
> - 目的：把"业界标准 harness"从博客/二手分析升级到**可核验的源码级对照**。按轮次深挖，每轮一节；行号基于该 commit。

### D.1 工具结果与历史组织（对照 P0-1 / P1-9）

**Codex 的源码事实**：

1. **工具结果是标准 Responses item，`call_id` 强配对**（`codex-rs/protocol/src/models.rs:678-714`）：
   - `FunctionCallOutput { call_id, output }`
   - `McpToolCallOutput { call_id, output: CallToolResult }` —— **MCP 结果有专属 item 类型**
   - `CustomToolCallOutput { call_id, name, output }`
   - `ToolSearchOutput { call_id, status, execution, tools }` —— 工具检索（动态加载工具）
   - `Message { role, content, phase }` —— message 带 `phase` 字段
2. **孤儿调用保护**（`core/src/context_manager/normalize.rs`）：`ensure_call_outputs_present` 检测缺失输出，合成 `"aborted"` 输出插在调用之后（语义上告诉模型"调用被中断"，而非"结果为空"）；合成 ID 用 **UUIDv5 + 硬编码 namespace** 从源调用 ID 确定性推导——同一调用永远得到同一 ID，注释明言这为了"normalize 反复运行仍保住 prompt 缓存"（item id 模型可见，属缓存前缀的一部分）
   > **协议说明（避免误读）**：上述 item 形态属于 **Responses API**——Codex 全仓无 `chat/completions` 引用、端点仅 `/v1/responses`（Responses-only，这也是第三方厂商需协议桥的原因）。但 **`call_id` 配对的语义在 Chat Completions 中同样存在**：`assistant.tool_calls[{id,...}]` + `{role:"tool", tool_call_id}`。因此"本项目用 Chat Completions"**不构成**无法做标准配对的理由——P0-1 的修复建议（`role:"tool"` + `tool_call_id`）在 Chat Completions 内即可实现。
3. **结果进入上下文时即按 token 策略截断**（`core/src/tools/context.rs`）：`truncate_function_output_payload(&payload, policy * 1.2)`；`FunctionToolOutput.body` 是 `Vec<FunctionCallOutputContentItem>`（结构化内容项，非纯文本）
4. **历史 = 标准 item 列表**（`core/src/context_manager/history.rs`）：`ContextManager { items: Arc<Vec<ResponseItem>>, history_version, token_info, ... }`，注释为 "Transcript of thread history"，append-only + 写时复制快照；`history_version` 仅在压缩/回滚时 bump

**对照本项目**：

| 维度 | Codex（源码实证） | 本项目 |
|---|---|---|
| 工具结果形态 | 结构化 item + `call_id` 强配对 | `"Observation: xxx"` 文本 human 消息（无 id） |
| 孤儿调用 | 检测 + 合成 `aborted` 输出 | 无配对语义 |
| 结果截断 | **中间截断**（保留首尾，`truncate_middle_*`）+ 警告头（原始 token 数/总行数）+ 图像与加密项保留；策略按 Bytes/Tokens 配置 | **三层防御设计、当前只接线两层**（工具内自截断 → 单结果落盘 100K；**单轮预算与窗口自适应未接线**，见 P1-19 / D.2）+ `<persisted-output>` 预览（1500 字符）+ `read_file` 翻页读回（`read_file` 自身阈值=∞ 防死循环）。血统：移植自 hermes-agent（逐条对照见 D.2） |
| 历史 | `Vec<ResponseItem>` append-only | 记忆片段 JSON「存-读-还原」 |
| 缓存意识 | 显式（连合成 ID 都稳定化） | 无 |

> **D.1 结论**：P0-1 的差距被一手源码证实——标准 harness 的工具结果是**结构化、可配对、可校验**的一等 item；本项目是文本回放。另注：`core/src/context/mod.rs` 有 40+ 个类型化上下文片段模块（留待第 2 轮：系统提示词与上下文组装）。

### D.2 工具结果"三层防御"（对照 P1-19，hermes-agent 源码，本机克隆于 2026-09-22）

> 来源：`/Users/zsm/Documents/Nxin/hermes-agent`——机制在 `tools/tool_result_storage.py` + `tools/budget_config.py`，接线在 `agent/tool_executor.py`。DB-GPT 文件头自述 "Ported from hermes-agent's tools/tool_result_storage.py"。

**Hermes 原版（三层完整接线）**：

| 层 | 位置 | 触发 | 阈值解析 |
|---|---|---|---|
| ① 工具内自截断 | 工具内部（如 `search_files`） | 返回前 | 工具自定；并向 registry 注册 `max_result_size_chars` |
| ② 单结果落盘 | `tool_executor.py:1398/2156` → `maybe_persist_tool_result` | 单结果 > 阈值 | pinned → overrides → **registry per-tool（cap 到 default）** → default(100K 字符) |
| ③ 单轮聚合预算 | `tool_executor.py:1500/2263/2326` → `enforce_turn_budget` | 一轮合计 > 200K | 按大小降序强制落盘（`threshold=0`）直到降到预算内 |

三层各堵一类失败模式：① 堵源头浪费（在工具里就 LIMIT，不生成巨型字符串）；② 堵"单个巨型结果"（一次 grep 50MB）；③ 堵"累加溢出"（多个中等结果合计爆窗口——②永远抓不到）。

**① 是三层中唯一"有损"的一层**：截断发生在工具返回**之前**，框架从未见到被截掉的部分 → ②③ 都救不回来（落盘只能保存"框架看到的内容"），`read_file` 也读不回。DB-GPT 中实际自截断的工具：

| 工具 | 自截断上限 | 可恢复性 |
|---|---|---|
| `sql_query` | 20,000 字符 + 仅前 50 行（告知总行数） | 半：SQL 可加 `WHERE/LIMIT/OFFSET` 重查 |
| `shell_interpreter` | 50,000 字符 | **不可恢复**（命令输出如日志无法重放） |
| `execute_tool` | 20,000 字符 | 视被调工具 |
| `knowledge_retrieve` | 单 chunk 4,000 / 总 20,000 字符 | 可换 query 再检索 |
| `load_file` | 20 文件 × 每文件 4,000 字符 | 不可（附件加载场景） |
| `kb_*`（grep/cat/ls…） | **无自截断** → 走 ②（可恢复）；且自带分页参数（`start_line/end_line`） | ✅ |

- **两处注释与事实矛盾**：① `storage.py` 文件头称 "Tools like sql_query, **kb_cat** pre-truncate their own output"——`kb_cat` 并不自截断（`kb_tools.py` 无任何截断逻辑，且有 `start_line/end_line` 分页）；② `sql_query.py:84-86` 称 "The full result remains available via the ToolResultStorage persistence layer if it exceeds the threshold"——截断后 ≤20K 字符**永远达不到** ② 的 100K 阈值，全文在函数返回时即被丢弃
- 对照 Hermes：`search_files` 用 `limit=50/offset=0` **分页参数**（默认有界 + 可翻页，模型可控），优于硬截断；registry 的 `max_result_size_chars` 只作 ② 的阈值来源（唯一调用点 `budget_config.py:54`，非强制截断）

**为什么是三层而不是一层（设计理由）**：
- **责任分工**：① 归工具作者（唯一具备领域知识、且能在数据产生前动作的人）；②③ 归框架（通用兜底；"整轮视角"只有框架有）。Hermes 原话："Layer 1 ... the only one the tool author controls"
- **① 无法被 ② 替代的四个硬理由**：ⓐ **时间点**——①在结果产生前/产生时生效（可省查库负载/网络/内存/写盘 I/O），②在"50MB 字符串已完整存在"之后才工作（防不住 OOM 与写盘成本）；ⓑ **语义**——②的预览只是字节前缀 `content[:1500]`（框架不懂内容，100K 字符 CSV 的前 1.5K 可能连一行都没走完），①能给出领域最优形态（"表头+前50行+总行数" vs 字节前缀）；ⓒ **前提条件**——②依赖"结果可写盘"（非字符串/敏感数据/无写权限做不到，Hermes 备有写失败兜底）；ⓓ **行为塑造**——①能"逼"模型改用 offset+limit 精准取数（Hermes `file_tools.py:53-56` 原话 "the model should use offset+limit"），②只能事后补救
- **成本结构**：①≈0（一条 `LIMIT`/切片）；②= 写盘 I/O + 预览 + 后续 `read_file` 往返（多一轮轮次）——能①解决的不拖到②
- **③ 的必要性（数学上）**：单结果阈值防不住"累加"——每个都 < 阈值、加总爆窗，只有全局视角能发现
- **模型无关依据**（Hermes `file_tools.py:51-56`）：无法按模型数 token → 用字符做保守代理（100K chars ≈ 25–35K tokens，已是 hazard 级）
- **截断返回规范**：Hermes 截断时返回结构化 `truncated / next_offset / hint`（"Use offset=N to continue"），连"首行超预算、其余不可通过 offset 取回"也明说；对照本项目：`load_file` 有取回提示（`request specific file_ids`），`sql_query`/`shell_interpreter`/`knowledge_retrieve` 无 → P2-20
- **本项目的结构性差异**：`@tool` 装饰器**无大小元数据通道**（`resource/tool/base.py:232-237`，仅 description/args/args_schema）→ 工具上限只能硬编码、与 ② 的全局阈值互相不可见（20K vs 100K 脱节的成因）；且 `sql_query` 的 ① 是"**取数后切片**"（SQL 原样执行，拿到全部 rows 后才 `rows[:50]`/`table[:20000]`）→ 只省上下文、不省查询成本；理想形态是把限制推到数据源（注入 `LIMIT`）

**① 的设计判据（修正版，避免"外部世界规模 vs 模型参数规模"式误判——`sql_query` 虽由外部数据决定规模，但模型可用 `LIMIT` 控制，故该轴不成立）**：
- **判据 A（默认路径是否有界）**：问"模型**不作任何额外限定**时输出是否有界"——`SELECT *`（合法且无界）/`find /`/`kb_cat` 不带 `end_line`（默认 0 = 到文件尾）→ 需保护；`search_files`（`limit` **默认 50**）/`calculate`（数字）→ 默认有界。⚠️ **"有分页参数" ≠ "默认安全"**：kb_cat 有参数但默认无界——判据看**默认值**，不看有没有参数
- **判据 B（①能否比 ② 做得更好）**：语义形态（前 50 行+总行数 vs 字节前缀）、push-down（SQL 注入 `LIMIT`/按行读文件，省查询与内存）、行为引导（把 `next_offset` 算好）→ 成立则即使默认有界也值得做①
- 两问皆否 → 不需要①（`calculate`/`todo_write`/`terminate`）
- **①的四种实现形态**：参数+默认值（首选）> 参数+硬天花板（`read_file`：limit max 2000 + 100K 字符双预算）> 硬截断+通报（不能参数化时）> 不设①纯靠②（本项目 `code_interpreter` 属此列 → 缺口）
- **取回通道（决定"截断后能否找回"）**：分页传参（✅）> 输入重写（SQL 加 `LIMIT/OFFSET`、shell 改 `sed -n`——**须工具提示，本项目缺** → P2-20）> 旁路（`load_file` 的 "request specific file_ids"）> 永久丢失（`knowledge_retrieve` 单个 chunk 的第 4K-8K 字符）
- **落盘文件的部分读取**：设计通道 = 预览（初判）+ `read_file` 分页（读多少，且自带 `use offset=N to continue` 续读指引 + 行号）；**"grep 定位（读哪里）"在本项目缺失**——`read_file` 无 `pattern` 参数，落盘文件只能盲翻页（Hermes 靠独立 `search_files` 可对任意目录 grep，含落盘目录，"定位+读取"两工具各司其职）→ P2-20。注：shell 实际能 grep 落盘路径（grep 在支持列表、黑名单不拦、无路径限制），但那是"**文件系统无隔离**"的副产品而非设计通道——shell 的沙箱只管超时/弱内存检查/窄黑名单（且绕过 `RuntimeFactory` 的安全默认，见 P0-9）；read_file 白名单的存在恰说明设计意图是"读文件走受限通道"

**"落盘 + 按需读回"为什么不多此一举（成本模型）**：
- 事实一：**绝大多数大结果永不读回**——模型从预览就能得到答案（grep 找位置、日志找报错、SQL 看几行）；少数只读一部分（offset/limit 分页）；需要全量是极罕见情况。读回是"选择性的 + 有界的"
- 事实二：**两种做法成本不同量级**——全塞进上下文是"大小 × **剩余轮数**"（每轮请求重新携带直到压缩，乘法）；落盘是"1.5K 预览（一次）+ 实际读回量"（加法）。算例：10 轮任务产生 6 个 100K 结果 → 全塞 600K 字符（≈150K token，首轮即近满）；落盘 ≈13K 字符（≈3.3K token），差 ~45 倍，且全塞方案长任务跑不下去（卡在压缩循环）
- 事实三：它保护的是**任务可持续性**而非单次请求——上下文被大结果占满 → 压缩频繁触发（有损/慢）→ 模型质量下降（长上下文迷失）→ 最终超限报错；Hermes 的 `context-budget-discipline.md` 整篇讲这套纪律（"Read depth scales with context window"、50% economize、70% checkpoint），落盘是它的物质基础
- 本质：**上下文 = 贵且有限的工作台，磁盘 = 便宜且无限的仓库**，`read_file` = 仓库的取货口；把"一次性付全款 + 永久占用"换成"少量押金（预览）+ 按需付费 + 用完即走"
- 附带收益：可寻址（有路径的文件可被分页/grep/再处理）、可审计（原文留盘）、压缩友好（1.5K 语义清晰的预览块 vs 600K 原文只能整块丢或摘要）
- 代价（诚实面）：写盘 I/O + 磁盘 + 多一轮 `read_file` 往返 + 依赖模型知道去读（故提示文案重要）；Codex 选了相反取舍（直接截断、有损、零额外机制）→ 属"无损但需配合 vs 有损但简单"的路线分歧，非唯一答案

**Hermes 关键工程细节**：
- 落盘在**沙箱内** `/tmp/hermes-results/{tool_use_id}.txt`（经 `env.execute()`，多后端 local/Docker/SSH/Modal/Daytona 通用）——因为 `read_file` 在同一个沙箱文件系统里读
- **stdin 推送而非命令行 heredoc**：Linux `MAX_ARG_STRLEN` 限制单个 argv 元素 128KB，早期把内容嵌进命令字符串 → **>128KB 的结果静默失败（`Errno 7: Argument list too long`）**——而恰是 >128KB 才需要落盘，bug 直接命中设计意图（`:100-116` 有完整注释）
- 写失败兜底：内联截断 + 明确告知 "Full output could not be saved to sandbox"（不静默）
- 预览在 `max_chars` 内最后一个换行处截断（且换行位置须 > max_chars/2）
- 窗口自适应：`_budget_for_agent()` 从 `agent.context_compressor.context_length` 实时取窗口 → `budget_for_context_window()`（单结果 15%、单轮 30%，clamp [8K,100K] / [16K,200K]，issue #23767）

**DB-GPT 移植差异**：

| 维度 | Hermes 原版 | DB-GPT 移植版 |
|---|---|---|
| 落盘位置 | 沙箱内（多后端） | 宿主机磁盘 `{output_dir}/persisted_results/{conv_id}/` |
| 写入方式 | `env.execute()` + stdin 推送（规避 128KB argv 上限） | Python 直接 `open().write()` |
| 阈值解析 | pinned → overrides → registry per-tool → default | pinned → overrides → default（**省掉 registry 层**） |
| 窗口自适应 | ✅ 每轮实时缩放 | ❌ **未接线**（函数在、无调用者；恒用固定 100K/200K） |
| ③ 单轮预算 | ✅ 三处调用 | ❌ **未接线**（`enforce_turn_budget` 无调用者） |
| 写失败兜底 | 库内兜底（内联截断 + 说明） | 移到调用方（`tool_action.py:221-227` try/except → 回退全文） |
| `read_file` 读回安全 | 无需白名单（同沙箱） | **新增目录白名单**（`persisted_results/`、`op_snapshots/` + realpath 防穿越）——无沙箱环境下的必要适配（正向） |

> **D.2 结论**：机制成熟（三层各堵一类失败模式），但本项目**只接上第二层**：小窗口模型阈值不收缩（原版 #23767 修掉的问题仍在）、单轮累加无防护。修复成本极低（接线 + 传参），见 P1-19。

### D.3 系统提示词与上下文组装（对照 P0-2 / P0-3 / P0-4 / P1-11）

> **固定源码版本**：DB-GPT `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`；Codex `7a0e974e08c798d1e8d59d407aeb6e24db1313af`。以下代码行号均按这两个 commit。缓存行为依据 OpenAI 官方 [Prompt caching 文档](https://developers.openai.com/api/docs/guides/prompt-caching)，访问于 2026-09-24；只用来说明 OpenAI 的 exact-prefix 规则，不外推到其他 provider。

> **术语沿用 §2**：本文的“记忆”仍指按 `conv_id` 重建工作的 ReAct 轨迹片段，不指跨会话偏好/事实；本节对照 Codex 消息历史与状态快照，不据此声称本项目已实现长期记忆或外挂记忆。

> **人话版小结（先读这段）**：
> **Codex 的"系统提示词"不是一段文字，而是四条独立通道**：
> ① `base_instructions`——唯一像 system prompt 的东西（工具无关的基础指令；**权威来源 `models-manager/prompt.md`**——`model_info.rs:17` `include_str!` 为 `BASE_INSTRUCTIONS`；`protocol/src/prompts/base_instructions/default.md` 是同内容的副本；**275 行**，开头 "You are a coding agent running in the Codex CLI…"，内容是工作规则而非具体任务）；放 API `instructions` 字段（`session/turn.rs:1289-1305` 的 `build_prompt` 三字段分离）。**选择顺序**（`session/mod.rs:636-659`）：config 覆盖 → 会话历史保存的 → 当前模型的指令模板（`model_info.get_model_instructions(personality)`，**可按人格渲染**；**无模板的模型返回空指令 + warn**，`openai_models.rs:487-505`）→ 即"**不存在对所有模型/配置都相同的系统提示词**"；且同一会话建立后固定于 `session_configuration`（`session/mod.rs:1261-1264`）→ **会话内稳定**，仅跨会话/跨模型/跨配置变化；
> ② `tools`——工具 schema，**由运行时工具路由生成**（`router.model_visible_specs()`），放 API `tools` 字段；
> ③ **类型化上下文片段**（`core/src/context/` 约 30 种）——AGENTS.md 指令（注意：**以 `user` 角色注入**，`user_instructions.rs:19` 标记 `# AGENTS.md instructions`）、权限/环境/应用/插件说明、**一次性提醒**（时间提醒、图片缩放、遗留警告、网络规则保存…）、多 agent 消息、预算上下文 → 均作为带角色的 input items；
> ④ **世界状态差分**（`core/src/context/world_state/` **16 个 section**：`agents_md` / `permissions` / `compact_permissions` / `environment` / `tools` / `model` / `personality` / `collaboration_mode` / `multi_agent_mode` / `multi_agent_usage_hint` / `apps_instructions` / `environments_instructions` / `plugins_instructions` / `context_window_guidance` / `realtime`）——每节有稳定 ID + 快照，只把"相对上次的变化"渲染成新片段（`world_state/mod.rs:200-243` trait：`snapshot()` + `render_diff()`）；**注意 `agents_md`（AGENTS.md）本身也是一个 section**——它的变化走**差分追加**，而非每轮全量重发（对照本项目：`{file_context}`/`{knowledge_context}` 每轮**内联**进 system prompt，正是"追加 vs 重写"的具体例证）。
> 补充：**动态上下文的完整四类** = ① 对话本身（user/assistant 消息 + function_call/output）② 类型化片段（~30 种，含一次性提醒/警告）③ 世界状态差分（16 section）④ **模型目录自带模板**（`ModelMessages`：approvals / collaboration_modes / auto_review / permissions / token_budget，`openai_models.rs:512-522`，按模型不同）。
>
> **每轮组装**：`input`（累积 items）+ `tools` + `base_instructions` 三样分开装进请求（`client.rs:862-891`：常规 Responses 用 `instructions`/`tools` 两字段；responses-lite 则把二者变为 input **前置**的 developer 消息）。**关键**：Codex 从不改 system prompt——**新信息一律追加为新的 item**，故"开头那段"永远逐字节相同（前缀稳定、缓存友好）。
>
> **与本项目的差距（三条）**：
> ① **分层**：Codex 四通道 vs 本项目**一个巨型字符串**（12 段 + 动态块内联 `:2790-2792` + connector 描述追加 `:2863-2938`）→ 每轮展开为 SYSTEM 消息（`base_agent.py:1285-1328`）；
> ② **动态内容位置**：Codex 追加尾部（前缀稳定）vs 本项目内联在 system 中部（其后的文字不再是稳定前缀）→ P1-11；
> ③ **来源与更新**：Codex 工具 schema 来自运行时、状态做**增量差分**、有**一次性提醒**机制 vs 本项目工具清单**手写**（已漂移 P0-2）、动态数据**每轮全量重建**、无"临时提醒"机制。
> **一句话**：差距不在"提示词写得好不好"，而在架构——Codex 是"**追加**"（消息流 + 类型化片段），本项目是"**重写**"（模板渲染）；这决定了缓存、漂移、增量能力三件事。

#### 1. Codex 的模板按用途拆分；“存在”不等于“运行时生效”

`codex-rs/prompts/src/` 用 `include_str!` 编译嵌入模板，模块再导出常量或渲染函数（`prompts/src/lib.rs:1-25`）。几个本轮指定模板的实际状态如下：

| 模板 | 接线与生效证据 | 判定 |
|---|---|---|
| `apply_patch_tool_instructions` | `prompts/src/apply_patch.rs:1-3` 定义并嵌入，`prompts/src/lib.rs:9` 导出；在该 commit 的 tracked tree 中，引用仅见声明/导出和 `core/tests/suite/prompt_caching.rs:8,215` 测试，没有生产调用点 | **已实现、已导出；生产路径未接线，实际生效未证实** |
| `compact` | `prompts/src/compact.rs:1-2` 嵌入；`core/src/compact.rs:113-124` 将 prompt 放入 compact 输入，集成测试 `core/tests/suite/compact.rs:417-420` 检查请求包含该 prompt | **已实现、已接线；请求层有测试验证** |
| `goals` | `prompts/src/goals.rs:30-98` 渲染目标/预算变量；`ext/goal/src/steering.rs:37-53` 将结果转成模型可见的 `InternalModelContextFragment` | **已实现并接线到 goal steering 路径** |
| `permissions` | `prompts/src/permissions_instructions.rs:87-120` 从有效权限配置构造文本；`core/src/context/world_state/permissions.rs:34-64,68-101` 将其纳入权限状态片段 | **已实现并纳入上下文组装** |
| `review` | `prompts/src/review_request.rs:8-10` 定义 rubric；`core/src/tasks/review.rs:114-122` 设为子 agent 的 `base_instructions`；`core/tests/suite/review.rs:1018-1020` 验证请求 `instructions` 等于该 rubric | **已实现、已接线；集成测试验证发送** |

因此，Codex 并非把这些 prompt 模板全部拼成一段常驻 system prompt；它们分属 tool、compact、goal steering、permission state、review 等具体功能路径。尤其 apply_patch 常量是本轮检索到的“实现了但无生产调用”案例，不能仅凭模板存在就声称运行时使用。

#### 2. 类型化上下文、模型可见差分与 rollout 基线是三件相关但不同的事

`core/src/context/mod.rs:1-37` 按模块组织多类上下文片段；`core/src/context/world_state/mod.rs:200-243` 定义 `WorldStateSection`，每类状态提供稳定 `ID`、可序列化 `Snapshot` 和 `render_diff`。WorldState 根据同一节的旧快照生成模型可见片段（`:347-392`）。首次建立上下文时，`core/src/session/mod.rs:3564-3606,3721-3740` 把完整片段整理成 developer/user 等 ResponseItem；后续变化由 `history.update_world_state` 计算片段差分，再作为新的 conversation items 记录（`:3741-3751,3764-3771`）。这部分是**实际接入模型输入历史**的状态更新路径。

另一个机制 `WorldStateItem::full/patch` 是 rollout 中的对比状态记录：`protocol/src/protocol.rs:3228-3244` 定义标记，`context_manager/history.rs:92-108` 首次写 full、变化时写 merge patch；session 代码在模型可见更新后持久化（`session/mod.rs:3022-3053,3764-3771`）。恢复测试把 full 与 patch 重放后还原最新 baseline（`session/rollout_reconstruction_tests.rs:1121-1157`），merge-patch 单测覆盖字段变化、删除和无变化（`context/world_state/world_state_tests.rs:242-275`）。**`WorldStateItem` 自己不作为 Responses API prompt 消息发送**；模型收到的是另外构造的 ResponseItem 片段。不能把 rollout 的 `full/patch` 误读成 token 级 prompt patch，也不能单凭它推断缓存收益。

#### 3. BaseInstructions 的位置与动态上下文

Codex 在 session 建立时按“配置覆盖 → 会话历史 → 当前模型 instructions”选取 `base_instructions`（`core/src/session/mod.rs:636-659`）；每次采样把 `input`、工具 schema、`BaseInstructions` 分开装入 `Prompt`（`core/src/session/turn.rs:1288-1305,1362-1367`）。常规 Responses 请求将其放在 API `instructions` 字段、工具定义走 `tools`（`core/src/client.rs:862-891`）；Responses-lite 则把 tools 和 base instructions 分别以前置 developer items 注入 `input`（同文件 `:862-885`）。同时，动态 WorldState 片段作为有角色的 ResponseItem 追加/更新在会话历史，而不是每轮拼回 `BaseInstructions`。

DB-GPT 主链路则在 `agentic_data_api.py:2652-2812` 构造通用 `workflow_prompt`，把 `{file_context}`、`{knowledge_context}`、`{database_context}` 内联在同一个字符串（`:2790-2792`），再追加 connector 描述（`:2863-2938`），转成 `PromptTemplate` 并 bind 到 `ToolCallingReActAgent`（`:2943-2960`）。`base_agent.py:1285-1328,1388-1411` 展开该模板并把完整结果放进 `SYSTEM` 消息。也就是说，模板化本身已接线，但片段边界和运行时数据没有像 Codex WorldState 那样成为独立、带状态的上下文贡献。

#### 4. 技能/记忆提示词的位置；续聊与持久化的对照

**技能简介/使用说明 + 可用技能目录**

- Codex 的“如何发现、何时使用、怎样读取技能”是源码中的固定说明文本；技能名、描述、路径/locator 则运行时从目录发现并渲染，不是固定清单（`ext/skills/src/catalog_prompt.rs:1-19,38-54`；`ext/skills/src/extension.rs:190-238`）。模型配置允许包含使用说明时，目录和说明组成 `AvailableSkillsInstructions`，内部归类为 `DeveloperCapabilities`；组装时它进入 `developer` 角色的上下文消息，**不是 `base_instructions` 本文，也不是工具调用结果**（`ext/skills/src/fragments.rs:16-53`；`core/src/session/mod.rs:960-976,3480-3493,3588-3607`）。
- Codex 选中技能后才读取技能正文；正文通过 `<skill>...</skill>` 作为一个 `user` 角色上下文 item 加入该轮输入，而不是作为工具结果回填（`core/src/session/turn.rs:793-813,851-863`；`ext/skills/src/fragments.rs:56-101`）。
- DB-GPT 完整模式有“可用技能清单 + Skill Execution Norms”；预选技能时切换到简化版 prompt，并把所选技能全文插进该 `SYSTEM` prompt（附录 C；`agentic_data_api.py:2372-2393,2466-2490,2701-2735,2943-2960`）。因此两项目差别不是“有无技能说明”，而是 Codex 把可用目录做成 developer capability 上下文、选中正文另作一条 user item；DB-GPT 则在对应模式把清单/规范或预选正文放进 system 字符串。

**记忆使用说明 + 记忆摘要**

- Codex 在记忆功能开启且 `memory_summary.md` 存在且非空时，把固定读取策略模板与该摘要合成一段 `DeveloperPolicy`，再进入 `developer` 角色上下文；摘要从 `codex_home/memories/memory_summary.md` 读取（默认通常在 `~/.codex/memories/`）并限长（`ext/memories/src/extension.rs:40-69`；`ext/memories/src/prompts.rs:23-51`；`ext/memories/templates/memories/read_path.md:1-41`；片段组装 `core/src/session/mod.rs:960-976,3480-3493,3588-3593`）。Memory tools 是否出现另受 `dedicated_tools` 配置控制（`extension.rs:98-115`）。所以这是**有条件注入的 developer policy + 动态摘要**，不属于 Codex 的基础 `base_instructions`。
- DB-GPT ReAct prompt 确实有 `task_progress` 进度摘要槽位，渲染在 ReAct system prompt 中（`agent/expand/react_agent.py:69-73`；`core/base_agent.py:1356-1360,1390-1411`）。但它由 Agent 对象的 `_task_progress` 列表生成；该列表是非序列化实例态，代码只支持同一 Agent 执行/retry 中延续，不能据此认定 agent 重建或新请求后仍有这份摘要（`core/role.py:59-62,212-240,351-356`）。续聊恢复的 ReAct 轨迹是另一条链路（见下）；因此它不是 Codex 那种“读取长期记忆的策略说明 + 跨运行 memory summary”。**核对的 DB-GPT 主链路默认路径未见对等的跨会话语义记忆注入**；不要把 `task_progress` 或 `conv_id` 轨迹称为长期记忆。

**恢复与持久化：记录分层不同，恢复语义也不同**

| | DB-GPT 主链路 | Codex |
|---|---|---|
| 持久化什么 | 两套主要记录：`StorageConversation` 保存 user/assistant 对话；`GptsMemory` 经 `MetaDbGptsMessageMemory` 保存 ReAct agent/action 消息和 `ActionOutput`。部分大操作细节另写 `op_snapshots` 文件（`agentic_data_api.py:2310-2357`；`gpts_memory.py:122-139`；`dbgpt_serve/agent/agents/db_gpts_memory.py:78-93`；`core/role.py:408-465`）。 | Rollout 按序记录有类型的 `RolloutItem`：模型可见 `ResponseItem`、事件、`Compacted` 替代历史、`TurnContext`、WorldState 状态等（`protocol/src/protocol.rs:3211-3263`）；模型可见消息在进入历史时同步写入 rollout（`core/src/session/mod.rs:2993-3019`）。 |
| 续聊如何恢复 | `StorageConversation.get_history_message()` 重载历史；`view` JSON 只提取 `final_content` 做 assistant 历史（步骤/计划等 UI payload 不作为同一条模型 transcript 原样恢复）。另从 `GptsMemory` 取 action outputs，经 `recovering_memory` 写回短期 buffer，再转成 `Question/Thought/Action/Observation` 等新 `AgentMessage`；随后与对话历史一起送入模型（`agentic_data_api.py:2334-2357`；`core/base_agent.py:245-259,1413-1428`；`gpts_memory.py:152-179`；`core/role.py:472-484`；`expand/react_agent.py:274-365`）。 | 从 rollout 重建有效的模型历史；遇到 compaction 使用 `replacement_history` 作为新基线，再重放后续 ResponseItems，并恢复相应 turn settings、引用上下文和 WorldState baseline（`core/src/session/rollout_reconstruction.rs:113-184,317-400`；安装恢复状态 `core/src/session/mod.rs:1428-1470`）。WorldState `full/patch` 明确是重建差分基线的持久记录，不是模型 prompt patch（`protocol/src/protocol.rs:3228-3243`）。 |
| 主要边界 | 记录分散在对话表、agent trace、快照文件；恢复时转换成模型消息，非原始请求的逐字重放。历史 role 还有按奇偶位置推断的路径（P1-10）。上下文压缩返回的摘要是本次构造出的 prompt 消息；`manage_context` 不把该摘要写成下一轮的权威恢复 checkpoint（`core/context/manager.py:93-155`；`core/context/compact.py:285-298`）。 | 对**模型可见历史与已记录状态**有明确 replay 语义；但这不等于重放外部工具副作用、文件系统或整个进程的任意运行态。 |

结论分级：🔴 **已验证差距**——Codex 保存并重建的是带类型的模型输入历史及压缩/状态基线；DB-GPT 将用户对话、ReAct 执行轨迹和 UI 历史 payload 分开保存，再转换/筛选后重组 prompt，所以不是同一种“恢复”。这支持既有 P1-9/P1-10，不表示 DB-GPT 完全没有 trace 持久化。⚖️ **权衡取舍**——DB-GPT 将 UI 对话、执行审计和模型上下文拆开有产品价值；不需要为了恢复语义而迁移 Responses API。💭 **主观建议**——若要提升精确续聊，先确立可 append/replay 的规范化模型消息记录；trace、UI payload、跨会话语义记忆分别存放、分别命名。

**设计边界决议（2026-09-27）：DB-GPT 的会话持久化以 Codex rollout 为对标对象**

- **目标**：设计一份数据库承载的、按序追加且可重建会话上下文的权威记录；这是“Codex rollout 的数据库版”这一架构目标，不是把 Codex 的 JSONL 文件格式照搬进 DB，也不是泛化成“所有运行时事件都进 `agent_event`”。`agent_event` 目前只是候选单表名称，表结构与事件枚举尚未定案。
- **记录边界**：记录恢复模型对话/会话状态所必需的规范化条目，包括用户输入、助手实际返回的消息与结构化工具调用、工具结果，以及必要的轮次终态和上下文/压缩状态。Codex 同样按策略筛选：`ResponseItem` 中的 `FunctionCall` / `FunctionCallOutput` 会持久化，而 `RawResponseItem`、`ItemStarted`、流式 delta 等瞬时事件不持久化；`TurnStarted` / `TurnComplete` / `TurnAborted` 等关键轮次事件会持久化（Codex checkout `7a0e974e08c798d1e8d59d407aeb6e24db1313af`：`rollout/src/policy.rs:8-19,37-58,85-182`；`protocol/src/protocol.rs:3213-3235`）。因此，“对标 rollout”指**记录语义与恢复目标**，不等于全量复制其每个 runtime callback。
- **概念隔离**：聊天 UI 历史、实时进度通知和 OpenTelemetry/Agent trace 是可由权威记录派生或独立维护的投影/观测数据，不应与可恢复的会话记录混称。完整 provider HTTP/SSE 包、逐 token delta、每个 UI begin/end 通知，除非另有明确审计需求，不默认作为恢复记录。
- **尚未决策**：`tool_call_started` 是否额外持久化，取决于是否要显式识别“外部副作用可能已执行、结果尚未落库”的不确定窗口；它是可靠性增强，不应声称是 Codex rollout 必需项。`action_output_created` 是 DB-GPT 的聚合层产物，须先确定它是否为模型实际消费且无法从已存工具结果确定性重建，再决定独立存储还是作为结果条目的载荷；不得为了事件数量齐全而重复记录。
- **阶段边界**：本决议只确定“有序、可恢复的会话记录”这一目标与记录边界；**不包含运行中 Agent 检查点、从工具副作用处自动续执行、自动重放外部工具，也不表示现在开始实现**。后续若进入方案/实现阶段，应先据此审视 `agent_event` 的字段、事务写入窗口与恢复语义。DB-GPT 当前 native `tool_calls` 暂存在 Agent 实例上，完成后的 `ActionOutput` 写入 `GptsMessages`（DB-GPT checkout `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`：`tool_calling_agent.py:472-490`、`base_agent.py:993-1045`）；这说明当前记录路径与目标不同，但本决议不预先指定表结构或重放策略。

#### 5. 对 §2 骨架的证据归档

- 🔴 **已验证差距——双源与双轨**：工具清单在 prompt 中手写并已出现重复编号（P0-2）；运行时 ToolPack 是另一份来源。native 路径确实会由 ToolPack 建 schema 并传 `tools`，文本 prompt 同时要求只用 Thought/Action 文本协议（P0-3）。这是“配置已启用、调用路径已接线”；实际 provider/model 返回 native call 的覆盖率待核实。knowledge 模式只替换 ToolPack，仍共用通用 prompt（P0-4）；注释称“only kb tools”与实际还加入 business/connector tools 有差异，故问题不是“所有非 KB 工具都不存在”，而是 prompt 没有和模式工具集同源。
- 🔴 **已验证差距——组装与运行时数据耦合**：DB-GPT 主链路的动态附件/知识库/数据库数据进入 system prompt；Codex 则将 BaseInstructions、tools、按角色组织的上下文 items 分开构造。此为结构差异（P1-11），不等价于缓存命中率已经下降。
- ⚖️ **权衡取舍——协议选择**：Codex 的 Responses `instructions` 与 `input` 结构提供清晰边界，但不证明 DB-GPT 必须迁移 Responses API；Chat Completions 与多 provider 兼容仍是现有合理取舍（§4）。可移植的改进点是稳定前缀和独立上下文片段。
- 💭 **主观判断 / 待验证——类型化与增量上下文是否值得移植**：Codex 的 WorldState 抽象能独立比较设置并重放 rollout；DB-GPT 可否从中受益，取决于同一对话中上下文变化频率、重复 token 成本与实现维护成本，当前没有测量。不得将 `full/patch` 直接照搬为模型协议。
- **缓存边界（含"缓存断点"概念的澄清）**：OpenAI 官方文档说明缓存复用要求 prompt 前缀相同（**自动前缀缓存、无显式断点**；≥1024 token、逐字节匹配），并建议监测 cache read；Anthropic 则为**显式 `cache_control` 断点**（≤4 个）——"cache breakpoint A/B"属后者风格，Codex 靠"**从不改前缀、新信息只追加**"获益而非打断点。另注：**"稳定块"是近似**——Codex 的 `tools` 每轮由工具路由器重新生成（`router.model_visible_specs()`），且**命名空间可延迟加载**（`tool_search`），"有哪些 deferred namespaces"本身是一个 world state section（`world_state/tools.rs`，ID=`"tools"`，差分渲染上限 4KB）；`permissions`、`agents_md` 等"看似 SYSTEM 的内容"实为 **world state section（走差分）**。本项目把变化块放在长 system prompt 中部，代码可证动态块之后不再是稳定公共前缀，但实际 cache hit、成本与延迟数据**待核实**。该结论不适用于未核对过的其他 provider。
  - 三条易混点的澄清：① **"重新生成" ≠ "内容变化"**——Codex 每轮重算 `tools` 列表，但工具集不变时序列化结果逐字节相同（依赖顺序确定性），缓存照常命中；仅当工具集真的改变（切模式/加载延迟命名空间）才从该点断开 ② **"追加在发生位置" = 追加到当前 items 末尾**（append-only），其之前的内容逐字节不变，前缀保持；插入到中间才会破坏前缀，而 Codex 明确避免（状态变化是"追加新的差分片段"而非回改旧片段）；**唯一例外是 compact**（重写历史，显式动作）③ **不是"分字段缓存"**——缓存作用于服务端把字段渲染后的**整体 token 序列**的前缀，客户端不能为单个字段单独缓存；且字段渲染顺序由服务端决定（图 "TOOLS→SYSTEM→MESSAGES" 更接近 Anthropic 排布；此为既有知识、本次未直连核实）。命中 = **最长匹配前缀** → 变化时"从变化点往后失效"而非全失效
  - 厂商参数（断点上限是否 ≤4、TTL、最小可缓存长度）**本次未能直连官方文档核实**（Anthropic 区域限制 / OpenAI 403）→ 标注为待核实，以官方文档为准
  - **缓存跨轮演进与破坏点**：T1 全 miss → T2 命中 `[instructions][tools][首轮 items]` → T3 命中更长前缀（只要 instructions/tools 不变、旧 items 不被改写，命中前缀随对话增长）。**四个破坏点**：① `instructions` 变（换模型/人格/配置）→ 从最前断开；② `tools` **真的变**（切模式/加载延迟工具）→ 从 tools 段断开；③ 历史被改写（compact、协议修复）→ 从改写点断开；④ 插入到中间（Codex 明确避免）。**注③的深意（连接 D.1）**：Codex 的协议修复（`ensure_call_outputs_present` 在中间插 `"aborted"` 输出）之所以不破坏缓存，是因为修复**确定性**——同样的历史渲染出同样的字节，连合成 item 的 ID 都用 **UUIDv5 + 固定 namespace** 推导（D.1 那条"改了会破坏 prompt 缓存"的注释正为此服务）
  - **正确心智模型（三桶而非线性文本）**：请求 = `instructions`（会话内固定）+ `tools`（每轮生成、通常字节相同）+ `input`（append-only，动态片段与当前用户请求都在**末尾**追加）；三桶的**渲染顺序由服务端决定**（客户端控制不了）→ 不要把 "TOOLS 在前/SYSTEM 其次" 画死；图上的 "cache breakpoint" 应读作"**自然前缀边界（自动发现）**"，显式断点仅 Anthropic 有

> **D.3 结论**：Codex 的对照价值在于模板按具体路径接线、类型化上下文状态、模型可见消息与持久化差分分层；并非“所有模板都有效”或“full/patch 是 prompt patch”。本轮补证强化 P0-2/P0-3/P0-4/P1-11，并澄清 P1-9/P1-10/P1-22 中 trace、续聊历史与长期语义记忆的边界；这些证据归入既有条目，不新增 P0-10/P1-22/P2-21。

### D.4 长期语义记忆实现评估（对照 P1-22，本机源码）

**问题**：`memory/` 下的 `LongTermMemory` / `HybridMemory` 是不是"真正的 agent 长期记忆"？实现质量如何？为什么未接主链路？

**（1）架构是真的（Generative Agents / YuLan-Rec 移植），但默认大面积空转**

- 架构来源可考：`LongTermRetriever` 继承 `TimeWeightedEmbeddingRetriever`（memory stream + 三因子 `score = (1-decay_rate)^hours + Σmetadata[other_score_keys] + vector_relevance`；`decay_rate=0.01`、`_k=4` 在检索器构造里**硬编码**，`time_weighted.py:50-83,280-311`）；`EnhancedShortTermMemory` 注释明示参考 YuLan-Rec（`short_term.py:66-69`），含"相似度增强→合并→提炼洞察"巩固机制（`enhance_similarity_threshold=0.7`、`enhance_threshold=3`、`random.random() < sigmoid_prob` 随机门）。→ 不是玩具桩，是学术记忆架构的忠实移植。
- **但默认全部空转**：`role.py:246-262` 的 `memory_importance_scorer` / `memory_insight_extractor` 都直接 `return None`，且**全仓库无任何覆盖**（grep 定义仅此两处）→ ① 洞察永不生成（`base.py:564-565` 无 extractor 即返回 `[]`）② 重要性退化为常量 `5 * importance_weight`（`base.py:596-597`）。默认配置下"重要性/反思"维度不存在，实际只剩"向量相似 + 时间衰减"。
- **写入路径可崩（代码路径分析，未做运行时验证）**：`LongTermMemory.write`（`long_term.py:235-240`）要求 importance；为 None 且 `_default_importance=None` 即 `raise ValueError("importance is required.")`。而巩固转移出来的片段 `reduce(..., importance=memory.importance)` 携带的正是 None（`agent_memory.py:254-280`；源片段由 `role.py` 构造时从不设置 importance）→ **一旦 `enhance_cnt≥3` 触发转移，写长期层即抛错**（恢复路径 `write_batch` 因 `write_long_term=False` 不受影响）。
- 其它未完成项：`LongTermMemory.clear()` 是 TODO `return []`（**无删除 → 合规缺口**）；`write_batch` 两条 TODO 明写 reflect / forget 未实现；`EnhancedShortTermMemory.write` 忽略 `handle_overflow` 返回值 → 最不重要的溢出片段被静默丢弃（与"转移长期"意图不符）；vector-store-only 模式（默认，无 `external_storage`）下 `last_accessed_at` 只改内存态 `memory_stream`、不回写向量库 → 检索器的"频繁访问不被遗忘"不成立（`time_weighted.py:218-234`）。
- 隔离/召回：`session_id` 过滤存在（`long_term.py:302-305`），legacy 侧取 `f"{conv_id}_{role}_{name}"`（`base_agent.py:245-247`）→ 按会话隔离成立；但**无用户维度、无删除/导出接口**。

**（2）内容形态：情景记忆（episodic trace），不是业界"用户记忆"**

写入内容是 `"[{idx}] " + raw_observation`（`long_term.py:251-254`）——ReAct 轨迹文本（含工具输出），不是被蒸馏的用户偏好/事实；检索回来的也是历史轨迹片段。对照 ChatGPT Memory / Anthropic Memory Tool / mem0 的"事实/偏好 + 可编辑可删除"，**形态不同**：它是"经验回放"，不是"用户画像"。

**（3）为什么未接主链路（证据分级）**

- **已验证（设计选择）**：主链路把"记忆职责"交给三件套——DB 消息持久化（`MetaDbGptsMessageMemory`）+ `build()` 时 `recovering_memory()` 从 DB 重建片段（`base_agent.py:245-259`）+ 会话历史重载（`historical_dialogues`，注释明写 "mirrors hermes' conversation_history"，`agentic_data_api.py:2337-2340`）。→ 不是"忘了接"，是**主动换实现**（无状态服务 + 历史重放）。
- **已验证（能力缺口）**：即便接入也会遇到 ① importance/insight 默认 None → 空转；② 转移触发即抛错；③ 无删除（合规）；④ `_k=4`、decay 0.01（半衰期≈69h）与"长期"定位不符；⑤ 每步一次 embedding，开启打分则额外多次 LLM 调用。
- **推论（待验证）**：主链路已有更结构化的历史物料（history_payload v2 + snapshot + `_task_progress`），质量优于轨迹文本 → 复用 v1 memory 的**边际收益低**；若要做长期记忆，更合理路径是给结构化历史做语义索引，而非接入这套实现。

> **D.4 结论**：这是一套"**学术级架构、原型级完成度、默认空转**"的长期记忆；主链路未接入既是设计选择（DB 历史重放），也受实现缺陷（importance 缺失、洞察空转、无删除）与合规约束限制。P1-22 的定性仍为 ⚖️，但"接线成本低"须补一句：**接线成本低，可用成本不低**（先修空转与删除，再做治理）。

**（4）长期记忆评测顺序决策（2026-09-27）**

- 🔴 **当前基线**：主链路缺少用户级跨会话语义记忆；这是 P1-22 / D.4 已由源码确认的能力缺口。因此，不把“先跑一次最小跨会话测试、确认当前功能不存在”作为前置工作，也不将当前路径的失败分数解释为长期记忆质量。
- 💭 **实施顺序**：若产品决定建设该能力，先明确用户/租户记忆身份与写入、更新、删除、权限策略；跨会话验收测试与实现同步加入。验收至少覆盖新 `conv_id` 同用户召回、不同用户隔离、事实更新/冲突处理及删除生效。验收通过后，再运行完整基准。
- **外部基准分工**：LongMemEval（https://github.com/xiaowu0162/LongMemEval）测多会话记忆问答、时间推理、知识更新和弃答；LongMemEval-V2（https://github.com/xiaowu0162/LongMemEval-V2）提供 Agent 轨迹记忆评测器及自定义后端接口；Mem2ActBench（https://github.com/Cantaloupe-M/Mem2ActBench）可参考其记忆驱动工具调用任务，并需改造成 DB-GPT 的 SQL/数据分析任务。基准应通过 DB-GPT 实际记忆写入/读取链路运行，不得把 gold facts 直接灌入记忆库。
- **对照与报告**：至少比较“无长期记忆 / DB-GPT 长期记忆 / oracle 证据”三组；分别报告答案正确率、证据召回、更新/冲突/弃答、SQL 工具参数与执行结果、用户隔离、延迟和成本。完整基准启动前，先固定模型、提示词、预算、数据和评分器版本。

### D.5 上下文压缩机制评估（对照 P1-19 / P2-17，本机源码）

**问题**：上下文超长了怎么办？有没有上下文压缩？

**结论：有，且是本项目少数"实现 + 接线 + 生效"齐备的机制**——四层渐进压缩（`agent/core/context/`：budget / compact / manager / storage，带测试）。

- **预算来源（自适应）**：`_load_context_budget_config`（`agentic_data_api.py:88-91`）**优先读模型元数据 `context_length`**，否则默认 120000；预留输出 4096；阈值 WARNING 0.70 / ERROR 0.90 / CRITICAL 0.95；计数用真实 tokenizer，失败回退 4 chars/token（`budget.py:106-116`）
- **每步 LLM 调用前检查**（`base_agent.py:1433` → `manager.manage_context`）按状态渐进：
  - **Layer 1** `ObservationMicroCompact`（WARNING）：截断超过 5 轮的旧 Observation 至 500 字符，**跳过已落盘的 `<persisted-output>` 预览块**（保住文件路径引用），截断后附 `[truncated, full detail at: snapshot_path]`（`compact.py:87-137`）
  - **Layer 2** `SessionMemoryCompact`（WARNING）：整轮丢弃旧 ReAct 三元组（保最近 3 轮 / ≥10000 token），**无需 LLM**——system prompt 里的 `task_progress` 充当隐式摘要（`compact.py:145-206`）
  - **Layer 3** `FullContextCompression`（ERROR）：**LLM 结构化摘要**替换旧消息（Task/Steps/State/Key Data 必须精确值/Errors/Next Steps），失败有断路器（3 次，`compact.py:214-298`）
  - **Layer 4** `ReactiveCompact`（响应式）：捕获 LLM `context_too_long`/`context_length_exceeded`/`maximum context length`（`base_agent.py:608-630`）→ 只留 system + 最近 2 轮 → 立即重试
- **落盘先行**（即 P1-19 三层防御）：`ToolResultStorage` 在 `init_context_management` 初始化（`base_agent.py:136-141`），单结果 >100k 字符落盘 `persisted_results/{conv_id}/` + 1500 字符预览
- **前端可视**：`context.status` SSE 事件（used/budget/ratio/state/layer）→ `_context_status_callback`
- **仍未接线（"未接线"清单第 6/7 例复核，2026-09-26）**：`enforce_turn_budget`（单轮聚合 200k）与 `tool_result_budget_for_context_window`（按模型窗口自适应每工具阈值）仍是**零调用**（已导出、有文档字符串；`storage.py:42,151-168`）→ 落盘阈值实为固定 100k，不随窗口缩放
- **注意点**：Layer 1 **原地 mutate** `msg.content`（非拷贝）——对当轮记忆片段"改了就保持"，与 append-only 原则相悖但影响可控（跨请求由 DB 重建恢复）；Layer 4 只重试一次、无二次压缩循环

> **D.5 结论**：压缩设计与 Codex（auto-compact）思路同构：便宜层先行（截断/丢轮）、贵的 LLM 摘要殿后、响应式兜底；Layer 2 用 `task_progress` 顶替 LLM 摘要是聪明的省成本设计。缺口是两个预算函数未接线（自适应名存实亡）与 Layer 1 的原地改写。证据归入 P1-19/P2-17，不新增条目。
>
> **⚠️ 2026-10-13 复核修正（详见 D.15）**：本节对 L1/L2/L3 的行为描述只对**旧文本 ReAct 结构**成立。主链路已切到 rollout 事件投影（原生 `role=tool` 消息、无 `Observation:` 文本），三层的门控（`_is_observation_message`/`_detect_round_boundaries`）全部不匹配 → **rollout 链路下 L1/L2/L3 均不触发（只剩 L4）**；且压缩产物不进入 provider 请求（请求由投影重建）、压缩结果未接入事件流。P1-25 记录该缺口。

### D.6 压缩与历史管理（第 3 轮，对照 D.5 / P2-17 / P1-9；Codex commit `7a0e974`）

**Codex 侧事实（一手源码）**：

1. **触发**：`auto_compact_token_limit = min(配置, 窗口×9/10)`（`protocol/src/openai_models.rs:468-479`）+ `fallback_buffer_tokens`（到限强制，`session/context_window.rs:66-78`）；**每个采样步之后检查**（`session/turn.rs:376-424`）→ `should_roll_over` → 中轮压缩（`MidTurn/ContextLimit`）→ `continue` 主循环（`turn.rs:434-463`，注释明言"压缩有效就不会死循环"）
2. **模型参与**：两个**模型可调用工具**——`get_context_remaining`（返回 tokens_left，`tools/handlers/get_context_remaining.rs`）与 `new_context_window`（模型主动请求换窗，`new_context_window.rs`）；框架侧还有 `take_new_context_window_request()` 承接模型请求
3. **压缩 = 专用 turn**：全历史 + `SUMMARIZATION_PROMPT`（"CONTEXT CHECKPOINT COMPACTION，为另一个 LLM 写交接摘要：进度/决策/约束偏好/下一步/关键数据"）→ 摘要以 `SUMMARY_PREFIX`（"Another language model started to solve this problem..."）为前缀编码为 user 消息（旧摘要会被 `is_summary_message` 过滤不重复保留）；**替换历史 = 最近用户消息原文（newest-first，≤20k tokens，超了截断）+ 摘要**（`compact.rs:55,342-378,616-690`）
4. **mid-turn vs pre-turn 注入策略**：mid-turn 把 world_state 初始上下文重注入到最后真实用户消息之前（`BeforeLastUserMessage`，因模型被训练为"摘要在历史末尾"）；pre-turn/manual `DoNotInject`（下一常规轮全量重注入）（`compact.rs:66-72,356-367,549-614`）
5. **压缩自身超窗的降级链**：压缩过程中 `ContextWindowExceeded` → `history.remove_first_item()` 重试（注释："Trim from the beginning **to preserve cache**"——从头裁剪保前缀缓存）；其他错误按 `stream_max_retries` 退避（`compact.rs:304-318,320-338`）
6. **压缩结果的地位**：`replace_compacted_history` 把替换历史写成**权威历史（持久化 checkpoint）**+ 重算 token 用量 + 给用户显式 Warning："Long threads and multiple compactions can cause the model to be less accurate. Start a new thread when possible."（`compact.rs:368-386`）
7. **可观测**：全链路 analytics（trigger/reason/phase/strategy=Memento、压缩前后 tokens、summary tokens、cache tokens）；pre/post compact hooks

**与 DB-GPT 主链路（D.5）对照**：

| 维度 | Codex | DB-GPT 主链路 |
|---|---|---|
| 档位 | 摘要一档 + 逐条裁剪重试兜底 | 四层（截断/丢轮/LLM 摘要/响应式）——**零成本层更细** |
| 触发 | 窗口 90%（配置只能调低）+ buffer；每采样步后 | 0.70/0.90/0.95（`context_length` 自适应）；每 thinking 前 |
| 摘要 | "交接给另一个 LLM"定位；保留用户消息原文 ≤20k | 结构化六段摘要；保留最近几轮原文 |
| 压缩超窗降级 | 逐条从头裁剪重试（保缓存前缀） | L3 失败仅断路器（3 次），**无逐条裁剪** |
| 压缩结果地位 | **替换权威历史并持久化 checkpoint** | 只作用当轮 `thinking_messages`，不落权威 checkpoint——下轮重新组装、重新压缩（D.3 表已指出；既重复计算，多次压缩还可能抖动） |
| 模型参与 | 查余量/换窗两个工具 | 无（纯框架驱动，仅 SSE `context.status` 可视） |
| 外部状态保全 | world_state 重注入 | **落盘 + read_file 读回**（hermes 式） |
| 多次压缩退化 | 显式 Warning 告知用户 | 未处理 |
| 未接线遗留 | — | `enforce_turn_budget` / `tool_result_budget_for_context_window` 零调用（D.5） |

**补充（Hermes 侧运行时压缩，本机源码 2026-09-26）**：Hermes 的哲学是"**第一道防线是不让大东西进上下文**"（工具内自截断 + 落盘预览 + read_file 读回，即 D.2），压缩只是最后兜底——但兜底做得极重（`agent/context_compressor.py` 6,883 行 + `conversation_compression.py` 4,014 行 + `context_engine.py`/`context_breakdown.py`，共 1.1 万行）：① 辅助廉价模型摘要**中段**，头（system/首轮）与**按 token 预算的尾段**保护；② **工具输出先剪枝再喂摘要器**（便宜预Pass，省摘要 token）；③ **迭代式摘要**（多次压缩间保留信息，v3 明确改进项）；④ 摘要预算按被压内容比例（`_SUMMARY_RATIO=0.20`）；⑤ **skill 剪枝标记的确定性重注入**（`_reinject_pruned_skill_markers`，防摘要器丢引用）；⑥ 触发阈值默认 **50%** 窗口（小窗口模型提到 75%，`_SMALL_CTX_THRESHOLD_PERCENT`；支持按模型覆盖）+ 请求压力信号 + overflow/413 响应式处理；⑦ **并发正确性工程**：`CompressionCommitFence` 提交栅栏、冷却租约、取消安全、尝试状态快照/恢复——因为压缩会边跑边改会话。注意：根目录 `trajectory_compressor.py` 是**离线训练数据后处理**（保护训练信号），不是运行时压缩。→ 三家对比：DB-GPT（四层渐进，零成本层最细）/ Codex（checkpoint 化 + 模型参与）/ Hermes（触发最早 + 迭代摘要 + 并发工程最重）。

**补充 2（预算缩放的出处，2026-09-26）**：`tool_result_budget_for_context_window`（`storage.py:151-180`）是 Hermes `budget_for_context_window`（`tools/budget_config.py:84-114`）的**逐字移植**（`_PER_RESULT_WINDOW_FRACTION=0.15`、`_PER_TURN_WINDOW_FRACTION=0.30`、8k/16k 下限、同款 docstring）。**Hermes 侧已接线**（`agent/tool_executor.py:89` 按当前模型窗口实时计算）→ Hermes 第二层有效阈值 = `min(窗口token×4×0.15, 100k)`（帽）且 ≥8k（底）；单轮 = 30%、帽 200k、底 16k。**DB-GPT 移植后零调用 → 恒为固定 100k**——对 200k+ 大模型两者恰好等价（120k 被 100k 帽住），差别只在 65k 这类小窗口模型。设计依据 Hermes 注释一手可考：① 100k/200k 是"为 200k+ 大模型定的"（docstring 原话 "correct for large (200K+ token) models"）；② 占比远小于 1.0 的原因——"窗口里还有 system prompt/工具 schema/历史/模型回复在竞争"（`budget_config.py:70-74`）；③ 缩放是 **bug #23767** 驱动：65k 小模型上固定 100k/200k 可独自逼近或超过整个窗口；④ 4 chars/token 方向保守（选反会 UNDER-protect 小模型，`budget_config.py:64-67`）。→ **未接线的实质危害：小窗口模型上 DB-GPT 会重现 #23767**。

**补充 4（摘要大小控制：Hermes 禁止 wire cap，DB-GPT 正在用，2026-09-26）**：Hermes 的摘要预算（被压内容×20%）**只进 prompt**（"Target ~{budget} tokens. Be CONCRETE — include file paths, command outputs..."，`context_compressor.py:3809`），**刻意不传 API 层 `max_tokens`**——注释与契约测试（"the no-wire-cap contract test"，:393-395）明令禁止，原因：Anthropic Messages 与 NVIDIA NIM 两个 wire 会转发该参数 → **摘要被切半**（thinking 模型还会把 cap 烧在思考上，:3869-3874）；截断的摘要丢尾部关键段是灾难，超长摘要只温和损失。真正的规模控制靠**输入侧**：每消息 6000 字符（头 4000/尾 1500）、工具参数 1500、聚合 160K 字符（≈40K token）有界化 + 头尾保留 + 省略中段标记（:396-396,3198-3206）。**对照：DB-GPT L3 摘要用了 `max_new_tokens=2000` 硬帽**（`compact.py:272-276`）——正是 Hermes 用事故换来的教训所禁止的模式；中段较大时摘要会被静默截断（截断不抛错、断路器也不感知）。建议：L3 去掉 wire cap、预算改 prompt 软目标 + 输入侧有界化。**（2026-10-13 更新："输入侧有界化"已定不做——摘要输入走 Codex 路线（移除既有 `content[:3000]`（`compact.py:255`）、全量喂入，超窗靠 trim-retry）；wire cap 纠错保留。见 D.16 Phase 2 ⑧。）**

**补充 3（压缩与重复调用，2026-09-26）**：压缩丢工具调用历史 → 模型重复调用。DB-GPT 已有第一版缓解——`task_progress` 的设计意图正是它（`base_agent.py:1354-1360` 注释："LLM always knows what has been done **regardless of how many memory fragments have been evicted**"）+ snapshot 可读回。但四个缺口：① progress 行**无参数**（`role.py:377-387` 只有 action 名/状态/token 数，无 action_input）→ 模型知道"查过"不知道"用什么查的"；② L2 是整轮删除、不留墓碑；③ L3 摘要不强制"已完成调用清单"；④ **执行层无去重护栏**（grep 证实无同参数重复检测）。分层解法（按成本）：A. progress 行加参数摘要+结果一句话（数据都在 memory_map，改模板即可）；B. L2 丢轮改"墓碑替换"（`[steps 5-12 compacted: sql_query(...), read_file(...); details: snapshots/*.json]`）；C. L3 摘要模板学 Hermes：Resolved/Pending 追踪 + "already addressed" 显式标注 + Historical 标题防摘要被当活跃指令（`context_compressor.py:105,280`）、todo 清单压缩中保护并原样重注入（`TODO_INJECTION_HEADER`，:4275）；D. 执行层去重护栏：记录 (tool, args_hash)，只读工具命中→返回"已在 step X 执行+摘要+snapshot 路径"，写/副作用工具→转确认提示（衔接 P1-21 审批分级）；E. 压缩 checkpoint 化（Codex `replace_compacted_history`，丢失行为可审计）；F. 模型侧余量可见（Codex `get_context_remaining`）。A+B 立即可做，D 是护栏值得做。

> **D.6 结论（第 3 轮完成）**：两侧同构——"渐进压缩 + LLM 摘要殿后 + 超长兜底"。三个可借鉴点：① **压缩结果持久化为权威 checkpoint**（DB-GPT 现状是每轮重算压缩，浪费且可能不稳定）；② 压缩自身超窗时的**逐条裁剪重试**（保缓存前缀）；③ 模型侧 `get_context_remaining` 工具（让模型自主管理余量）。DB-GPT 的相对优势：L1/L2 零成本层、落盘读回、SSE 可视。证据归入 P1-9/P1-19/P2-17，不新增条目。

### D.7 会话记录实现落点审计（第 4 轮，2026-09-28）

> **范围与版本**：只读核对 DB-GPT 主 ReAct HTTP 链路，不改代码、不迁移数据库、不验证生产部署。DB-GPT commit `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`；Codex checkout `7a0e974e08c798d1e8d59d407aeb6e24db1313af`。本节补证既有 P1-9/P1-10，不新增问题编号。

**结论先行**：仓库当前没有 `agent_event` 权威事件表。主链路把 UI 会话历史、部分已完成的 Agent 消息/ActionOutput、内存中的步骤列表、成功操作快照文件分开保存。它们不是一条可按序重建的 rollout。注意这不等于“完全没有 Agent 消息持久化”：循环继续时，部分中间回复会在工具执行后通过 self-send 写入 GptsMessages；但它不是工具执行前的 write-ahead 记录，终止工具的最终轮也不会走同一条 self-send 路径。

#### 五种场景的写入点、恢复点与失败窗口

1. **普通文本轮次**
   - 创建 StorageConversation 后先保存会话对象，再把本轮用户消息加到对象；模型运行期间的 `history_steps` 也只是内存列表。终止路径才把包含步骤的 history payload 作为 view 消息写入并保存（`agentic_data_api.py:2310-2357,2999-3003,3566-3581`；`core/interface/message.py:859-912,1342-1357`）。
   - 下一轮只从已存会话历史取 human/ai；view JSON 只提取 `final_content` 作为 assistant 历史（`agentic_data_api.py:2334-2356`）。
   - 随后模型输入构造仍按历史消息奇偶位置赋 HUMAN/AI role，而非由事件本身显式恢复 role（`core/base_agent.py:1413-1425`；对应既有 P1-10）。
   - 🔴 **已验证差距**：本轮用户输入在终止保存前尚未写入 message storage；若运行中进程中断，本轮用户输入和已流出的步骤可能不在可恢复历史中。恢复的是已完成问答，不是中断现场。

2. **单个 native tool call**
   - LLM 返回的 tool_calls 暂存在 Agent 实例 `_pending_native_tool_calls`；解析后原始 arguments 字符串变成参数 dict，再交给工具执行（`tool_calling_agent.py:451-490,254-280`）。
   - 单工具分支只回填工具名，没有保留 call_id 或原始参数字符串；ActionOutput 在工具执行后才附到 reply message。若循环继续，之后才通过 self-send/receive 写入 GptsMessages；若动作 terminate，循环直接结束，不经过该 self-send（`tool_action.py:308-322`；`core/base_agent.py:675-695,740-756,392-403,433-434,993-1046,1088-1095`）。
   - 🔴 **已验证差距**：单工具路径无法从已存 ActionOutput 原样还原 provider 返回的调用结构；工具执行前也没有权威的调用记录。

3. **并行多个工具**
   - `asyncio.gather` 等全部调用返回后才生成一个聚合 ActionOutput；其中多工具结果含 call_id，但作为聚合 JSON 一次生成，不是每个工具完成时独立追加（`tool_action.py:324-395`）。
   - 🔴 **已验证差距**：批次中已有工具成功、其他工具仍运行时若进程中断，已完成结果可能没有可恢复记录；聚合数组按输入 spec 顺序构造，不能表达真实完成顺序。

4. **工具错误与重试**
   - 工具异常被转换为失败 ActionOutput；正常运行时错误 observation 可进入下一轮，但持久化取决于后续消息路径，UI 的 history_steps 仍在内存中直到终止保存（`tool_action.py:169-251`；`core/base_agent.py:730-756`；`agentic_data_api.py:3416-3471,3566-3581`）。
   - ⚖️ **权衡取舍**：运行期间将错误交给模型继续尝试是现有 Agent 策略；但错误结果应成为可恢复记录中的工具结果，不应只存在于当轮内存或最终 UI 文本。

5. **外部副作用已发生、结果尚未记录时崩溃**
   - 操作快照由成功动作的记忆写入阶段生成，属于动作完成后的文件快照，不是工具执行前的调用日志，也不是统一事件序列（`core/role.py:320-350,357-405,408-465`）。
   - 🔴 **已验证差距**：崩溃后无法仅靠现有记录区分“工具尚未执行”与“副作用已发生但返回值丢失”。该窗口不能靠增加一条事后 ActionOutput 自动消除。
   - 💭 **建议/阶段边界**：第一阶段将缺结果的调用标成未决，不自动重跑；写入型工具若以后要自动续执行，需另行设计幂等键或工具级对账，不属于本轮 rollout 日志范围。

#### 单表 rollout 的记录边界

- 记录模型实际交互所需的规范化顺序：用户输入；模型实际返回的 assistant 消息（包括 native tool_calls 的 ID、名称和原始参数）；每个 call_id 对应的工具结果/错误；assistant 后续消息；必要的 turn_started / turn_completed / turn_aborted 等终态标记。每个事件使用同一事件表中的单调序号排序。
- Codex rollout 同样是筛选后的记录：FunctionCall 与按 call_id 关联的 FunctionCallOutput 是独立 ResponseItem；TurnStarted/TurnComplete/TurnAborted 等生命周期事件被 policy 选中，且 `send_event_raw` 确实包装并交给 rollout append 路径；大量 delta/runtime 事件被排除（Codex `protocol/src/models.rs:878-929`、`rollout/src/policy.rs:8-20,37-58,85-182`、`core/src/session/mod.rs:2061-2088`）。工具调用先记录再排入工具执行，输出在 future 完成后记录；本地 thread store 的 append 有 recorder flush barrier（Codex `core/src/stream_events_utils.rs:296-326`、`core/src/session/turn.rs:2096-2113`、`thread-store/src/local/live_writer.rs:280-335`）。但高层 `persist_rollout_items()` 只记录 append 错误、不向调用方传播（`core/src/session/mod.rs:3628-3635`），所以不能把 Codex 说成已保证“落盘失败就阻止副作用”。JSONL 的 file flush 也不能直接等同 DB 事务提交或硬件 fsync。完整逐项对照与 DB-GPT 适配见[会话记录设计文档 §10.8](DB-GPT-Agent-Rollout会话记录与上下文恢复设计.md#108-codex-rollout-源码对照与-db-gpt-适配决策2026-09-28)。
- 💭 **建议**：`action_output_created` 是 DB-GPT 执行后的聚合对象；若能由工具调用和逐个结果确定性重建，就作为 UI/兼容投影，不重复作为权威事件。`tool_call_started` 是额外运行状态信号，不是 Codex rollout 的必需等价物；第一阶段可凭“已记录 assistant 调用、尚无对应结果”标记未决，但不得据此自动重放。
- 🔴 **已验证差距（上下文重建边界）**：系统提示词、运行时资源和工具 schema 会动态组装；当前没有保存每次请求最终送给模型的完整有效 messages/tools 快照。因此“按事件恢复对话历史”与“逐字复现原始模型请求”不是同一验收目标。后者是否需要保存请求快照或不可变配置版本，留待表结构/隐私策略评审，不在本节擅自决定（`base_agent.py:1380-1451`；`tool_calling_agent.py:458-485`）。

#### 现有测试覆盖与缺口

- 已有生命周期测试验证历史持久化失败时仍发 final/done、运行时失败有结构化 final；工具测试验证并行部分失败结果和 native 多工具 call_id 聚合；数据库测试只验证 TEXT/LONGTEXT 方言类型（`test_agentic_data_api_lifecycle.py:18-44,87-141`；`test_parallel_tool_action.py:74-88,146-167`；`test_gpts_messages_db.py:8-19`）。
- 🔴 **已验证差距**：本次检索未发现覆盖“逐事件写入 → 模拟进程中断 → 新进程从权威事件记录重建模型输入”的集成测试；当前也没有 agent_event 表可供此类测试验证。

> **D.7 结论（恢复建议已被 D.14 取代）**：P1-9 的核心缺口不只是“恢复时格式有损”，还包括**事件写得太晚、写入分散、并行结果没有逐个落点**。第一阶段实施规格建议已整理到独立设计文档。此处历史上“模型输入只恢复到最后完整 turn”的建议已被 D.14 明确撤销：当前约束是保留整轮，只处理缺结果的具体调用。OpenAI 官方 Chat Completions 工具调用示例以 `tool_call_id` 关联 assistant tool call 与 `role=tool` 结果，结果内容可为字符串（[Function calling](https://developers.openai.com/api/docs/guides/function-calling)，访问于 2026-09-28）；这只证实协议形状，不等于第三方兼容 provider 已通过恢复占位消息测试。事件设计与并发方案仍属 💭 **建议/待评审**，尚非实现事实。Codex 源码适配结论见设计文档 §10.8；审计未运行新测试，线上 tracer 持久化情况仍待核实。

### D.8 会话记录落地前兼容性核对（第 5 轮，2026-09-28）

> **范围与版本**：核对元数据数据库支持、Chat Completions 本地消息序列化及 provider 证据边界；不连生产数据库、不发真实 provider 请求、不改代码。DB-GPT commit `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`。实施规格与候选流程见[独立设计文档 §8](DB-GPT-Agent-Rollout会话记录与上下文恢复设计.md#8-落地前兼容性核对2026-09-28)。

#### 数据库与 payload

- **项目现状（源码证据）**：元数据库默认 SQLite `pilot/meta_data/dbgpt.db`，配置文字列 SQLite/MySQL/OceanBase（`packages/dbgpt-app/src/dbgpt_app/config.py:332-340`；`packages/dbgpt-app/src/dbgpt_app/base.py:57-78`）。仓库存在 PostgreSQL datasource connector，初始化入口也接受通用 RDBMS 参数（`packages/dbgpt-ext/src/dbgpt_ext/datasource/rdbms/conn_postgresql.py:30-63`；`base.py:65-88`）；但 SQLite 才执行自动 `create_all`/Alembic，非 SQLite 走手工 schema 路径（`base.py:132-163`），且未核实 PostgreSQL 元数据库完整迁移/运行验收。不能说“没有 PostgreSQL connector”，也不能把 datasource connector 当作 metadata backend 已验证。
- ⚖️ **权衡取舍**：事件 `payload` 使用 JSON 编码不要求切换 PostgreSQL。现有大文本实现 `Text().with_variant(LONGTEXT(), "mysql")`（`packages/dbgpt-serve/src/dbgpt_serve/agent/db/gpts_messages_db.py:18-26`）可作跨方言参考；查询/排序字段应独立成列。仅当 payload 内部查询和索引成为已验证需求时，再评估原生 JSON 类型。不能从“JSON payload”直接推出“应迁移 PostgreSQL”。
- 💭 **未决**：`chat_history` 有 `conv_uid` 唯一会话行（`packages/dbgpt-core/src/dbgpt/storage/chat_history/chat_history_db.py:25-55`），但无事件序号计数或活动轮次互斥实现。把它作为协调锚点需要扩展现有 schema；是否选用待并发方案设计及迁移评审。

#### Chat Completions/provider 兼容边界

- **已验证的本地实现与接线路径**：`ModelMessage` 支持 assistant `tool_calls` 和 tool `tool_call_id`；消息转换能产出 assistant/tool 配对形状（`packages/dbgpt-core/src/dbgpt/core/interface/message.py:184-204,508-534`）。OpenAI client 使用 `chat.completions.create`（`chatgpt.py:345-372`），LiteLLM 使用转换后的 messages 调 `acompletion`（`litellm.py:231-266`）。这只证明“代码可表达/请求路径已接线”。
- **官方常规协议证据**：OpenAI 官方工具调用示例展示 `assistant.tool_calls[].id` 与 `role=tool.tool_call_id` 配对（[OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling)，访问 2026-09-28）；Moonshot 官方示例和 Qwen 官方文档也展示同一类结果回填结构（[Moonshot Tool Calls](https://moonshot-ai.gitbook.io/moonshot-ai/getting-started-guide/use-kimi-api-for-tool-calls)、[Qwen Function Calling](https://help.aliyun.com/en/model-studio/qwen-function-calling)，访问 2026-09-28）。
- 🔴 **已验证差距（测试覆盖）**：本地 serializer 冒烟能生成标准结构，`uv run pytest packages/dbgpt-core/src/dbgpt/core/interface/tests/test_message.py -q` 为 30 passed；但这组测试没有覆盖工具调用与结果配对恢复。本轮未向真实 endpoint 发恢复请求，这是集成测试尚未运行，不代表标准 Chat Completions 消息形状本身不明确，也不是开始实现的前置阻塞。synthetic result 不能写入权威事件；它也不提供 exactly-once 副作用保证。
- 💭 **常规集成覆盖**：仓库 profile 包括 OpenAI、Kimi、Qwen、MiniMax、GLM、custom OpenAI-compatible endpoints（`packages/dbgpt-core/src/dbgpt/cli/_profiles.py:87-165`）。各实际部署 provider 的方言差异可纳入常规集成测试；不必先逐一证明标准 `role=tool` 文本结果可被接受。

#### 顺序与并发约束

- **数据库并发事实（官方文档）**：SQLite 写入事务存在单 writer 限制；`BEGIN IMMEDIATE` 可提前争取写事务，冲突可能得到 `SQLITE_BUSY`（[SQLite Transactions](https://www.sqlite.org/lang_transaction.html)、[SQLite Isolation](https://www.sqlite.org/isolation.html)，访问 2026-09-28）。MySQL InnoDB 支持在短事务中对索引行执行 locking read（[MySQL InnoDB locking reads](https://dev.mysql.com/doc/refman/8.0/en/innodb-best-practices.html)，访问 2026-09-28）。
- ⚖️ **不可混淆的两个保证**：唯一约束 `(conv_id,event_seq)` 防止重复序号，但不会自动阻止两个请求的轮次交错。轮内并行工具结果只需逐个短事务 append；same-conv 单活动轮次需要额外互斥。不能在模型/工具执行期间持有数据库事务。
- 💭 **实现尚未决策**：评估复用 `chat_history` 行原子更新序号/活动轮次字段、独立 DB lease，或上层队列串行化；还须覆盖 SQLite、MySQL、OceanBase 的锁语义、进程重启及租约失效后的旧 worker fencing。第一阶段不因该问题增加 `agent_stream`/`agent_turn` 表，也不把 Agent trace 混入 rollout。

> 🔴 **已验证差距**：尚无本地测试覆盖跨进程恢复与 synthetic/unknown 工具结果的 provider 集成验收；ReAct HTTP 入口未实现按 `conv_id` 的整轮互斥，不能证明并发同会话请求会排队或拒绝（`agentic_data_api.py:72,2310-2319,2980-2989,3835-3862`）。
>
> ⚖️ **权衡取舍**：沿用项目已支持的元数据库，以 JSON TEXT/LONGTEXT 保存 payload；没有证据表明仅因 payload 是 JSON 就应迁移 PostgreSQL。
>
> 💭 **下一步建议**：实现设计前先确认部署数据库/provider 矩阵及同会话并发策略，再确定 DDL 和事务序号分配；本轮不批准 schema，也没有修改代码或数据库。

### D.9 本机配置与同会话并发核对（第 6 轮，2026-09-28）

> **范围与版本**：核对当前 CLI profile、ReAct HTTP 入口的同会话并发控制、既有会话存储锚点和 v1 方案边界。DB-GPT `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`。本机配置字段经过白名单读取，未读取/输出密钥；本机状态不代表生产部署。

#### 本机环境和 DB 支持边界

- **本机配置事实**：`uv run dbgpt setup --show` 报告 active profile=`qwen`，配置路径 `~/.dbgpt/configs/qwen.toml`；脱敏读取确认 `service.web.database.type=sqlite`、path=`pilot/meta_data/dbgpt.db`，LLM=`qwen-plus` / `proxy/tongyi` / DashScope compatible-mode。API key/password 未读取。
- **PostgreSQL 结论修正**：配置说明列 SQLite、OceanBase、MySQL；仓库确有 PostgreSQL datasource connector（`packages/dbgpt-ext/src/dbgpt_ext/datasource/rdbms/conn_postgresql.py:30-63`），元数据库初始化也接受通用 RDBMS 参数（`packages/dbgpt-app/src/dbgpt_app/base.py:65-88,166-206`）。但初始化升级路径只对 SQLite 执行 `create_all`/Alembic；非 SQLite 要手工 schema，代码对 MySQL/OceanBase 才有创建 database 特判（`base.py:132-163,176-206`）。因此准确说法是：**PostgreSQL connector 存在，不等于 PostgreSQL metadata backend 的完整迁移/运行已验证**；不应笼统写“完全没有 PostgreSQL 支持”，也不应仅因 JSON payload 就换库。
- **模型协议证据**：Qwen 官方文档示范把 assistant 原始工具调用和 `role=tool` / `tool_call_id` 结果加入 Chat Completions messages（[Qwen Function Calling](https://help.aliyun.com/en/model-studio/qwen-function-calling)，访问 2026-09-28）。这支持常规工具结果形状，不证明 synthetic/unknown 恢复占位消息已通过目标 endpoint 测试。

#### 同会话并发现状

- 🔴 **已验证差距**：ReAct endpoint 是 async streaming route，每次请求会启动独立 Agent task；入口检索未发现以 `conv_id` 为键的整轮锁（`packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py:3835-3862,2980-2990`）。同文件全局 `REACT_AGENT_MEMORY_CACHE` 按 `conv_id` 共享 `GptsMemory`（`:72,2310-2319`）。所以现状没有证据表明并发请求会自动排队/拒绝；此结论指保护机制缺失，不声称已实测数据损坏。
- **可复用的既有行（源码证据）**：Agent 开始前创建并保存 `StorageConversation`（`agentic_data_api.py:2321-2333`）；`ConversationServe` 用 SQLAlchemy 将其映射至 `chat_history`（`packages/dbgpt-serve/src/dbgpt_serve/conversation/serve.py:90-115`；`packages/dbgpt-core/src/dbgpt/storage/chat_history/storage_adapter.py:22-48`），且 `chat_history.conv_uid` 唯一（`packages/dbgpt-core/src/dbgpt/storage/chat_history/chat_history_db.py:25-40`）。这是可考虑扩展的协调锚点，当前并无序号头或活动 turn 控制。
- **单进程边界（源码证据）**：内置 `uvicorn.run()` 未显式设置 `workers`（`packages/dbgpt-app/src/dbgpt_app/dbgpt_server.py:258-276`）；不能由此推断线上没有多 worker/多副本。进程内锁不提供跨进程互斥。

#### 第一阶段建议（未批准实现）

- 🔴 **必须满足**：同一会话的 turn 不交错；事件按 `(conv_id,event_seq)` 唯一、递增追加。`agent_event` 仍是唯一权威事件记录；`turn_id` 是行字段，不加 `agent_stream` / `agent_turn` 表。
- ⚖️ **第一阶段语义建议**：同一 `conv_id` 同时只允许一个用户 turn；不同会话仍可并行，单个 turn 内的并行 tool calls 不受影响。若产品需要两个用户 turn 同会话并行，须另定义分支/合并语义。
- 💭 **并发实现建议**：以所有相关服务实例共享同一元数据库为前提，在数据库层原子领取会话 turn 并分配事件序号；不把本机单进程配置升格为生产要求。SQLite 可用短写事务，MySQL 可锁定现有会话行；OceanBase 兼容性待测。`chat_history` 若承载协调字段，也仅是锁/序号元数据，`agent_event` 仍是唯一权威事件流。
- 💭 **拓扑的作用与边界**：worker 数量不是逻辑设计的前置条件；只有在验证跨实例互斥时，才要确认各实例是否共享同一数据库。若每个副本各有独立 SQLite 文件，数据库锁彼此不可见。中断后如何判定 stale owner 与释放占用也要在部署集成阶段测，不影响当前继续细化事件表与写入事务。
- 💭 **冲突请求 API 建议**：UI 禁止重复提交，服务端对已有活动 turn 建议立即返回 409 而不排队；这是推荐产品行为，不是当前 API 契约。

> **D.9 结论**：本机 SQLite + Qwen Plus 仅是 PoC 测试配置。可以继续按“同一会话一个活动用户 turn、turn 内工具仍可并行、共享元数据库负责原子互斥/序号分配”细化方案；生产拓扑只决定跨实例集成验收及 stale-owner 清理策略，不是逻辑设计的阻塞条件。409 是建议的冲突响应方式，尚非既定 API 契约。

### D.10 `agent_event` 单表规格收敛（第 7 轮，2026-09-28）

> **范围与版本**：本节是 P1-9 的设计摘要，不是 schema 批准或实现报告。DB-GPT checkout `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`；Codex checkout `7a0e974e08c798d1e8d59d407aeb6e24db1313af`。实现现状详见 D.7，落地兼容和并发核对详见 D.8-D.9；详细规格见[会话记录设计文档 §10](DB-GPT-Agent-Rollout会话记录与上下文恢复设计.md#10-agent_event-单表评审规格第-7-轮)。

- 🔴 **已验证差距**：DB-GPT 当前没有 `agent_event` 权威事件表，也没有“逐事件提交→进程中断→新 session 从事件重建模型输入”的集成验收（D.7；DB-GPT `ca9f014...`）。当前 UI `chat_history`、`GptsMessages` 和内存 Agent 状态不是一条有序、可恢复的 rollout。
- ⚖️ **权衡取舍**：本设计只记录模型交互协议事实及必要轮次终态；UI 历史可投影，Agent trace 另行负责运行观测。第一阶段不存完整 provider 请求快照、不采集 SSE/span 全事件、不做 runtime checkpoint 或工具自动重放。沿用项目元数据库、payload 使用 JSON 文本是跨方言选择，不代表 PostgreSQL JSONB 是必需项。
- 💭 **待评审建议**：只设 `agent_event` 作为事件事实表，含 `event_id/conv_id/event_seq/turn_id/event_type/schema_version/payload/created_at`；v1 事件为 `turn_started`、`user_message`、`assistant_message`、`tool_result`、`turn_completed`、`turn_aborted`。事件 payload 与触发时点、事务不变量、恢复投影和验收矩阵见设计文档 §10。`turn_id` 仅为行字段，不建 `agent_stream`/`agent_turn`；同会话一个活动用户 turn，turn 内并行工具仍可执行。
- **仍未决定、实现前必须处理**：序号分配及活动 turn 的 DB 锁/崩溃后 fencing；旧 `chat_history`/`GptsMessages` 的清理范围与 cutover 执行边界（不迁移，见 D.12）；rollout 会话禁用 GptsMessages 跨请求回填的接线方式（见 D.13）；payload 脱敏、授权、保留/删除与大小限制。目标 provider 的行为差异作为常规集成覆盖，不再视为恢复投影的设计阻塞。任何一项都不能被当作当前已有能力。

> **D.10 结论**：下一步评审设计文档 §10 的事件边界、DDL/事务与同会话并发实现；逐调用恢复投影的 Chat Completions 消息形状已明确，provider 差异进入常规集成测试，不另设实现前 PoC。Codex rollout 生命周期持久化依据见 D.7 所列源码位置，不能将本设计解释为“把所有 Agent trace 写进 `agent_event`”。本轮只更新文档，未改代码、数据库或测试。

---


### D.11 旧历史切换与中断工具调用策略（第 8 轮，2026-09-28）

> **历史记录提示**：本节保留第 8 轮当时的懒 bootstrap 建议；用户随后决定旧会话不迁移、切换时清理。恢复边界的历史建议由 D.14 纠正并取代；当前恢复约束以 D.14 和设计文档 §11.6 为准。

> **范围与版本**：针对 ReAct 主链路和当前旧历史存储，只读核对 DB-GPT commit ca9f014cb3ead157ca2ee6ce645658f5fb55138c；不是迁移实现或线上状态。设计细节见[会话记录设计文档 §11](DB-GPT-Agent-Rollout会话记录与上下文恢复设计.md#11-旧历史切换与中断工具调用的方案比较第-8-轮)。

- 🔴 **已验证事实/差距**：当前 ReAct 续聊从 StorageConversation 读 human、ai 和 view.final_content，再交给 Agent；BaseAgent 又按历史消息奇偶赋 HUMAN/AI（agentic_data_api.py:2334-2357,2980-2986；base_agent.py:1413-1425）。这不是原始 provider tool transcript。
- 🔴 **已验证事实/差距**：聊天存储由 chat_history 元信息与 chat_history_message 明细组成，并兼容更早内嵌消息格式（chat_history_db.py:25-55,63-90；storage_adapter.py:27-73,95-134,137-155）。rich UI payload 还含 steps、task_plan、generated_images、sub_agents、input_files、citations（agentic_data_api.py:996-1028）；当前 v1 协议事件不足以无损重建这些展示字段。
- 🔴 **已验证事实/差距**：终态历史保存失败仍会发送 SSE final/done，测试明确覆盖该行为（agentic_data_api.py:1046-1062；test_agentic_data_api_lifecycle.py:18-44）；因此“用户看到了 final”不能推导“旧 DB 一定能迁到 event log”。
- 🔴 **已验证事实/差距（D.13 已细化）**：GptsMessages 恢复路径会尝试抽取 ActionOutput memory fragments 并加入模型消息（base_agent.py:245-259,1427-1428；gpts_memory.py:152-179；role.py:472-484），但目标 ReAct API 只写部分 self-send 消息，恢复又依赖相邻两行配对；不能假设它是可靠的 turn memory。若被恢复，仍没有 event turn-complete 过滤。详细事实与建议见 D.13/设计文档 §11.5。
- ⚖️ **权衡取舍**：旧 UI payload 含展示信息但不是原始工具协议；可以暂时保留为 UI read model，不能宣称单靠协议事件无损生成全量 UI。GptsMessages 工作记忆也不能伪装成带原始 call ID/arguments 的 tool_result。
- 💭 **第 8 轮历史建议（已被 D.14 取代）**：每个 conv 第一次切换时，按现有 StorageConversation 读取路径懒 bootstrap 当前可见的 user/assistant transcript，并标注 legacy 来源；之后该 conv 的协议 transcript 以 agent_event 为准。当时曾建议模型只收到最后完整 turn 前缀；该恢复边界已被 D.14 撤销。旧会话迁移建议也已由用户决定“不迁移、切换时清理”。
- **实现门槛**：定 bootstrap 幂等标识/marker、异常历史处理和大历史上限；决定 rich UI projection 的保留周期；明确 GptsMessages memory 如何按 turn 过滤或暂时关闭；确定中断 turn 的 UI 行为。以上均未实现。

> **D.11 结论（历史建议，已被 D.14 取代）**：D.10 曾把 synthetic/unknown tool message 作为未决调用的一种恢复候选；本轮比较后，它保留为未来扩展，不再是第一期建议。第一期优先避免把未知副作用当成可安全续跑；当时提出的完整前缀恢复仅为历史建议，现已撤销。当前按 D.14 保留整轮，只处理缺结果的具体调用。

---


### D.12 旧会话清理决定与 Codex 中断工具恢复核对（第 9 轮，2026-09-28）

> **范围与版本**：DB-GPT commit ca9f014cb3ead157ca2ee6ce645658f5fb55138c；本地 Codex codex-rs commit 7a0e974e08c798d1e8d59d407aeb6e24db1313af。只读源码和设计文档更新；不是线上运行时观测，也未执行清理或代码修改。D.12 更新并取代 D.11 中的旧历史懒 bootstrap 建议。

- ✅ **用户已定切换策略**：旧会话不迁入 agent_event；切换后从空的新会话开始，旧会话记录按明确范围清理即可。因此 history_baseline_imported、legacy transcript bootstrap、保留旧 UI history 供续聊的建议取消。实际清理前仍须明确环境、conv 范围、关联 chat_history_message / GptsMessages、备份与回滚；本轮没有删除任何数据。
- 🔴 **Codex 已验证实现**：模型 FunctionCall item 先持久化，随后才启动工具；工具 future 返回后才持久化结果（core/src/stream_events_utils.rs:296-326; core/src/session/turn.rs:2096-2113）。所以中间崩溃会留下未闭合调用，而没有工具运行检查点。
- 🔴 **Codex 已验证恢复**：停止的 thread 从 rollout 读取历史并构造 InitialHistory::Resumed（core/src/agent/control/spawn.rs:930-952,991-1007），新 Session 重建 response history（core/src/session/mod.rs:1321-1335; core/src/session/rollout_reconstruction.rs:317-340）。若 thread 仍在运行，resume 复用存活的内存 thread；已停止的 thread 才会从 rollout 重新创建（core/src/thread_manager.rs:1642-1661）。这不是从工具函数内部断点续跑。
- 🔴 **Codex 已验证协议补齐行为**：下一次模型请求先对 clone_history() 的副本调用 for_prompt()（core/src/session/turn.rs:334-340）；归一化发现缺少 output 的 function/custom tool call 时，插入与原 call ID 配对、内容为 aborted 的 synthetic output（core/src/context_manager/history.rs:141-146,329-336; core/src/context_manager/normalize.rs:20-61,81-95）。测试验证 function call 缺结果时补 aborted（core/src/context_manager/history_tests.rs:1420-1453）。这是 prompt-only 修复，不写回 rollout、不重跑旧工具。
- ⚖️ **受控取消 vs 崩溃**：Codex 的受控 cancellation 可收尾并产生 aborted by user 工具响应（core/src/tools/parallel.rs:178-217），之后由 drain 写回结果；硬崩溃则可能没有这个机会。即使 prompt 中显示 aborted，也不证明外部副作用未发生；后续模型仍可能在新响应中提出新的同类调用。
- 💭 **第 9 轮历史建议（已被 D.14 取代）**：当时曾建议首期维持完整 turn 前缀恢复、不自动 replay，并把 provider PoC/副作用重试列作门槛；这两项当前均不再是恢复协议投影的前置条件。D.14 的当前方案是按 Codex 逐 call 补齐，provider 差异归常规集成测试；具体工具的副作用策略另按风险处理。
- **当时记录的未解决门槛（由 D.13 细化）**：新事件历史截断不能自动约束 GptsMessages memory 回填。后续专项核对发现，更基础的问题是目标 API 只写入部分 self-send 消息，而恢复器按相邻两行配对；D.13 又澄清，改进后的 `tool_result` 已承担实时工具结果上下文，因此默认既关闭跨请求回填，也不额外注入重复的 turn 内 `Observation:` memory。

> **D.12 结论（恢复建议已被 D.14 取代）**：旧历史迁移不做，切换清空；Codex 对已持久化但缺工具结果的调用不是恢复执行现场，而是在下一次模型请求的历史副本中补一个 aborted 配对输出。当前 DB-GPT 方案按 Chat Completions 标准消息形状逐 call 补齐；本节当时提出的“完整 turn 前缀”和 provider PoC 前置建议均已撤销。GptsMessages 的真实主链路写入/恢复不匹配问题由 D.13 继续核查。

### D.13 GptsMessages 工作记忆与 rollout 唯一来源边界（第 10 轮，2026-09-28）

> **范围与版本**：DB-GPT commit `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`。只读跟踪目标 `/agent/stream` ReAct API 从 Agent 初始化、GptsMessages 写入/恢复到模型上下文组装的源码；未运行真实数据库/provider 故障注入，未改代码、数据或配置。完整调用链和限制见[会话记录设计文档 §11.5](DB-GPT-Agent-Rollout会话记录与上下文恢复设计.md#115-gptsmessages-与-rollout-边界专项核对第-10-轮)。

- 🔴 **已验证差距：它不是一份完整 rollout。** Agent `build()` 会按 `conv_id/role` 查询 GptsMessages，并从 `action_report` 恢复 memory fragments（`packages/dbgpt-core/src/dbgpt/agent/core/base_agent.py:245-259`；`packages/dbgpt-core/src/dbgpt/agent/core/memory/gpts/gpts_memory.py:152-179`）。但目标 API 直接调用 `agent.generate_reply()`，而不是先调用 `receive()`；初始 user input 不经过 GptsMessages append。只有 loop 内部 self-send 的 retry/中间消息经过 `_a_process_received_message()` 写入；API 直接收到的最终返回也不走该 self-send 路径（`packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py:2960-2988`；`packages/dbgpt-core/src/dbgpt/agent/core/base_agent.py:405-434,538-566,751-757,993-1046,1088-1096`）。
- 🔴 **已验证差距：恢复配对不具备事件级关联保证。** `get_agent_history_memory()` 每两行一组并只选第二行的 `action_report`（`gpts_memory.py:152-179`）；数据库查询按 conv 后缀和 `rounds` 排序，而非 `id/event_seq`（`packages/dbgpt-serve/src/dbgpt_serve/agent/db/gpts_messages_db.py:145-173`）。`GptsMessages` 没有 `turn_id/event_seq`。源码无法保证跨 turn 或重复 rounds 时仍能配成原始的用户问题/Agent 动作对；需真实 DB 验收，不能仅依据函数注释推断恢复正确。
- 🔴 **已验证旁路：恢复成功的片段会进入模型输入。** `ActionOutput.memory_fragments` 存的是由 thought/action/action_input/observation 渲染的 ReAct 摘要（`packages/dbgpt-core/src/dbgpt/agent/core/role.py:303-349,390-405`；`packages/dbgpt-core/src/dbgpt/agent/expand/react_agent.py:84-93`）；恢复后 ReActAgent 转为普通 HUMAN/AI 消息，并插在历史对话和当前输入之间（`react_agent.py:274-364`；`base_agent.py:1413-1449`）。这不是带原始 `tool_call_id/arguments` 的 provider tool result，但其 observation 可能语义重复工具输出；读取不按 rollout 的 `turn_completed` 过滤。
- ⚖️ **职责区分**：GptsMessages 可承载 ReAct 的局部工作记忆/可视化消息，不等于模型协议权威历史，也不因有 `trace_id` 就等同完整 Agent trace。rollout 的目标仍是一份有序、可恢复的会话协议记录；不要为解决 Agent trace 而把所有运行时事件塞入 `agent_event`。
- 💭 **第 10 轮历史建议（已被 D.14 取代）**：rollout 路径的实时模型上下文从有序 `agent_event` 构造，包含当前活动 turn 中已发生的 assistant tool calls 与 `tool_result`；当时曾建议进程中断后只投影最后完整 turn 前缀，该恢复边界现已撤销。关闭 GptsMessages 跨请求回填，也不把相同工具结果再注入为 `Observation:` memory 消息。若将来确需压缩摘要/跨 turn 记忆，须证明其提供协议历史没有的信息，并携带来源 turn/event；不能让 GptsMessages 成为第二权威 transcript。当前恢复约束见 D.14。
- **待核实**：①真实 SQLite/目标 DB 方言下，单步终态、多步 tool、retry、连续多 turn 的 GptsMessages 行和恢复结果；②验证活动 turn 的每个后续模型请求都从事件投影得到正确配对的 `tool_result`，且不重复注入 ReAct `Observation:` memory；③若后续提出压缩摘要，评估它是否提供协议 transcript 未含的信息及 token/语义收益。定向测试检索未发现 `get_agent_history_memory()` / `recovering_memory()` 的专测；本轮没有运行测试。

> **D.13 结论（恢复边界由 D.14 纠正）**：目标主链路的 GptsMessages 不是完整问答日志，恢复器按两行配对也没有可靠 turn 关联。对改进后的 Chat Completions 路径，工具结果应由 `tool_result` 消息提供；“Observation”只是当前 ReAct 的旧 prompt 标签，不应再把相同结果作为短期 memory 重复拼入上下文。实时 turn 从事件序列继续；崩溃后的整轮保留及缺失 call 的处理以 D.14 为准，不能按最后完整 turn 前缀截断。

### D.14 恢复边界纠正：保留整轮，只处理未决工具调用（第 11 轮，2026-09-28）

> **范围与依据**：纠正此前把“跳过缺结果的工具调用”误解/扩大为“恢复时丢弃整个未完成 turn”的建议。Codex 源码对照版本为 `codex-rs` commit `7a0e974e08c798d1e8d59d407aeb6e24db1313af`；DB-GPT 当前代码基线仍为 commit `ca9f014cb3ead157ca2ee6ce645658f5fb55138c`。本轮只修订设计/清单文档，未改代码、数据库或测试。详见[会话记录设计文档 §11.6](DB-GPT-Agent-Rollout会话记录与上下文恢复设计.md#116-恢复边界纠正保留整轮只处理未决工具调用2026-09-28)。

- 🔴 **用户明确约束**：恢复时绝不能跳过整个未完成轮次。user input、assistant 原始 `tool_calls` 和本轮已有的真实 `tool_result` 都必须保留；同一 assistant 消息中多个调用只缺一个结果时，不能连已完成的其他调用和结果一起丢弃。
- 🔴 **Codex 源码事实**：Codex 对历史副本运行 `for_prompt()`，对缺少 output 的具体 function/custom tool call 按 call ID 补 prompt-only `aborted` output（Codex commit `7a0e974e08c798d1e8d59d407aeb6e24db1313af`：`core/src/session/turn.rs:334-340`、`core/src/context_manager/history.rs:141-146,329-336`、`core/src/context_manager/normalize.rs:20-61,81-95`；测试 `core/src/context_manager/history_tests.rs:1420-1453`）。这不是丢弃整轮，也不把 synthetic output 写回 rollout 或重跑旧工具。该源码是实现证据，不证明所有 Codex 产品运行时都相同。
- 🔴 **风险事实/约束**：硬崩溃时缺少真实结果，不能据此断定工具没执行；它可能已造成外部副作用。因此不能自动重放旧调用，不能将 synthetic output 伪装为持久化的真实工具结果。
- 💭 **DB-GPT 实施建议（协议映射明确，未实现）**：权威 `agent_event` 保留整轮及全部真实事件；投影模型输入时保留原 assistant `tool_calls` 和已完成调用的真实结果，只为缺结果的 call_id 加一条 prompt-only `role=tool`、`tool_call_id` 匹配、文本 `content` 的消息。OpenAI 官方 Chat Completions schema 与示例支持这种消息形状及字符串结果（[Chat API schema](https://developers.openai.com/api/reference/cli/resources/chat)、[Function calling](https://developers.openai.com/api/docs/guides/function-calling)，访问于 2026-09-28）。占位文本可先沿用 Codex 的 `aborted` 以便实现对齐；若要更准确表达硬崩溃的未知结果，可改成说明 outcome unknown 的文本，这不是 wire-protocol 风险。synthetic 项绝不 append 到权威事件日志，不重跑旧工具，也不跳过整轮。
- ⚖️ **协议/执行风险分开处理**：Codex 使用 Responses `ResponseItem`；DB-GPT 使用 Chat Completions，wire format 不同，但逐 call_id 补齐的恢复思路一致。符合 OpenAI Chat Completions 形状的标准 provider 没有理由因 content 是普通文本而拒收；第三方兼容差异可纳入常规集成测试，不阻塞实现。另一方面，Codex 源码没有提供模型绝不再次发出同类新调用的通用 exactly-once 保证；这不是 prompt projection 可以解决的。仅对有外部副作用的工具按工具风险考虑幂等、状态确认或既有审批。
- **验收用例**：同一 turn 两个并行调用 A/B，A 有真实结果、B 因崩溃缺结果。恢复必须保留 user、原 assistant A/B calls、A 的真实结果；只给 B 补 prompt-only 配对文本；不重放 A/B，不将 B 的 synthetic 项落库，且绝不只投影上一轮完整前缀。用确定性测试断言投影结构和持久化不变；第三方 provider 行为做常规集成覆盖，不要求先完成 provider 矩阵 PoC。

> **D.14 结论**：此前“只恢复最后完整 turn 前缀”的建议已废止。当前方向是对齐 Codex 的“整轮保留、逐调用补齐”：只在模型输入副本中为缺结果的 call_id 补配对文本，不落库、不重放工具。Chat Completions 的消息形状明确；实现前用确定性投影测试验证即可，provider 差异属于常规集成测试。模型是否提出新的同类调用是执行策略风险，不是协议兼容障碍，也不是 Codex 在该 normalization 中已经解决的问题。

---

### D.15 压缩层结构失配与 rollout 基础设施落地复核（第 12 轮，2026-10-13）

> **范围与版本**：只读核对 DB-GPT 工作区（基线 `ca9f014`，2026-10-13）；未改代码/数据库、未做运行时验证。本轮更新 D.7-D.14 设计轮的**落地状态**，并修正 D.5/P1-12/P1-19/P1-23 中"压缩层可用"的隐含前提。

**✅ 已验证落地：rollout 事件基础设施已实现（D.7/D.10 曾记录"尚未实现"）**

- **权威事件表已存在**：`AgentEventEntity` + `AgentEventDao`——`(conv_id, event_seq)` 唯一递增、`event_id` 幂等、payload 校验、封闭事件类型 `turn_started/user_message/assistant_message/tool_result/turn_completed/turn_aborted`（与 D.10 规格一致）（`packages/dbgpt-serve/src/dbgpt_serve/agent/db/agent_event_db.py:35-44,103-120,130-224,409-495`）
- **turn 租约已实现**：`AgentTurnLease` + `acquire/renew/release/assert_turn_lease`（`ActiveTurnInProgress`/`TurnLeaseLost`）——D.9"同会话单活动 turn"的落地形态（`:62,225-304`）
- **投影器已实现**：`project_rollout_events`——user→HUMAN、assistant 保留 tool_calls、tool_result→`role=tool`；缺结果的调用在内存投影中确定性补 `content="aborted"`（与 D.14"逐 call 补齐、不落库、不重放"一致）（`packages/dbgpt-core/src/dbgpt/agent/core/rollout.py:49-105`）
- **主链路已接线**：`AgentRolloutSession(conv_id, …)`、`enable_rollout_history=True`、`configure_rollout_history(message_loader=load_projection, …)`（`agentic_data_api.py:2504,2553,3146,3156-3173`）；rollout 模式跳过旧记忆回填与 `task_progress` 注入（"In rollout mode the Chat Completions transcript is authoritative"，`core/base_agent.py:1347-1351,1363`）——与 D.13 决策一致
- D.10"仍未决定"清单中：序号分配与租约已落地；payload 脱敏、保留/删除、旧历史清理范围仍待办（不在本轮范围）

**🔴 已验证差距：四层压缩的 L1/L2/L3 在 rollout 结构下全部不触发**

- 三层的门控都依赖 `_is_observation_message`（`role==HUMAN and content.startswith("Observation:")`）与 `_detect_round_boundaries`（以 `Observation:` 消息闭合 round）（`core/context/compact.py:41-47,55-79`）
- rollout 投影产生的是 `role=tool` / `assistant(tool_calls)` 消息，**没有 `Observation:` 文本** → 无 round 闭合，全部消息成 **1 个 round**
- 结果：**L1** 扫而无获；**L2** `len(rounds)=1 ≤ min_keep_recent_rounds(3)` 直接返回；**L3** 同依赖 round 检测（`keep_rounds=min(3,1)=1 ≥ len(rounds)=1`）直接返回——**三层全部 no-op，只剩 L4 响应式兜底**（`compact.py:87-137,145-206,214-238`；`budget.py:52-55`）
- 连带失效的前提：D.5 对 L1/L2/L3 的行为描述、P1-12 的"让 L1/L2 先行"、P1-23 的"下游压缩层善后"

**🔴 已验证差距：压缩结果未接入事件流（"基础在、上层未接"）**

- 事件类型为**封闭集合**，append 拒绝未知类型——不存在 compaction/summary 类事件（`agent_event_db.py:409-410`）
- 压缩在每步 `_load_thinking_messages` 内对旧结构列表执行（`base_agent.py:1330-1445`），结果不写回事件流；下一步 `load_projection` 全量重放
- **且 rollout 分支的 provider 请求由投影重建**：`_thinking_native` 从传入列表只取 system + 末条用户兜底（`tool_calling_agent.py:512-545`）——压缩产物不进入请求（代码阅读结论，未运行时验证）
- 接入所需（均未实现）：① 新事件类型（如 `context_compacted`：摘要 + 被替换范围/水位）；② 投影器应用该事件（跳过被压消息、注入摘要）；③ 压缩后 token 重算与状态刷新

**💭 建议**：
1. 先重写 L1/L2/L3 的 round 检测以匹配原生 tool 消息（按 assistant `tool_calls` ↔ `role=tool` 配对与消息计数分 round），否则"压缩接线"无层可用
2. 压缩结果接入事件流（新事件类型 + 投影应用）——即 D.6 补充 3-E"压缩 checkpoint 化"的落地形态
3. 若旧文本路径保留，两套检测共存并各自回归

> **D.15 结论**：D.7-D.14 的 rollout 设计已大部分落地（事件表/租约/投影/主链路接线）；但**压缩体系仍按旧文本 ReAct 结构设计**——rollout 链路下 L1/L2/L3 全部不触发、压缩结果未接入事件流、请求由投影重建。"基础在、上层未接"成立；在接线压缩前必须先修门控结构。
>
> **后续**：移植与改造路线图（Codex 压缩 + Hermes 剪枝 → DB-GPT）见 **D.16**。

---

### D.16 改造路线图：Codex 压缩 + Hermes 剪枝 → DB-GPT（第 13 轮，2026-10-13）

> **范围与版本**：本节是**设计建议**（💭），不是实现报告；未改代码。事实依据：D.15/P1-25 的 rollout 复核（DB-GPT 基线 `ca9f014` 工作区）、D.2/D.6 的 Hermes/Codex 源码对照、以及逐点核对。凡涉及 DB-GPT 现状的均为一手核实；凡涉及落地方案的均为建议，标注"待评审/未核实"；**标注"已定"的条目为 2026-10-13 与用户确认的决策**。
>
> **实现级设计已单独成文**：《[DB-GPT-上下文压缩改造设计（Phase 0）](./DB-GPT-上下文压缩改造设计（Phase 0）.md)》（2026-10-13）——含 turn 判定表、`context_compacted` 事件 schema、新触发配置、rearm 存放决策、P1-19/P1-23 接线与测试验收计划。
>
> **决策更新（2026-10-13）**：估算器 = 独立 chars/4；P1-19 ② 暂不接线（① 与 P1-23 保留）；Phase 0 不动 UI；void 暂不做；向前兼容 = 部署纪律。**以设计文档 §11 决策记录为准。**
>
> **Phase 1+2 设计（2026-10-14）**：《[DB-GPT-上下文压缩改造设计（Phase 1+2）](./DB-GPT-上下文压缩改造设计（Phase 1+2）.md)》——去重 / 泄压阀 / 剪枝形态（激进一行）+ 摘要生产者 / 无硬帽 / trim-retry / 模板三版本；**Phase 1 与 Phase 2 合并为一批实施**（6 组提交）。

**0. 前提：先修地基（否则移植无落地处）**

- L1/L2/L3 在 rollout 结构下不触发（D.15）→ 任何剪枝/摘要组件没有宿主可挂
- 压缩结果必须事件化（事件类型封闭 + 每步全量重投影）→ 否则压缩产物进不了 provider 请求（`_thinking_native` 由投影重建，`tool_calling_agent.py:512-545`）
- 两个预算函数未接线 + read_file 无帽（P1-19/P1-23）→ "落盘读回"安全网有洞

**1. Hermes 剪枝组件 → 可移植性**

| 组件 | 判断 | 落地形态（建议） |
|---|---|---|
| Pass 1 去重（md5、保留最新、无损） | ✅ 先做 | 可作为**投影时变换**（无损、可重算、不写事件即可生效）或 compaction 事件 |
| Pass 2 信息性单行摘要（工具感知模板） | ⚖️ 部分采纳（已定） | **不建 per-tool 模板**；保留现状"截断 + 快照路径"。执行侧已写入结构化 `success`（`tool_action.py:446-458`）——如需可零成本加一行通用元数据（`[工具名] success=? · N chars · → path`），无新字段、无模板。门槛双档（压缩内 200 / 主动 8000，clamp≥200 防摘要被再摘要）照抄 |
| Pass 3 参数截断 | ❌ 不采纳（已定） | 发生频率低；**源头限额也不做**。从路线图移除 |
| Pass 4 压力降级 | ✅ 新增 | 配"消息数下限封顶"防死锁（#61932 场景：保护区被大输出堆满 → 无所可剪） |
| 边界对齐（call↔result 不切断） | ✅ 必须 | 投影器已有 aborted 合成；剪枝不得制造孤儿配对 |
| Never raises / 幂等守卫 | ✅ 原则层 | 加固层不得反过来搞挂主流程 |
| 主动路径五道门（水位/rearm/能力/回收量） | ⚠️ 适配 | 能力门→"事件类型扩展是否就绪"；rearm 水位持久化位置待定；回收量门 4096 tokens |
| 缓存契约（min_reclaim + rearm 跑道） | ✅ 全抄（已定） | **按"有前缀缓存"处理**：min_reclaim + rearm 跑道 + 水位持久化照抄，不核实 provider 差异 |
| DB 同步 → 事件流写入 | 🔁 结构替换 | **追加"解读指令"事件**（不改旧事件）：投影时应用（替换范围 + 注入摘要）+ turn lease 保护；载荷形态采用 **Codex 式"整份 replacement_history"**（应用=整体替换，最简，Codex 已证明可行；范围引用式留作后续优化） |
| 保留 DB-GPT 现有优点 | — | L1 跳过 persisted-preview（保留；升级为"摘要/事件携带可恢复资产清单"）、SSE 状态可视；**移除 task_progress 隐式摘要**（rollout 模式下已不注入，`base_agent.py:1363`，L2 前提失效） |

**2. Codex 压缩组件 → 可移植性**

| 组件 | 判断 | 说明 |
|---|---|---|
| 触发 90%+buffer | ⚠️ 不照搬 | 触发模型将**重划**（见 Phase 0）：**弃用 DB-GPT 的 70/90/95 三档**（已定），改"低水位主动维护 + 压缩阈值（可配、可按模型覆盖）"；对齐点是"每步检查 + 中轮压缩" |
| compaction turn 生命周期（hooks/遥测/事件） | ✅ 借鉴 | 配合 SSE 基础，把压缩变为可观测的 turn 级操作 |
| 摘要模板（handoff 四段） | ✅ 双模板对比（已定） | 六段（DB-GPT）与四段（Codex）**都保留做 A/B**；另做"合体第三版"= 六段骨架 + Codex 的"用户约束/偏好"槽 + handoff framing + Hermes 式"已完成勿重做"声明（路线图编号：**⑫**，Phase 2） |
| 摘要位置（历史最后一项） | ❌ 不采纳（已定） | **与 Hermes 对齐**：摘要居中（现状 `system + [摘要] + 最近消息` 已是同形状）+ 结束标记（`_SUMMARY_END_MARKER` 等价物）+ 机器可识别元数据键（学 `_compressed_summary`） |
| 保留用户消息预算 20k + truncate_text | ✅ 可抄 | 注意 UI 富字段兼容（D.11：steps/task_plan/generated_images 等） |
| **权威替换（live/persisted 一致）** | ✅ **最该抄** | = compaction 事件 + 投影应用 + token 重算；根治"每轮重算"（D.6 补充 3-E 的落地形态） |
| **no wire cap** | ✅ **必抄（纠错）** | DB-GPT 现为 `max_new_tokens=2000` 硬帽——正是 Hermes 用事故禁止的模式 |
| Trim-retry（逐条删最老、保配对、retries 清零） | ✅ 可抄 | 比 L4"留 system+2 轮"一刀切更渐进 |
| 旧摘要过滤（`is_summary_message` 等价物） | ✅ 需要 | 防摘要叠加与"旧任务被当新指令" |
| 模型 fallback（7 类错误换模型） | ⚠️ 看 provider 矩阵 | 多 provider 下有 fallback 价值 |
| 输入有界化（头尾保留+聚合上限） | ❌ **不采纳**（2026-10-13 已定） | 摘要输入走 **Codex 路线**：不预裁剪、全量喂入，超窗靠 trim-retry 兜底；**移除既有 `content[:3000]`**（`compact.py:255`，2026-10-13 已定，纯 Codex 全量喂入）——trim-retry 因此从"可抄"升级为 **Phase 2 必需项** |
| 服务端压缩 / 无摘要换窗档 | 💭 长线 | 需 provider 支持；可作新增档 |

**3. 三条硬约束（DB-GPT 特有）**

1. **事件溯源**：历史上限 = 事件重放（投影）→ 一切"改写已发送历史"必须落成"写事件 + 投影应用"，不能就地改
2. **封闭事件类型**：`rollout.py:17`（`_SUPPORTED_EVENT_TYPES`）+ `agent_event_db.py:35`（`_EVENT_TYPES`）两处封闭集合、append 拒绝未知类型（`:409-410`）→ 需 schema 演进（`context_compacted`：载荷采用 Codex 式**整份 replacement_history** + 水位/窗口记账 + `schema_version`）
3. **投影每步全量重放**：新事件必须在 `project_rollout_events`（`rollout.py:49`）中被"应用"——遇 `context_compacted` 即以其载荷**整体替换**累积历史（Codex 式），否则压缩结果被绕过

外加：子代理各自 `conv_id`（状态按会话隔离）；rearm/水位持久化位置（事件流 or 会话配置，待评审）；UI read model 不被破坏；P1-19/P1-23 前置。

**4. 路线图（Phase 0-3）**

- **Phase 0 修地基（必须最先）**：① L1/L2/L3 门控重写——结构检测 + 双分派（旧文本 ReAct 走旧逻辑；`role=tool` 走新逻辑：按 `tool_call`↔`role=tool` 配对与消息计数分 round）；**同时重划触发模型：弃用 70/90/95 三档（已定），改"低水位主动维护 + 压缩阈值（可配/可按模型覆盖）"**；**L2 归属在重写时一并定**——其"task_progress 顶替摘要"前提已失效（rollout 模式下不注入，`base_agent.py:1363`），待决：保留"整轮丢弃"（需新的进度件载体）或合并进剪枝/摘要层 ② 压缩事件化（新事件类型 + 两处类型集合扩展 + 投影应用规则；载荷 = 整份 replacement_history）③ 接线 P1-19 ①（窗口自适应）+ P1-23 read_file 帽；**P1-19 ② 已定暂不接线**（b3，回访条件见设计文档 §7.1 ②）
- **Phase 1 无损/廉价（快速见效）**：④ 去重（投影时变换先行）⑤ L1 保留"截断 + 快照路径"（可选零成本加一行 `success` 通用元数据，不建模板）+ 保留 persisted-preview 跳过 ⑥ Pass 4 压力降级 + 消息数封顶（Pass 3 不采纳）
- **Phase 2 权威化**：⑦ 摘要事件化提交（替代每轮重算；载荷 = 整份 replacement_history）+ token 重算 + lease 保护 + 旧摘要过滤 ⑧ 去 wire cap + trim-retry + 摘要结束标记/机器可识别元数据键（**输入有界化不做**，2026-10-13 定：摘要输入走 Codex 路线——不预裁剪、全量喂入；**移除既有 `content[:3000]`**（`compact.py:255`）；超窗靠 trim-retry 兜底——trim-retry 为**必需项**） ⑫ **摘要模板 A/B + 合体第三版**（2026-10-13 已定、本次补编号：六段 vs 四段对比；合体版 = 六段骨架 + Codex"用户约束/偏好"槽 + handoff framing + Hermes 式"已完成勿重做"声明）
- **Phase 3 可选激进（按需）**：⑨ 主动剪枝低水位路径（opt-in、默认关、rearm 跑道 + 回收量门；缓存契约按"有缓存"全抄）⑩ 微压缩式细水长流（`every_n_turns` 门控缓存成本）⑪ 长线：服务端压缩 / 无摘要换窗档

**5. 未决 / 待核实**

- 压缩事件 schema 与 UI 投影（富字段兼容）——**设计待评审**
- rearm/水位放事件流还是会话配置——**设计待评审**
- 移植后的回归用例：结构分派（两套消息形态）、投影应用、去重无损性、孤儿防护

> **D.16 结论**：改造可行，但顺序不可颠倒——**Phase 0（门控重写 + 触发重划 + 压缩事件化 + 接线）是所有移植的前置**；此后按"无损 → 廉价 → 权威化 → 激进"推进。分工上：Hermes 提供剪枝零件（去重/压力降级/信息性摘要/缓存契约），Codex 提供权威化与纠错（整份替换式事件载荷/无硬帽/trim-retry/旧摘要过滤）。2026-10-13 决策：Pass 3 不采纳、不建 per-tool 模板（`success` 已结构化可用）、缓存按"有"处理、弃用 70/90/95、摘要位置对齐 Hermes。绕过 Phase 0 的任何移植都会被每步重投影绕过。

---

*本文档为个人复盘记录，观点分为“已验证差距 / 权衡取舍 / 主观判断”三类，转载或对外引用前建议重新核对代码（行号可能随版本变化）。*
