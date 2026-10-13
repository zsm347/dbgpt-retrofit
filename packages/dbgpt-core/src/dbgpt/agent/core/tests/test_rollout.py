"""Tests for pure rollout-to-model-history projection."""

from copy import deepcopy

import pytest

from dbgpt.agent.core.rollout import RolloutEvent, project_rollout_events


def _tool_call(call_id: str, name: str = "query"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": '{"sql":"select 1"}'},
    }


def test_projects_messages_in_event_order_and_omits_lifecycle_markers():
    events = [
        RolloutEvent("turn_started"),
        RolloutEvent("user_message", {"content": "What is 1 + 1?"}),
        RolloutEvent("assistant_message", {"content": "2"}),
        RolloutEvent("turn_completed"),
    ]

    result = project_rollout_events(events)

    assert [(message.role, message.content) for message in result.messages] == [
        ("human", "What is 1 + 1?"),
        ("ai", "2"),
    ]
    assert result.synthetic_tool_call_ids == frozenset()


def test_preserves_incomplete_turn_and_synthesizes_only_missing_call_result():
    events = [
        RolloutEvent("turn_started"),
        RolloutEvent("user_message", {"content": "Run both queries"}),
        RolloutEvent(
            "assistant_message",
            {
                "content": None,
                "tool_calls": [_tool_call("call_a"), _tool_call("call_b")],
            },
        ),
        # Parallel results are preserved in their committed event order.
        RolloutEvent(
            "tool_result",
            {"tool_call_id": "call_b", "content": "real result B"},
        ),
        RolloutEvent("turn_aborted", {"reason": "process_lost"}),
    ]
    original = deepcopy(events)

    result = project_rollout_events(events)

    assert [message.role for message in result.messages] == [
        "human",
        "ai",
        "tool",
        "tool",
    ]
    assistant_call_message = result.messages[1]
    assert assistant_call_message.tool_calls == [
        _tool_call("call_a"),
        _tool_call("call_b"),
    ]
    assert result.messages[2].tool_call_id == "call_b"
    assert result.messages[2].content == "real result B"
    assert result.messages[3].tool_call_id == "call_a"
    assert result.messages[3].content == "aborted"
    assert result.synthetic_tool_call_ids == frozenset({"call_a"})
    # The projection is a copy: it does not add a fake result to source events.
    assert events == original


def test_complete_out_of_order_parallel_results_need_no_synthetic_messages():
    events = [
        RolloutEvent(
            "assistant_message", {"tool_calls": [_tool_call("a"), _tool_call("b")]}
        ),
        RolloutEvent("tool_result", {"tool_call_id": "b", "content": "B"}),
        RolloutEvent("tool_result", {"tool_call_id": "a", "content": "A"}),
    ]

    result = project_rollout_events(events)

    assert [
        (message.tool_call_id, message.content) for message in result.messages[1:]
    ] == [
        ("b", "B"),
        ("a", "A"),
    ]
    assert result.synthetic_tool_call_ids == frozenset()


def test_missing_result_is_inserted_before_the_next_message_boundary():
    events = [
        RolloutEvent("user_message", {"content": "first"}),
        RolloutEvent("assistant_message", {"tool_calls": [_tool_call("call_x")]}),
        RolloutEvent("user_message", {"content": "next turn"}),
    ]

    result = project_rollout_events(events)

    assert [(message.role, message.content) for message in result.messages] == [
        ("human", "first"),
        ("ai", ""),
        ("tool", "aborted"),
        ("human", "next turn"),
    ]
    assert result.messages[2].tool_call_id == "call_x"


@pytest.mark.parametrize(
    "event",
    [
        RolloutEvent("tool_result", {"tool_call_id": "missing", "content": "x"}),
        RolloutEvent("user_message", {}),
        RolloutEvent("assistant_message", {"content": None}),
        RolloutEvent("unknown_event"),
    ],
)
def test_rejects_malformed_or_unknown_events(event):
    with pytest.raises(ValueError):
        project_rollout_events([event])


def test_rejects_duplicate_tool_result_for_a_call():
    events = [
        RolloutEvent("assistant_message", {"tool_calls": [_tool_call("call_x")]}),
        RolloutEvent("tool_result", {"tool_call_id": "call_x", "content": "first"}),
        RolloutEvent("tool_result", {"tool_call_id": "call_x", "content": "duplicate"}),
    ]

    with pytest.raises(ValueError, match="no pending assistant tool_call"):
        project_rollout_events(events)
