# agent_memory_v1

这是 DB-GPT 外部的独立长期记忆第二批最小原型，实现“本次消息 → 同 scope 候选 → LangMem 整理 → 程序校验 → Mem0 写回”，并补充普通 search/get、替代关系写回和按 ID forget。DB-GPT 主链路已有默认关闭的可选桥接；独立 demo 和少量真实请求都不等于记忆质量评测。

## 已实现的链路

同一 `MemoryService` 实例的 `add()` / `write()` 在同一 scope 内用 `asyncio.Lock` 串行执行；多个实例不共享这把锁：

1. 用本次 `user` / `assistant` 消息拼写入查询；最近最多 10 条消息只给 manager，不重复进查询。
2. 调用 Mem0 2.2.1 的公开 `AsyncMemory.search(filters={"user_id": ...}, top_k=10, rerank=False)`。
3. 用 LangMem 0.0.30 的无状态 `MemoryManager.ainvoke()` 提取/整理。使用 `MemoryContent` 和上游 `Done` 工具；按本次来源 ID 生成调用专属 schema，让 Trustcall 校验/修复遗漏新来源的新增或 patch。未改动旧对象仍由框架回带；删除关闭。
4. 程序校验 scope、来源 ID/role/observed_at、候选 ID 和基本字段；整批通过后才写回。事实时间字段当前只做可空/非空字符串等基本检查，不能证明日期可解析、先后正确或事实时间有证据。
5. 按实际 Mem0 ID 和对象差异执行 `add(infer=False)` 或显式 `update()`。LangMem 临时 ID 不当作 Mem0 ID；新增摘要使用 Mem0 返回的真实 ID。
6. 模型通过临时 `supersedes_ids` 请求状态替代；程序先 ADD 新记录，再把真实新 ID 写入旧记录 `superseded_by`，有明确新生效时间时同时补旧 `valid_to`。ADD 失败不会提前改旧记录。
7. `search()` 返回正文、来源、事实时间、记录时间和 `superseded_by`；`get()` 只读取一个实际 ID；`forget()` 先整批核对 scope，再逐项报告 `deleted`、`missing` 或结果未知。
8. `build_answer_context()` 将候选 metadata 和调用方提供的 `reference_time` 规范化给回答模型；它不做额外检索、全局过滤或事实裁决。

Mem0 的 metadata 使用 `source_refs_json`、`valid_from`、`valid_to`、`recorded_at` 和 `superseded_by` 等 JSON/标量字段。第一批留下的 `related_memory_ids_json` 只在读取旧记录时兼容保留；新记录不写入。`recorded_at` 由程序保存，`valid_from` / `valid_to` 没有明确证据时为 `None`；普通整理更新会保留已有来源、替代关系和已有事实时间，模型省略可选时间字段不会把旧边界清空。密钥从环境或显式配置读取，代码和输出不包含密钥。

## 固定版本适配记录

- Mem0 2.2.1 的 `AsyncMemory.search()` 返回 `{"results": [...]}`，scope 必须放在 `filters={"user_id": ...}`；它没有公开 `bm25=False` 参数。`add(infer=False)` 对字符串逐条保存原文并返回结果列表，`update()` 返回成功消息而不是新 ID，所以更新沿用已核对的候选 Mem0 ID。
- Mem0 2.2.1 的 `get()` 将 `user_id` 等字段提升到结果顶层、其余 metadata 放在 `metadata` 中；适配层据此重新核对 `user_id`、`scope_type`、`scope_id` 和来源字段，不把搜索命中直接当成可信跨 scope 对象。
- LangMem 0.0.30 的 `max_steps` 是 `ainvoke()` 输入字段，不是 manager 构造参数；默认 `_prepare_messages()` 还会追加“错误/过时则更新”的通用指令。适配类去掉该追加语句，并为每次调用单独约束新来源；不修改共享 manager 的 schema，不重写 Trustcall 的 patch、校验或修复算法。
- Trustcall 在有旧对象且允许新增时绑定 `tool_choice="any"`。LangMem 默认到第二轮才加入 `Done`，本实现从第一轮就复用它，支持寒暄/无新信息时明确不变更；`Done` 由上游消耗，不存成记忆。`max_steps=1` 仍不保证只有一次 LLM 请求，schema 校验修复可能额外调用模型。
- LangMem 返回的 `ExtractedMemory.id` 是 manager 文档 ID；它与 Mem0 新增返回的 ID 分开处理。写回前会把 Pydantic/dict 结果统一成 `MemoryContent`，再按实际 Mem0 候选 ID和对象差异决定动作。

## 独立安装

不要修改 DB-GPT 根 `pyproject.toml` 或根 `uv.lock`。在本目录创建独立环境：

```bash
cd /Users/zsm/Documents/DB-GPT/examples/agent_memory_v1
uv venv --python 3.11 .venv
uv pip sync --python .venv/bin/python requirements.lock
```

