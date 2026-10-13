"""Prompt and input-boundary helpers for the first memory write path."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Sequence

from langchain_core.messages import HumanMessage

from .schema import ExistingMemory, SourceMessage, SourceRef


MEMORY_INSTRUCTIONS = """你负责从一段对话中形成对未来任务有用的长期记忆。

工具用法：
- MemoryContent：新增一个对象。每项可独立更新的信息分别调用一次；一次响应可以包含多个工具调用。
  不要把整个输入或不同属性的事实压成一个大摘要。例如职业和回答偏好分开，生产主库和运行时约束分开。
- PatchDoc：对一个既有对象做非矛盾补充或追加来源，json_doc_id 必须是其真实旧 ID。
  同一旧 ID 最多调用一次 PatchDoc，正文和 source_ids 的所有变更放进同一次 patches，不能分开调用。
- Done：本次没有值得保存或更新的信息时调用。不要为了调用工具而重写、补造或提取旧上下文。
  未修改的旧对象由 LangMem 自动回带，不需要任何工具调用。不删除记忆。

关系选择的最高优先级规则：
- 明确的状态变化、明确纠正、独立历史事实和历史回填都必须调用 MemoryContent 新增，不能用 PatchDoc
  改写当前记录。即使它们谈的是同一个主体或属性，即使新消息使用“补充/补录”这个词，也不能因此变成 UPDATE。
- 只有新消息在完善同一条仍然成立的事实时才使用 PatchDoc。主题相同、内容不矛盾，不能单独证明是同一条事实的补充。
- 例如已有“Atlas 生产主库自 2026 年 10 月 1 日起使用 PostgreSQL”，新消息是“补录旧记录：2025 年 6 月至
  12 月生产主库曾使用 MySQL，那是过去的情况”：必须新增独立的 MySQL 历史记忆；不能 patch PostgreSQL 记录，
  也不能把 MySQL 历史、PostgreSQL 当前状态和未来计划合并成一条摘要。用户说“补充”不等于 UPDATE。

输入分为四个明确区域：New Messages 是本次新输入，Recent Context 是最多十条的旧对话上下文，
Existing Memories 是本次范围内检索到的有限旧候选，Source Metadata 是程序提供的真实来源目录。

提取规则：
- 只保存值得未来使用的明确事实、偏好、约束、决定、计划、任务进展或可复用经验；无价值内容不写。
- 普通寒暄、感谢、一次性的随口感受不形成长期记忆。新的偏好、约束、决定、可复用任务结果要保留，
  不能因为已经有旧对象就略过新信息；tool 的已完成检查结果可以独立形成记忆。
- 纯查询、回顾或确认已有记忆的问题本身不产生新事实；如果 New Messages 只是询问“现在是什么/之前是什么”、
  要求重复确认，或 assistant 仅回答“收到/是/否”，调用 Done，不要把问题、答案或确认语句追加到已有记忆正文。
  如果同一条 user 消息同时包含新事实和问题，只提取其中有长期价值且由该 user 消息直接支持的事实，不保存问题和 assistant 的回答。
- 新事实必须由 New Messages 支持。Recent Context 和 Existing Memories 只用于理解、去重、关联，
  不能因为旧候选原样出现就重新生成一批旧记忆。
- 严格区分 user、assistant、tool、system。assistant 的建议、推测或复述不能自动写成用户已确认的事实；
  tool 观察也按 tool 来源表达；不要保存隐藏推理。
- 保留“计划”“可能”“尚未确认”等原意，不把没有依据的推断改写成确定事实。
- 正文必须保留影响理解的限定：谁提出建议、是否确认、先前说法是否错误、信息是否曾经未知。
  后续重复确认或补充时也保留这些限定，不能把更正或澄清记录压缩成仅有当前值的句子。

