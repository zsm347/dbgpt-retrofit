"""Tests for pure rollout-to-model-history projection."""

from copy import deepcopy

import pytest

from dbgpt.agent.core.rollout import (
    CompactionState,
    RolloutEvent,
    project_rollout_events,
)


def _tool_call(call_id: str, name: str = "query"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": '{"sql":"select 1"}'},
    }


def _compaction_payload(**overrides):
    payload = {
        "schema_version": 1,
        "reason": "proactive",
        "strategy": "prune",
        "replacement_history": [
            {"role": "human", "content": "question one"},
            {"role": "ai", "content": "", "tool_calls": [_tool_call("c1")]},
            {"role": "tool", "tool_call_id": "c1", "content": "pruned result"},
            {"role": "human", "content": "question two"},
            {"role": "ai", "content": "answer two"},
        ],
        "covers": {"through_event_seq": 42},
        "accounting": {
            "tokens_before": 1000,
            "tokens_after": 400,
            "reclaimed": 600,
            "pruned_count": 1,
        },
        "rearm_tokens": 700,
        "created_by": {"turn_id": "turn-1", "ts": "2026-10-13T00:00:00Z"},
    }
    payload.update(overrides)
    return payload


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


def test_projection_without_compaction_keeps_audit_fields_empty():
    events = [
        RolloutEvent("turn_started"),
        RolloutEvent("user_message", {"content": "What is 1 + 1?"}),
        RolloutEvent("assistant_message", {"content": "2"}),
        RolloutEvent("turn_completed"),
    ]

    result = project_rollout_events(events)

    assert result.latest_event_seq is None
    assert result.last_compaction is None


def test_context_compacted_replaces_all_prior_projected_history():
    events = [
        RolloutEvent("turn_started"),
        RolloutEvent("user_message", {"content": "old question"}),
        RolloutEvent("assistant_message", {"content": "old answer"}),
        RolloutEvent("context_compacted", _compaction_payload()),
        RolloutEvent("user_message", {"content": "follow up"}),
    ]

    result = project_rollout_events(events)

    assert [(message.role, message.content) for message in result.messages] == [
        ("human", "question one"),
        ("ai", ""),
        ("tool", "pruned result"),
        ("human", "question two"),
        ("ai", "answer two"),
        ("human", "follow up"),
    ]
    assert result.messages[1].tool_calls == [_tool_call("c1")]
    assert result.messages[2].tool_call_id == "c1"
    assert result.synthetic_tool_call_ids == frozenset()


def test_later_compaction_event_supersedes_the_earlier_replacement():
    first = _compaction_payload()
    second = _compaction_payload(
        replacement_history=[
            {"role": "human", "content": "compacted v2"},
            {"role": "ai", "content": "answer v2"},
        ],
        covers={"through_event_seq": 88},
        accounting={
            "tokens_before": 900,
            "tokens_after": 100,
            "reclaimed": 800,
            "pruned_count": 2,
        },
        rearm_tokens=999,
    )
    events = [
        RolloutEvent("user_message", {"content": "original"}),
        RolloutEvent("context_compacted", first),
        RolloutEvent("user_message", {"content": "between"}),
        RolloutEvent("context_compacted", second),
    ]

    result = project_rollout_events(events)

    assert [(message.role, message.content) for message in result.messages] == [
        ("human", "compacted v2"),
        ("ai", "answer v2"),
    ]
    assert result.last_compaction == CompactionState(
        rearm_tokens=999,
        through_event_seq=88,
        accounting={
            "tokens_before": 900,
            "tokens_after": 100,
            "reclaimed": 800,
            "pruned_count": 2,
        },
    )


@pytest.mark.parametrize(
    "payload",
    [
        _compaction_payload(
            replacement_history=[
                {"role": "ai", "content": "", "tool_calls": [_tool_call("c1")]},
                {"role": "tool", "tool_call_id": "missing", "content": "x"},
            ]
        ),
        _compaction_payload(
            replacement_history=[{"role": "system", "content": "system prompt"}]
        ),
        _compaction_payload(
            replacement_history=[{"role": "view", "content": "display only"}]
        ),
        _compaction_payload(
            replacement_history=[{"role": "human", "content": 5}]
        ),
        _compaction_payload(
            replacement_history=[{"role": "ai", "content": None}]
        ),
        _compaction_payload(replacement_history=[]),
        _compaction_payload(rearm_tokens=-1),
        _compaction_payload(covers={"through_event_seq": "42"}),
        _compaction_payload(accounting=None),
    ],
)
def test_rejects_malformed_compaction_payloads(payload):
    with pytest.raises(ValueError):
        project_rollout_events([RolloutEvent("context_compacted", payload)])