主包固定为：

- `mem0ai==2.2.1`（源码锚点 `94c3fe9f238f3dbf29c9ce98643bd71eb13077cd`）
- `langmem==0.0.30`（源码锚点 `56d85939d80bb731bd5e237567148d817d7bfd16`）

`langchain-openai` 是显式的 DashScope OpenAI-compatible chat 客户端依赖；其余版本由本目录的 `requirements.lock` 固定。锁文件当前没有加入 `fastembed`、spaCy/NLP extras、BM25 专用扩展、图数据库或 reranker。Mem0 2.2.1 的公开 `search()` 签名没有 `bm25=False`，本实现不会伪造该参数；`rerank=False` 只关闭配置的 reranker。固定依赖环境不带 `fastembed` 和可用 spaCy 模型，因此 Qdrant provider 的关键词/实体增强通常降级为无信号，实际候选路径以运行时日志和 provider 状态为准。若后续安装这些可选组件，公开 `search()` 会按 Mem0 默认实现使用其现成关键词/实体信号；本目录没有另写一套检索融合。

## 配置与运行

模型和存储配置可由 `MemoryStoreConfig.from_env()` 读取，也可在 Python 中显式传入。当前默认值是 `deepseek-v4.1-flash`、`text-embedding-v3`、1024 维和 DashScope OpenAI-compatible 基础 URL；默认只说明连接起点，不包含密钥。

常用环境变量：

```bash
export DASHSCOPE_API_KEY='…'
export DASHSCOPE_BASE_URL='https://dashscope.aliyuncs.com/compatible-mode/v1'  # 可选
export AGENT_MEMORY_CHAT_MODEL='deepseek-v4.1-flash'                          # 可选
export AGENT_MEMORY_ENABLE_THINKING='1'                                        # 默认开启 DeepSeek 思考
export AGENT_MEMORY_EMBEDDING_MODEL='text-embedding-v3'                       # 可选
export AGENT_MEMORY_EMBEDDING_DIMS='1024'                                     # 可选
export AGENT_MEMORY_DATA_DIR='/absolute/path/to/agent_memory_v1/data'          # 可选
export AGENT_MEMORY_COLLECTION='agent_memory_v1'                               # 可选
```

无 key 时运行 demo 只打印非敏感配置提示，不初始化 Mem0，也不写数据：

```bash
.venv/bin/python -m memory_v1.demo
```

有 key 时 demo 仍默认禁写；审阅数据目录后显式设置 `AGENT_MEMORY_DEMO_WRITE=1` 才会写入示例 scope。调用方也可以直接使用：

```python
from datetime import datetime, timezone

from memory_v1 import MemoryScope, MemoryService, SourceMessage, build_answer_context

service = MemoryService.from_config()
summary = await service.add(
    scope=MemoryScope(scope_type="user", scope_id="user-123"),
    new_messages=[
        SourceMessage(
            id="conversation-7/message-2",
            role="user",
            content="我希望回答先给结论。",
            observed_at=datetime.now(timezone.utc),
        ),
    ],
    recent_messages=[],
)
print(summary.model_dump())

# 普通召回：旧记录也可能返回，由调用方结合 superseded_by 判断当前/历史语义。
records = await service.search(
    MemoryScope(scope_type="user", scope_id="user-123"),
    "我之前使用的数据库",
)

# 回答上下文：reference_time 由应用提供；旧记录保留在候选中，交给回答模型结合问题判断。
answer_context = build_answer_context(
    records,
    reference_time=datetime.now(timezone.utc),
)
# 将 answer_context 作为 JSON/结构化上下文交给现有回答模型。

# 遗忘入口接收实际 ID；用户侧应由 agent 先 search/get 解析自然语言目标。
forget_summary = await service.forget(
    MemoryScope(scope_type="user", scope_id="user-123"), [records[0].id]
) if records else None
if forget_summary:
    print(forget_summary.model_dump())

# 自然语言遗忘的接线：应用先把候选交给 agent，agent 只能从候选中选择真实 ID。
async def agent_forget(query: str):
    candidates = await service.search(
        MemoryScope(scope_type="user", scope_id="user-123"), query
    )
    selected_ids = [candidate.id for candidate in candidates if "回答先给结论" in candidate.content.content]
    return await service.forget(
        MemoryScope(scope_type="user", scope_id="user-123"), selected_ids
    )
```

Qdrant 使用 Mem0 自带 provider 的本地持久化，默认目录是本目录下的 `data/qdrant`，Mem0 操作历史默认是 `data/history.db`；均可配置为绝对路径。不要把这些运行数据目录当作 DB-GPT 的 Chroma 或元数据库，也不要在生产多进程场景直接复用本地模式。

