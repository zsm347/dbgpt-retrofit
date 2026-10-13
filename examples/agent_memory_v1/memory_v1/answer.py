"""Thin answer-side context preparation for the memory prototype.

The memory service deliberately stops at structured retrieval.  This module
does not rank, filter, or decide which candidate is true; it only gives the
answer model the candidate metadata and an application-owned reference time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Sequence

from .schema import ExistingMemory


ANSWER_INSTRUCTIONS = """你正在使用长期记忆候选回答用户问题。
reference_time 是应用提供的当前参考时刻，不是事实发生时间。
候选集合不是完整历史；不要因为没有命中就断言事情从未发生。
superseded_by 非空的记录已被另一条记录替代，不能单独作为当前状态；它仍可能回答合适的历史问题。
替代关系本身不能证明旧说法曾经成立。正文说明旧说法错误或被更正时，不把新旧值解释成真实状态变化。
正文说明信息此前未知、后来才确认时，保留这种知识变化，不把后来确认的结论归到较早的来源消息。
valid_from/valid_to 是有依据的事实时间，observed_at 是来源消息被观察到的时间，recorded_at 是系统保存时间，三者不能混用。
只有日期/月等精度时，不杜撰午夜或更精确的时刻；边界是否包含当天须依据原文，字段名本身不决定包含性。
只说“某年曾经/用过/发生过”不等于全年持续；不能把年份级事实回答成“该年全年”，也不能补出未提供的月日。
无事实时间边界时，消息时间只支持当时的报告，不能证明状态在整个间隔内一直持续或某次变更必然发生。
结合用户问题判断当前、历史、变化、来源或其他任务意图；不要把向量分数、插入顺序或较新的 recorded_at 当作事实真伪依据。
用户的偏好和约束按原意回答，不增加没有来源支持的新要求；原因、批准条件和阈值也不能自行补全。
多个 source_refs 只表示该记忆的来源集合，不表示每条消息都确认了正文的每个部分。
直接、简洁地回答所问；除非用户询问依据，不展示逐条候选分析、内部字段或额外方案。
没有足够时间或来源依据时，明确说明不确定性，不补造日期。"""


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value


def _iso_utc(value: datetime) -> str:
    """Render an aware timestamp consistently with the stored source JSON."""

    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _answer_record(memory: ExistingMemory) -> dict[str, Any]:
    """Serialize one retrieved memory without exposing manager-internal IDs."""

    return {
        "id": memory.id,
        "content": memory.content.content,
        "source_refs": [ref.model_dump(mode="json") for ref in memory.source_refs],
        "valid_from": memory.content.valid_from,
        "valid_to": memory.content.valid_to,
        "superseded_by": memory.superseded_by,
        "recorded_at": _iso_utc(memory.recorded_at) if memory.recorded_at else None,
    }


def build_answer_context(
    records: Sequence[ExistingMemory], *, reference_time: datetime
) -> dict[str, Any]:
    """Build the minimal structured context supplied to an answer model.

    ``reference_time`` is required so a caller cannot silently ask the model
    to interpret "current" without an application-owned as-of time.  Records
    are kept in the search result order and are not filtered here: an old
    record may be needed for a historical question.
    """

    reference_time = _require_aware(reference_time, "reference_time")
    return {
        "reference_time": _iso_utc(reference_time),
        "instructions": ANSWER_INSTRUCTIONS,
        "memory_candidates": [_answer_record(memory) for memory in records],
    }


def build_answer_prompt(
    question: str, records: Sequence[ExistingMemory], *, reference_time: datetime
) -> str:
    """Render a stable prompt payload for an existing answer model call."""

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must not be empty")
    context = build_answer_context(records, reference_time=reference_time)
    return "\n".join(
        (
            ANSWER_INSTRUCTIONS,
            f"<reference_time>{context['reference_time']}</reference_time>",
            f"<user_question>{question.strip()}</user_question>",
            "<memory_candidates>",
            json.dumps(context["memory_candidates"], ensure_ascii=False, indent=2),
            "</memory_candidates>",
        )
    )
