"""Pure projection helpers for rebuilding model history from rollout events.

This module deliberately has no storage or execution responsibilities. Callers
must provide committed events in ``event_seq`` order. Synthetic tool results
exist only in the returned prompt projection; they must never be persisted as
authoritative rollout events or used to re-run a tool.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Tuple

from dbgpt.core.interface.message import ModelMessage, ModelMessageRoleType

_SUPPORTED_EVENT_TYPES = {
    "turn_started",
    "user_message",
    "assistant_message",
    "tool_result",
    "context_compacted",
    "turn_completed",
    "turn_aborted",
}
_LIFECYCLE_EVENT_TYPES = {"turn_started", "turn_completed", "turn_aborted"}
_SYNTHETIC_TOOL_RESULT = "aborted"
# Roles allowed inside a context_compacted replacement_history. System is
# deliberately excluded: it is injected separately by the request assembly
# layer, and a system entry here would produce a duplicate system prefix.
_REPLACEMENT_ROLES = frozenset(
    {
        ModelMessageRoleType.HUMAN,
        ModelMessageRoleType.AI,
        ModelMessageRoleType.TOOL,
    }
)


@dataclass(frozen=True)
class CompactionState:
    """Audit fields of the most recent ``context_compacted`` event.

    ``rearm_tokens`` is the watermark below which future compaction is
    suppressed; ``through_event_seq`` is the event sequence the replacement
    already reflected when it was written; ``accounting`` carries the
    estimated token deltas of that compaction.
    """

    rearm_tokens: int
    through_event_seq: int
    accounting: Mapping[str, Any]


@dataclass(frozen=True)
class RolloutEvent:
    """Storage-independent event input for the rollout history projector.

    ``payload`` follows the v1 logical event contract. This is intentionally
    not a persistence model and contains no database IDs or schema machinery.
    ``event_seq`` is optional commit-order metadata; it is only used to expose
    the latest observed sequence on the projection.
    """

    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_seq: Optional[int] = None


@dataclass(frozen=True)
class ChatCompletionHistoryProjection:
    """Model messages plus IDs of prompt-only synthetic tool results.

    ``latest_event_seq`` is the last commit-order sequence observed across the
    projected events (``None`` when the input carries no sequences).
    ``last_compaction`` describes the most recent ``context_compacted`` event
    (``None`` when the stream has never been compacted).
    """

    messages: Tuple[ModelMessage, ...]
    synthetic_tool_call_ids: frozenset[str]
    latest_event_seq: Optional[int] = None
    last_compaction: Optional[CompactionState] = None


def _replacement_from_payload(
    payload: Mapping[str, Any],
) -> Tuple[list[ModelMessage], list[str]]:
    """Validate and convert a ``context_compacted`` replacement history.

    Returns the projected messages plus the ids of assistant tool calls that
    the replacement leaves open (no matching tool result). Those calls were
    synthesized as ``aborted`` in the pre-compaction projection; they stay
    open here so later real results can still resolve them and boundary
    flushes re-synthesize placeholders deterministically.
    """

    replacement = payload.get("replacement_history")
    if not isinstance(replacement, list) or not replacement:
        raise ValueError(
            "context_compacted 'replacement_history' must be a non-empty list"
        )

    messages: list[ModelMessage] = []
    call_order: list[str] = []
    tool_call_ids: set[str] = set()
    resolved_ids: set[str] = set()

    for entry in replacement:
        if not isinstance(entry, Mapping):
            raise ValueError("each replacement_history entry must be an object")
        role = entry.get("role")
        if role not in _REPLACEMENT_ROLES:
            raise ValueError(
                "replacement_history entries must use role human/ai/tool"
            )
        if not isinstance(entry.get("content"), str):
            raise ValueError(
                "replacement_history entries must contain string 'content'"
            )

        if role == ModelMessageRoleType.HUMAN:
            messages.append(
                ModelMessage(
                    role=ModelMessageRoleType.HUMAN,
                    content=deepcopy(entry["content"]),
                )
            )
            continue

        if role == ModelMessageRoleType.AI:
            tool_calls = deepcopy(entry.get("tool_calls") or [])
            if not isinstance(tool_calls, list):
                raise ValueError("replacement_history 'tool_calls' must be a list")
            entry_ids: set[str] = set()
            for tool_call in tool_calls:
                if not isinstance(tool_call, Mapping):
                    raise ValueError(
                        "each replacement tool_call must be an object"
                    )
                call_id = tool_call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError(
                        "each replacement tool_call must have a non-empty 'id'"
                    )
                if call_id in entry_ids:
                    raise ValueError(
                        f"duplicate tool_call_id in replacement: {call_id}"
                    )
                entry_ids.add(call_id)
                tool_call_ids.add(call_id)
                call_order.append(call_id)
            messages.append(
                ModelMessage(
                    role=ModelMessageRoleType.AI,
                    content=deepcopy(entry["content"]),
                    tool_calls=tool_calls or None,
                )
            )
            continue

        # role == tool
        call_id = entry.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError(
                "replacement_history tool entries need a non-empty 'tool_call_id'"
            )
        if call_id not in tool_call_ids:
            raise ValueError(
                f"replacement_history tool result has no matching tool_call: {call_id}"
            )
        resolved_ids.add(call_id)
        messages.append(
            ModelMessage(
                role=ModelMessageRoleType.TOOL,
                content=deepcopy(entry["content"]),
                tool_call_id=call_id,
            )
        )

    open_calls = [call_id for call_id in call_order if call_id not in resolved_ids]
    return messages, open_calls


def _compaction_state_from_payload(payload: Mapping[str, Any]) -> CompactionState:
    """Extract the audit fields a compaction event exposes on the projection."""

    rearm_tokens = payload.get("rearm_tokens")
    if not isinstance(rearm_tokens, int) or rearm_tokens < 0:
        raise ValueError(
            "context_compacted 'rearm_tokens' must be a non-negative integer"
        )
    covers = payload.get("covers")
    if not isinstance(covers, Mapping) or not isinstance(
        covers.get("through_event_seq"), int
    ):
        raise ValueError(
            "context_compacted 'covers.through_event_seq' must be an integer"
        )
    accounting = payload.get("accounting")
    if not isinstance(accounting, Mapping):
        raise ValueError("context_compacted 'accounting' must be an object")
    return CompactionState(
        rearm_tokens=rearm_tokens,
        through_event_seq=covers["through_event_seq"],
        accounting=deepcopy(dict(accounting)),
    )


def project_rollout_events(
    events: Iterable[RolloutEvent],
) -> ChatCompletionHistoryProjection:
    """Project ordered rollout events into DB-GPT ``ModelMessage`` history.

    Lifecycle markers are not model messages. Assistant tool calls are kept
    intact, including their provider IDs and argument strings. If the stream
    reaches another message/lifecycle boundary while a call has no real tool
    result, a matching ``role=tool`` message with content ``"aborted"`` is
    appended to this in-memory projection. Real tool results remain in event
    order; missing results are added deterministically in original call order.

    A ``context_compacted`` event replaces everything accumulated so far with
    its ``replacement_history`` payload (the latest one wins). Malformed
    protocol history raises ``ValueError`` instead of silently dropping or
    mis-pairing messages. The function never mutates its inputs.
    """

    projected: list[ModelMessage] = []
    synthetic_ids: set[str] = set()
    latest_event_seq: Optional[int] = None
    last_compaction: Optional[CompactionState] = None
    # Keep insertion order so unresolved calls are synthesized in the same
    # deterministic order as the assistant's original tool_calls array.
    pending_calls: dict[str, None] = {}

    def flush_unresolved_calls() -> None:
        for call_id in pending_calls:
            projected.append(
                ModelMessage(
                    role=ModelMessageRoleType.TOOL,
                    content=_SYNTHETIC_TOOL_RESULT,
                    tool_call_id=call_id,
                )
            )
            synthetic_ids.add(call_id)
        pending_calls.clear()

    for event in events:
        event_type = event.event_type
        if event_type not in _SUPPORTED_EVENT_TYPES:
            raise ValueError(f"Unsupported rollout event type: {event_type!r}")
        if event.event_seq is not None:
            latest_event_seq = event.event_seq

        # A tool result can resolve the current assistant call group. Any
        # other event marks the end of that group's result window.
        if pending_calls and event_type != "tool_result":
            flush_unresolved_calls()

        if event_type in _LIFECYCLE_EVENT_TYPES:
            continue

        payload = event.payload
        if event_type == "context_compacted":
            replacement_messages, open_calls = _replacement_from_payload(payload)
            compaction_state = _compaction_state_from_payload(payload)
            # Whole-history replacement: discard everything projected so far.
            # Synthetic ids were computed against the replaced view; pending
            # calls are re-derived from open calls in the replacement so later
            # real results still resolve them.
            projected = replacement_messages
            synthetic_ids.clear()
            pending_calls.clear()
            pending_calls.update((call_id, None) for call_id in open_calls)
            last_compaction = compaction_state
            continue
        if event_type == "user_message":
            if "content" not in payload:
                raise ValueError("user_message payload must contain 'content'")
            projected.append(
                ModelMessage(
                    role=ModelMessageRoleType.HUMAN,
                    content=deepcopy(payload["content"]),
                )
            )
            continue

        if event_type == "assistant_message":
            tool_calls = deepcopy(payload.get("tool_calls") or [])
            if not isinstance(tool_calls, list):
                raise ValueError("assistant_message 'tool_calls' must be a list")
            if "content" not in payload and not tool_calls:
                raise ValueError(
                    "assistant_message without tool_calls must contain 'content'"
                )

            call_ids: list[str] = []
            for tool_call in tool_calls:
                if not isinstance(tool_call, Mapping):
                    raise ValueError("each assistant tool_call must be an object")
                call_id = tool_call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError(
                        "each assistant tool_call must have a non-empty 'id'"
                    )
                if call_id in call_ids:
                    raise ValueError(
                        f"duplicate tool_call_id in assistant message: {call_id}"
                    )
                call_ids.append(call_id)

            content = deepcopy(payload.get("content"))
            # ModelMessage's content is non-nullable, while Chat Completions
            # permits null content for an assistant message containing calls.
            if content is None:
                if not tool_calls:
                    raise ValueError(
                        "assistant_message without tool_calls cannot have null content"
                    )
                content = ""
            projected.append(
                ModelMessage(
                    role=ModelMessageRoleType.AI,
                    content=content,
                    tool_calls=tool_calls or None,
                )
            )
            pending_calls.update((call_id, None) for call_id in call_ids)
            continue

        # The only remaining supported non-lifecycle event is tool_result.
        call_id = payload.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("tool_result payload must have a non-empty 'tool_call_id'")
        if call_id not in pending_calls:
            raise ValueError(
                f"tool_result has no pending assistant tool_call: {call_id}"
            )
        if "content" not in payload or not isinstance(payload["content"], str):
            raise ValueError("tool_result payload must contain string 'content'")

        projected.append(
            ModelMessage(
                role=ModelMessageRoleType.TOOL,
                content=deepcopy(payload["content"]),
                tool_call_id=call_id,
            )
        )
        del pending_calls[call_id]

    if pending_calls:
        flush_unresolved_calls()

    return ChatCompletionHistoryProjection(
        messages=tuple(projected),
        synthetic_tool_call_ids=frozenset(synthetic_ids),
        latest_event_seq=latest_event_seq,
        last_compaction=last_compaction,
    )