整理和写回规则：
- 已有相同信息不新增、不为改写措辞而更新；若确有新的来源或观察时间值得保留，可 patch 原对象的 source_ids。
- 新信息对旧记忆作非矛盾补充时，patch 对应旧对象，保留原内容和原有 source_ids，并加入新来源。
  更新是补充旧正文，不能只用新消息重新概括后覆盖它。旧正文中的独立事实、已完成检查及限定都要保留；
  新消息未再次提及不表示它们应被删掉。先前未知/未确认与后续确认并列表达，可用各来源消息日期说明先后。
- 同一属性的事实未改变，新增候选方案、建议或确认细节时优先用 PatchDoc 补充原对象，supersedes_ids 为空；
  在正文中区分当前事实与未确定方案。独立值得保存的事件或建议也可单独新增，但不替代仍然成立的事实。
- 同一旧对象在本次整理中只选择一种处理：非矛盾补充用 PatchDoc；状态变化/明确纠正用 MemoryContent 新增，
  绝对不要用 PatchDoc 把旧值改成新值，也不要 patch 旧对象的 supersedes_ids。
- 新增的替代对象在临时 supersedes_ids 中引用被替代的旧 Mem0 ID，旧正文和旧来源保持原样。
  该字段只供程序完成 ADD 后写旧记录 superseded_by；不要把 ID 写进 content。
- 替代仅用于同一主体、同一属性、同一情境的状态真正变化，或明确纠正旧说法。方案确认不会替代已发生的
  工具检查事件；补充建议、计划、不同环境或过去事件都不自动替代当前事实。
  历史回填和独立事件用 MemoryContent 新增，保留各自时间，不把当前记录改写成整个历史摘要。
- Source Metadata 中 editable_memory_ids 是可 patch/替代的目标；已有 superseded_by 的对象仅为只读历史上下文，
  不调用工具修改它，不引用它作为替代目标；只引用本次可编辑的当前对象。
- 明确纠正时，新正文必须直接说明“先前哪项说法错误，以及更正后的事实”，不要仅写新值。
  旧对象只能表示先前说法，不能当作过去成立的事实；没有事实切换时间时不填事实时间边界。
  状态变化不表示旧说法错误：“迁移/切换/改用”不能写成“以前说错了/被更正”。
- 未知信息后来被澄清时用 PatchDoc 补充更新，正文必须简要保留“此前未知/未确认，后来确认”的含义。
  可用来源消息时间说明何时获知，不把后来确认的知识归到较早的消息时刻。
- 历史回填是例外：如果新消息明确说的是过去某个时间段、旧记录或曾经状态，即使它补充了同一属性，
  也必须新增历史记忆；不要把它 patch 到当前状态记录。只有历史记忆自身的来源和日期能被当前输入补充时，
  才可以更新那条历史记忆。
- 原因不明的矛盾保留两边及其来源，不只凭写入先后裁定谁正确。
- 无关或未改变的旧对象无需调用工具；框架原样回带不是新增。
- supersedes_ids 只能引用 editable_memory_ids 中真实存在的旧 Mem0 ID；它是临时请求字段，不落库。

来源和时间规则：
- source_ids 只能引用 Source Metadata 中出现的真实 ID；每个来源的 role 和 observed_at 必须按目录使用，不能编造。
- 新对象至少引用一条 New Messages 的 source_id；更新对象也必须有本次新输入的依据，并保留旧来源。
  PatchDoc 更改正文时，必须在同一次 patches 中追加支持该变更的新 source_id。只给确实支持该对象的消息追加来源，
  不给无关记忆批量追加本轮来源。同一新信息补充最直接相关的一个旧对象，不复制进多个不同主题/情境的旧对象。
- observed_at 是消息被说出/观察到的时间，不是事实必然生效时间。valid_from/valid_to 只有新消息明确支持时才填写，
  否则必须是 null；未知时间留空，不把 assistant 的当前日期或 Mem0 入库时间当作事实时间。
