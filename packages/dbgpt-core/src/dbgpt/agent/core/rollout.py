"""Pure projection helpers for rebuilding model history from rollout events.

This module deliberately has no storage or execution responsibilities. Callers
must provide committed events in ``event_seq`` order. Synthetic tool results
exist only in the returned prompt projection; they must never be persisted as
authoritative rollout events or used to re-run a tool.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Tuple

from dbgpt.core.interface.message import ModelMessage, ModelMessageRoleType

_SUPPORTED_EVENT_TYPES = {
    "turn_started",
    "user_message",
    "assistant_message",
    "tool_result",
    "turn_completed",
    "turn_aborted",
}
_LIFECYCLE_EVENT_TYPES = {"turn_started", "turn_completed", "turn_aborted"}
_SYNTHETIC_TOOL_RESULT = "aborted"


@dataclass(frozen=True)
class RolloutEvent:
    """Storage-independent event input for the rollout history projector.

    ``payload`` follows the v1 logical event contract. This is intentionally
    not a persistence model and contains no database IDs or schema machinery.
    """

    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatCompletionHistoryProjection:
    """Model messages plus IDs of prompt-only synthetic tool results."""

    messages: Tuple[ModelMessage, ...]
    synthetic_tool_call_ids: frozenset[str]


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

    Malformed protocol history raises ``ValueError`` instead of silently
    dropping or mis-pairing messages. The function never mutates its inputs.
    """

    projected: list[ModelMessage] = []
    synthetic_ids: set[str] = set()
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

        # A tool result can resolve the current assistant call group. Any
        # other event marks the end of that group's result window.
        if pending_calls and event_type != "tool_result":
            flush_unresolved_calls()

        if event_type in _LIFECYCLE_EVENT_TYPES:
            continue

        payload = event.payload
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
    )