当前默认使用 `deepseek-v4.1-flash`。阿里百炼官方说明该模型支持 Function Calling 和结构化输出，因此本原型默认保留思考模式（`AGENT_MEMORY_ENABLE_THINKING=1`）。如果某个部署端点对思考模式下的工具调用不兼容，再通过环境变量显式关闭；这不影响 embedding 配置。

## 当前未启用与边界

本原型自身没有加入 fastembed、spaCy/NLP extras、BM25/中文分词、实体 store、图数据库、reranker、意图分类、完整时间线推理或检索融合；DB-GPT 主链路的可选桥接见下文。跨进程锁、持久队列、整批事务、模型/API 兼容性、整体记忆质量和回答模型的泛化效果仍未由本原型证明；第一轮真实写回与带 `reference_time` 的回答侧定向观察已完成。`related_memory_ids_json` 仅兼容读取，不参与新 schema 的替代判断。

`batch_id` 当前只用于追踪，不是幂等键；相同批次再次调用仍会重新检索/提取。写入异常携带已返回的实际 ID，但事件列表仍含计划动作，不具备自动恢复能力。

### 第二批实现说明（2026-10-09）

最新约定见 [v1 实施说明 17](../../docs/agent_long_term_memory_v1_design.md#实施说明-17第二批增量与编码约定2026-10-09)。本 README 上面的字段和调用是第二批实际行为；固定版本 SDK 已完成第一轮真实模型/embedding 观察，整体效果仍未验证。

1. 正文自包含，不附加“本条替代某记忆”等管理说明。模型以临时 `supersedes_ids` 提出明确替代的旧 ID；它不落库，不套用旧正文去重转 update/skip。程序先规划整批、ADD 新记录取得真实 ID，再一次写旧 `superseded_by` 和有依据的结束日期；旧记录保留原来源，新来源只属于新记录。ADD 失败时不提前修改相关旧记录。
2. 已确认状态替代且有可靠切换日期时，允许补旧 `valid_to`、新 `valid_from`。日期未知时仍可保存明确替代关系，不能照抄消息/入库时间，不用 Mem0 `expiration_date` 隐藏历史。来源可能仅支持结束日期，不能取最大来源时间当作旧正文的确认时间。计划、含糊矛盾、不同情境、较晚导入的历史事实不自动替代。旧通用关联键保留但忽略，新写入停用，不批量清空历史数据。
3. 补同 scope 的 `search/get`，返回正文、来源、消息/事实时间和替代标记；旧记录仍可召回，agent 可按需读取替代目标。目标缺失须可见，不递归构建完整历史链。
4. 补 scope 校验的 `forget` 及可运行的 agent 工具示例：用户自然语言提出请求，agent 查到实际 ID 后调用删除。一次 Top-k 不保证找全主题，SDK delete 不保证清除 history/聊天/备份；删除替代目标不能自动重新激活旧记录。
5. 当前已用 fake/stub 验收字段/ID 映射、兼容第一批 metadata、替代边界、召回上下文、删除范围，以及已完成/未尝试/结果未知的部分失败摘要；真实模型/embedding 写回和带 `reference_time` 的回答侧定向观察也已完成。第二批不要求新增批次缓存或自动重试器，已知新 ID 后定向核对/补写，不能重跑提取来补关系。

### 多类型失败修正与复跑（2026-10-09）

本轮修正了单轮整理的无变更工具、遗漏新来源的 schema 修复、重复 UPDATE/ADD 的确定性合并，以及替代与普通 patch 同时出现时的旧记录保留。写入时，已被替代的旧记录只作为上下文，不开放给 manager patch；普通召回仍可返回它们。没有增加持久化字段、图数据库或检索分类器。

24 项无外部 API 回归检查：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

手动真实模型复跑（会调用配置的模型/embedding；每次创建新的临时 Qdrant，不使用现有数据目录）：

```bash
.venv/bin/python tests/observe_real_model.py --profile ~/.dbgpt/configs/qwen.toml
# 或不传 --profile，使用前述环境变量。
# 只重复关键样本：
.venv/bin/python tests/observe_real_model.py --profile ~/.dbgpt/configs/qwen.toml \
  --cases C_provenance_plan D_correction E_ambiguous_context F_multitopic_reference \
  --targeted-repeat
```

脚本打印实际结果目录，可用 `--output` 指定仓库外尚不存在的目录，或用 `--reference-time` 固定回答参考时间。输入保存在 `tests/fixtures/multi_type.json`；本轮输入、manager 输出、写回快照、候选和回答见 `tests/observations/20261009_fix.json`。它们都是合成样本，不是生产数据。

此前主跑与关键复跑共 35 次写入、34 次回答，均未出现 API/写入错误；24 项回归和 385 项来源字段一致性检查通过。随后提升“明确历史回填必须 ADD”的规则并重新跑完整六组：22 次写入、23 次回答均完成，2025 年 MySQL 被单独保存，普通补充仍使用 UPDATE。**这些都不是 100% 记忆准确率。** 当前状态更新可以接受，但“当时系统知道什么”、来源逐句归属、日期精度和回答中的无依据解释仍需继续验证。完整结果见 [原型评估](../../docs/agent_long_term_memory_prototype_eval.md#多类型观察后的最小修正与复跑2026-10-09) 和 [历史回填规则复跑记录](tests/observations/20261010_historical_rule.json)。

2026-10-10 又用 7 组措辞变体（历史回填、偏好补充、状态变化）完成 14 次写入和 12 次回答：历史回填均独立 ADD，偏好补充均 UPDATE，状态变化均保留新旧记录；无 API/写入/回答异常。这仍是合成样本观察，不是质量基准。摘要见 [措辞变体复跑记录](tests/observations/20261010_variants.json)，输入见 [变体 fixture](tests/fixtures/memory_variants.json)。

随后进行风险定向观察：assistant/工具来源归属基本正确，但“现在”仍可能被写成事实 `valid_from`，后来澄清的信息也不能回答审计型“当时知道什么”。这两项暂列为 v1 已知限制，不作为当前修正目标；结果见 [风险观察记录](tests/observations/20261010_risk_observation.json)，输入见 [风险 fixture](tests/fixtures/memory_risks.json)。

### DB-GPT 主链路的可选接入

第一版桥接已接到 `packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py` 的 ReAct 主链路，但默认关闭。开启方式：

```bash
export DBGPT_AGENT_MEMORY_ENABLED=1
export DBGPT_AGENT_MEMORY_ROOT='/absolute/path/to/DB-GPT/examples/agent_memory_v1'  # 可选
```

桥接在回答前用当前用户消息调用 `MemoryService.search()`，把候选正文、来源、`observed_at`、事实时间和 `superseded_by` 作为系统提示上下文交给当前 agent；回答成功后用本轮 user + assistant 两条 `SourceMessage` 调用 `MemoryService.add()`。历史对话最多取事件记录中的十条 user/assistant 消息作为整理上下文。tool 结果不自动传入，也不参与写入查询；以后如需纳入，必须由调用方显式扩展。

Mem0/LangMem 仍在本目录的独立环境中加载，DB-GPT 根依赖和根 `uv.lock` 没有被修改。未设置开关、依赖不可用或记忆服务失败时，主链路继续正常回答，并在日志中记录失败。接入复核还覆盖了 DB-GPT 的消息边界：代理层会保留 `tools`/`tool_choice`，OpenAI 风格的 `user` 消息会归一化为 DB-GPT 内部 `human` 消息，避免模型收到只有系统提示的请求。修复后的真实请求能正确看到当前用户；写入“偏好使用 PostgreSQL”后，下一次请求能从 Qdrant 召回该记忆并回答“PostgreSQL”。DB-GPT 会用认证用户 `001` 覆盖请求体中的 `user_name`，因此端到端检查必须按实际认证 scope 查询。

随后在同一认证范围完成“写入 PostgreSQL 偏好→查询→切换到 MySQL→查询当前偏好”四个真实请求，回答依次为“收到”“PostgreSQL”“收到”“MySQL”；最终保留两条记录，旧 PostgreSQL 记录指向新 MySQL 记录。提示词新增的纯查询保护也已验证：查询轮次没有把问题和 assistant 答案追加到记忆正文。详情见 [DB-GPT 真实桥接观察](tests/observations/20261010_dbgpt_e2e.json)。

第二轮还观察了历史事实、明确日期、历史补充、重复确认和未确定计划。发现“2025 年曾经使用过 Oracle”不能展开成全年；已增加年份精度约束，复跑保存为 `valid_from=valid_to=2025`，回答只保留“2025 年曾经使用过 Oracle”。详情见 [第二轮观察](tests/observations/20261010_dbgpt_e2e_round2.json)。

这只证明一个真实认证范围的写入→召回→回答闭环和失败隔离，不代表已经完成生产部署、跨进程一致性或整体记忆质量验收；当前观察环境仍只使用语义检索。实现完成后的下一步是正式评测整体记忆效果和对后续任务的帮助，再决定是否修改实现或进入部署准备。

### LongMemEval-S 评测接线

正式评测适配器位于 [`evaluation/longmemeval.py`](evaluation/longmemeval.py)，契约和执行清单见 [`evaluation/PLAN.md`](evaluation/PLAN.md)。它按题目隔离 scope、按 session 顺序回放 history，并输出官方 scorer 可读的 `predictions.jsonl` 以及包含候选来源的 `records.jsonl`。先用 `--dry-run` 检查数据，再运行小规模 API pilot；该 runner 不复制或修改官方评分器，也不会把问题和 gold answer 写入记忆。