- “现在/目前/当前”只说明状态在该消息的 observed_at 时被观察到，不说明状态从该时刻开始或在该时刻结束；
  仅凭这些词时 valid_from 和 valid_to 都必须为 null，即使新状态替代了旧状态。像“从 10 月 1 日起”“上周”“下个月”
  这样的相对或明确日期，只有在 reference/observed_at 足以换算且原文确实表达了事实或计划时间时才填写，并保留计划语义和日期精度。
- 只说“2025 年曾经/用过/发生过”只证明该年某个未确定时段有记录，不等于全年持续；不要自动展开为
  `2025-01-01` 至 `2025-12-31`。若能保留年份精度就只写 `2025`（或在正文中保留“2025 年”并将边界留空），不能把未知的月日补出来。
- source_ids 和 supersedes_ids 去重；不要生成第二套记忆 ID，也不要把 LangMem 的内部对象 ID写入 content。

工具选择示例（仅说明形式；执行时使用本次输入里的真实 ID 和来源，不复制示例 ID）：
1. 旧对象 A 是“服务使用 Python 3.10”，新消息 m 是“已升级到 Python 3.12”：
   调用 MemoryContent(content="服务已升级到 Python 3.12", source_ids=[m], supersedes_ids=[A])。
   不调用 PatchDoc；旧 3.10 正文保持原样。新正文不写成“先前 3.10 是错误”。
2. 旧对象 A 是“用户负责数据平台”，新消息 m 是“还负责 ETL 监控”：
   只调用一次 PatchDoc(json_doc_id=A)，在 patches 中同时补充正文、追加 m；不调用 MemoryContent 新增同义概括。
3. 旧对象 A 是“联系方式 a@example.com”，新消息 m 是“刚才写错了，正确的是 b@example.com”：
   调用 MemoryContent(content="用户联系方式是 b@example.com；先前 a@example.com 是笔误", source_ids=[m], supersedes_ids=[A])。
   不调用 PatchDoc，不把笔误描述成真实联系方式变化。
4. 消息 m 是“暂存环境的检查通过，但方案还没定”：检查结果单独保存，不替代或修改待确认的生产方案。
5. 新消息只有“谢谢”：调用 Done；不从 Recent Context 补提旧事实。
6. 旧对象 A 是“费用约 200，币种未知”，新消息 m 是“查清是美元”：用一次 PatchDoc 补充为
   “费用约 200 美元；此前币种未知，后来查清为美元”，同时追加 m。不能只留下“费用 200 美元”，
   否则会丢掉先前为何无法采用此信息的依据。
7. PatchDoc 的完整形式示意：旧正文是“9月5日检查通过，耗时2秒；方案尚未批准”，9月8日用户批准试用：
   patches=[
     {"op":"replace","path":"/content","value":"9月5日检查通过，耗时2秒；当时方案尚未批准。9月8日用户批准试用。"},
     {"op":"add","path":"/source_ids/-","value":m}
   ]。不能把旧检查正文替换成仅有“用户批准试用”，不能说9月5日的检查证明用户已批准。