def test_compaction_leaves_open_calls_resolvable_by_later_results():
    payload = _compaction_payload(
        replacement_history=[
            {"role": "human", "content": "run the query"},
            {"role": "ai", "content": "", "tool_calls": [_tool_call("c1")]},
        ]
    )
    events = [
        RolloutEvent("context_compacted", payload),
        RolloutEvent("tool_result", {"tool_call_id": "c1", "content": "real result"}),
    ]

    result = project_rollout_events(events)

    assert [(message.role, message.content) for message in result.messages] == [
        ("human", "run the query"),
        ("ai", ""),
        ("tool", "real result"),
    ]
    assert result.synthetic_tool_call_ids == frozenset()


def test_compaction_re_synthesizes_unresolved_calls_at_next_boundary():
    payload = _compaction_payload(
        replacement_history=[
            {"role": "human", "content": "run the query"},
            {"role": "ai", "content": "", "tool_calls": [_tool_call("c1")]},
        ]
    )
    events = [
        RolloutEvent("context_compacted", payload),
        RolloutEvent("user_message", {"content": "next turn"}),
    ]

    result = project_rollout_events(events)

    assert [(message.role, message.content) for message in result.messages] == [
        ("human", "run the query"),
        ("ai", ""),
        ("tool", "aborted"),
        ("human", "next turn"),
    ]
    assert result.messages[2].tool_call_id == "c1"
    assert result.synthetic_tool_call_ids == frozenset({"c1"})


def test_projection_exposes_latest_event_seq_and_last_compaction():
    payload = _compaction_payload()
    events = [
        RolloutEvent("turn_started", event_seq=1),
        RolloutEvent("user_message", {"content": "hi"}, event_seq=2),
        RolloutEvent("context_compacted", payload, event_seq=3),
        RolloutEvent("user_message", {"content": "again"}, event_seq=4),
    ]

    result = project_rollout_events(events)

    assert result.latest_event_seq == 4
    assert result.last_compaction is not None
    assert result.last_compaction.rearm_tokens == 700
    assert result.last_compaction.through_event_seq == 42
    assert result.last_compaction.accounting["reclaimed"] == 600
    # The audit snapshot is a copy: later mutation of the source payload
    # must not bleed into the projection.
    payload["accounting"]["reclaimed"] = -1
    payload["rearm_tokens"] = 1
    assert result.last_compaction.accounting["reclaimed"] == 600
    assert result.last_compaction.rearm_tokens == 700


def test_projection_records_structured_tool_success():
    events = [
        RolloutEvent("user_message", {"content": "run both"}),
        RolloutEvent(
            "assistant_message",
            {"tool_calls": [_tool_call("c1"), _tool_call("c2")]},
        ),
        RolloutEvent(
            "tool_result", {"tool_call_id": "c1", "content": "ok", "success": True}
        ),
        RolloutEvent(
            "tool_result", {"tool_call_id": "c2", "content": "boom", "success": False}
        ),
    ]

    result = project_rollout_events(events)

    assert result.tool_success == {"c1": True, "c2": False}


def test_projection_ignores_non_boolean_success_values():
    events = [
        RolloutEvent("assistant_message", {"tool_calls": [_tool_call("c1")]}),
        RolloutEvent(
            "tool_result", {"tool_call_id": "c1", "content": "ok", "success": "yes"}
        ),
        RolloutEvent("assistant_message", {"tool_calls": [_tool_call("c2")]}),
        RolloutEvent("tool_result", {"tool_call_id": "c2", "content": "ok"}),
    ]

    result = project_rollout_events(events)

    assert result.tool_success == {}


def test_tool_success_survives_a_context_compacted_replacement():
    events = [
        RolloutEvent("user_message", {"content": "q"}),
        RolloutEvent("assistant_message", {"tool_calls": [_tool_call("c1")]}),
        RolloutEvent(
            "tool_result", {"tool_call_id": "c1", "content": "ok", "success": True}
        ),
        RolloutEvent("context_compacted", _compaction_payload()),
    ]

    result = project_rollout_events(events)

    # F8: a replacement keeps call ids, so recorded outcomes stay valid and
    # are deliberately not cleared.
    assert result.tool_success == {"c1": True}