一次业务整理循环只使用 max_steps=1。"""


@dataclass(frozen=True)
class InputPartitions:
    """Validated partitions passed to the prompt builder."""

    new_messages: tuple[SourceMessage, ...]
    recent_messages: tuple[SourceMessage, ...]


def _as_message(value: SourceMessage | dict) -> SourceMessage:
    return value if isinstance(value, SourceMessage) else SourceMessage.model_validate(value)


def _dedupe_messages(messages: Iterable[SourceMessage], label: str) -> list[SourceMessage]:
    by_id: dict[str, SourceMessage] = {}
    for message in messages:
        previous = by_id.get(message.id)
        if previous is not None:
            if previous != message:
                raise ValueError(
                    f"{label} contains conflicting messages for source id {message.id!r}"
                )
            continue
        by_id[message.id] = message
    return list(by_id.values())


def prepare_partitions(
    new_messages: Sequence[SourceMessage | dict],
    recent_messages: Sequence[SourceMessage | dict] | None = None,
    *,
    recent_limit: int = 10,
) -> InputPartitions:
    """Validate message identity and keep only the latest ten recent messages."""

    if not new_messages:
        raise ValueError("new_messages must contain at least one message")
    if recent_limit < 1:
        raise ValueError("recent_limit must be positive")

    new = _dedupe_messages((_as_message(item) for item in new_messages), "new_messages")
    recent_all = _dedupe_messages(
        (_as_message(item) for item in (recent_messages or ())), "recent_messages"
    )
    recent = recent_all[-recent_limit:]

    new_ids = {message.id for message in new}
    recent_ids = {message.id for message in recent}
    overlap = sorted(new_ids & recent_ids)
    if overlap:
        raise ValueError(
            "new_messages must not be repeated in recent_messages: " + ", ".join(overlap)
        )
    return InputPartitions(tuple(new), tuple(recent))


def build_write_query(
    new_messages: Sequence[SourceMessage], *, include_tool: bool = False
) -> str:
    """Build the Mem0 search query from only the current input messages."""

    allowed_roles = {"user", "assistant"}
    if include_tool:
        allowed_roles.add("tool")
    lines = [
        f"{message.role}: {message.content}"
        for message in new_messages
        if message.role in allowed_roles
    ]
    if not lines:
        raise ValueError(
            "new_messages must contain a user/assistant message for the write query; "
            "tool messages require include_tool=True"
        )
    return "\n".join(lines)


def build_source_catalog(
    partitions: InputPartitions, existing: Sequence[ExistingMemory]
) -> dict[str, SourceRef]:
    """Create the source directory and reject conflicting source IDs."""

    catalog: dict[str, SourceRef] = {}

    def add(source: SourceRef) -> None:
        previous = catalog.get(source.id)
        if previous is not None and previous != source:
            raise ValueError(
                f"conflicting source metadata for id {source.id!r}: "
                "the same ID cannot identify different roles or observed times"
            )
        catalog[source.id] = source

    for message in (*partitions.new_messages, *partitions.recent_messages):
        add(message.to_source_ref())
    for memory in existing:
        for source in memory.source_refs:
            add(source)
    return catalog


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def build_manager_message(
    *,
    batch_id: str,
    partitions: InputPartitions,
    scope: dict[str, str],
    existing: Sequence[ExistingMemory],
) -> HumanMessage:
    """Build one human message containing explicit, source-aware input sections."""

    existing_source_metadata = [
        {
            "memory_id": memory.id,
            "content": memory.content.model_dump(mode="json"),
            "source_refs": [source.model_dump(mode="json") for source in memory.source_refs],
            "recorded_at": memory.recorded_at.isoformat() if memory.recorded_at else None,
            "superseded_by": memory.superseded_by,
        }
        for memory in existing
    ]
    source_metadata = {
        "scope": scope,
        "editable_memory_ids": [memory.id for memory in existing if not memory.superseded_by],
        "read_only_memory_ids": [memory.id for memory in existing if memory.superseded_by],
        "new_message_sources": [
            message.to_source_ref().model_dump(mode="json")
            for message in partitions.new_messages
        ],
        "recent_message_sources": [
            message.to_source_ref().model_dump(mode="json")
            for message in partitions.recent_messages
        ],
        "existing_memory_sources": existing_source_metadata,
    }
    text = "\n".join(
        (
            f"<batch_id>{batch_id}</batch_id>",
            "<new_messages>",
            _json([message.model_dump(mode="json") for message in partitions.new_messages]),
            "</new_messages>",
            "<recent_context>",
            _json([message.model_dump(mode="json") for message in partitions.recent_messages]),
            "</recent_context>",
            "<source_metadata>",
            _json(source_metadata),
            "</source_metadata>",
        )
    )
    return HumanMessage(content=text)
