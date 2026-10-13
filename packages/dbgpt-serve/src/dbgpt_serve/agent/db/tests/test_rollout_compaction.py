"""Tests for the rollout compaction coordinator (Phase 0)."""

import json
import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from dbgpt.agent import AgentContext, AgentMessage
from dbgpt.agent.core.context.budget import ContextBudgetConfig
from dbgpt.agent.expand.tool_calling_agent import ToolCallingReActAgent
from dbgpt.agent.resource.tool.base import tool
from dbgpt.agent.resource.tool.pack import ToolPack
from dbgpt.core.interface.llm import ModelOutput
from dbgpt.core.interface.message import ModelMessageRoleType
from dbgpt.storage.chat_history.chat_history_db import ChatHistoryEntity
from dbgpt.storage.metadata import db
from dbgpt_serve.agent.db import AgentRolloutSession
from dbgpt_serve.agent.db.rollout_compaction import (
    COMPACTION_LOG_PREFIX,
    maybe_compact,
)


def _tool_call(call_id: str, name: str = "query"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": '{"sql":"select 1"}'},
    }


def _compaction_config(**overrides):
    values = dict(
        context_compaction_enabled=True,
        proactive_prune_tokens=1000,
        min_reclaim_tokens=100,
        protect_tail_tokens=0,
        protect_last_rounds=1,
        min_prune_chars=1000,
    )
    values.update(overrides)
    return ContextBudgetConfig(**values)


@pytest.fixture
def conversation_db(tmp_path):
    if db.engine is not None:
        db.engine.dispose()
    db.init_db(
        f"sqlite:///{tmp_path / 'rollout_compaction_test.db'}",
        engine_args={"connect_args": {"timeout": 10}},
    )
    db.create_all()
    with db.session() as session:
        session.add(
            ChatHistoryEntity(
                conv_uid="conv-1",
                chat_mode="chat_react_agent",
                summary="test conversation",
            )
        )
    yield db
    db.engine.dispose()


def _seed_session(session: AgentRolloutSession, big: str = "B" * 20000):
    """One compressible turn with a big result plus a protected current turn."""
    session.start_turn("first question")
    session.append(
        "assistant_message",
        {"content": None, "tool_calls": [_tool_call("old-call")]},
    )
    session.append(
        "tool_result", {"tool_call_id": "old-call", "content": big, "success": True}
    )
    session.append("user_message", {"content": "current question"})


def _event_types(session: AgentRolloutSession):
    return [event.event_type for event in session.dao.list_by_conv_id(session.conv_id)]


def test_dry_run_measures_without_appending_events(conversation_db, caplog):
    session = AgentRolloutSession("conv-1", str(uuid4()))
    _seed_session(session)
    before = _event_types(session)

    with caplog.at_level(
        logging.INFO, logger="dbgpt_serve.agent.db.rollout_compaction"
    ):
        outcome = maybe_compact(
            session, _compaction_config(), window_tokens=200000, dry_run=True
        )

    assert outcome.dry_run is True
    assert outcome.appended is None
    assert outcome.decision is not None and outcome.decision.should_compact
    assert outcome.decision.would_reclaim > 0
    assert outcome.decision.would_prune_count == 1
    # Zero events written and the projection is unchanged.
    assert _event_types(session) == before
    assert outcome.projection is not None
    assert outcome.projection.messages == session.load_projection().messages

    log_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(COMPACTION_LOG_PREFIX)
    ]
    assert len(log_lines) == 1
    fields = json.loads(log_lines[0].split(" ", 1)[1])
    assert fields["event"] == "dry_run"
    assert fields["gate"] == "passed"
    assert fields["est_tokens"] == outcome.decision.est_tokens
    assert fields["watermark"] == outcome.decision.watermark
    assert fields["would_reclaim"] == outcome.decision.would_reclaim
    assert fields["would_prune_count"] == 1


def test_dry_run_logs_rejection_reasons_for_the_gate_distribution(
    conversation_db, caplog
):
    session = AgentRolloutSession("conv-1", str(uuid4()))
    _seed_session(session)

    with caplog.at_level(
        logging.INFO, logger="dbgpt_serve.agent.db.rollout_compaction"
    ):
        outcome = maybe_compact(
            session,
            _compaction_config(min_reclaim_tokens=10**9),
            window_tokens=200000,
            dry_run=True,
        )

    assert outcome.appended is None
    assert outcome.decision.gate == "reclaim"
    log_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(COMPACTION_LOG_PREFIX)
    ]
    assert len(log_lines) == 1
    fields = json.loads(log_lines[0].split(" ", 1)[1])
    assert fields["gate"] == "reclaim"
    assert fields["would_reclaim"] > 0
    assert fields["would_prune_count"] == 1


def test_commit_appends_event_and_switches_the_projection(conversation_db):
    session = AgentRolloutSession("conv-1", str(uuid4()))
    _seed_session(session)

    outcome = maybe_compact(session, _compaction_config(), window_tokens=200000)

    assert outcome.appended is not None
    assert outcome.appended.event_type == "context_compacted"
    projection = session.load_projection()
    assert outcome.projection is not None
    assert outcome.projection.messages == projection.messages
    pruned = projection.messages[2].content
    assert "[truncated, total 20000 chars]" in pruned
    assert len(pruned) < 2000
    assert projection.last_compaction is not None
    assert (
        projection.last_compaction.rearm_tokens == outcome.decision.draft.rearm_tokens
    )
    # The replacement covers everything committed before its own append.
    assert (
        projection.last_compaction.through_event_seq == outcome.appended.event_seq - 1
    )


def test_second_compaction_supersedes_the_first_one(conversation_db):
    session = AgentRolloutSession("conv-1", str(uuid4()))
    _seed_session(session)
    first = maybe_compact(session, _compaction_config(), window_tokens=200000)
    assert first.appended is not None

    # Grow the transcript until another compaction is due (rearm runway).
    session.append(
        "assistant_message",
        {"content": None, "tool_calls": [_tool_call("newer-call")]},
    )
    session.append(
        "tool_result",
        {"tool_call_id": "newer-call", "content": "C" * 40000, "success": True},
    )
    session.append("user_message", {"content": "third question"})

    second = maybe_compact(session, _compaction_config(), window_tokens=200000)

    assert second.appended is not None
    events = session.dao.list_by_conv_id("conv-1")
    assert [event.event_type for event in events].count("context_compacted") == 2
    projection = session.load_projection()
    assert projection.messages[-1].content == "third question"
    assert "[truncated, total 40000 chars]" in projection.messages[-2].content
    assert (
        projection.last_compaction.through_event_seq == second.appended.event_seq - 1
    )


def test_failed_append_leaves_the_event_stream_unchanged(conversation_db, caplog):
    session = AgentRolloutSession("conv-1", str(uuid4()))
    _seed_session(session)
    # Simulate a fenced-out worker: the lease expires behind the session's back.
    with conversation_db.session() as db_session:
        conversation = (
            db_session.query(ChatHistoryEntity)
            .filter(ChatHistoryEntity.conv_uid == "conv-1")
            .one()
        )
        conversation.active_agent_turn_lease_until = datetime.utcnow() - timedelta(
            seconds=1
        )
    before = _event_types(session)

    with caplog.at_level(
        logging.WARNING, logger="dbgpt_serve.agent.db.rollout_compaction"
    ):
        outcome = maybe_compact(session, _compaction_config(), window_tokens=200000)

    assert outcome.decision is not None and outcome.decision.should_compact
    assert outcome.appended is None
    assert _event_types(session) == before
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(COMPACTION_LOG_PREFIX)
    ]
    assert len(warnings) == 1


def test_synthetic_turn_is_protected_while_other_turns_compact(conversation_db):
    session = AgentRolloutSession("conv-1", str(uuid4()))
    session.start_turn("crashed turn")
    session.append(
        "assistant_message",
        {
            "content": None,
            "tool_calls": [_tool_call("call-done"), _tool_call("call-unknown")],
        },
    )
    session.append(
        "tool_result",
        {"tool_call_id": "call-done", "content": "D" * 20000, "success": True},
    )
    session.append("user_message", {"content": "second question"})
    session.append(
        "assistant_message", {"content": None, "tool_calls": [_tool_call("call-2")]}
    )
    session.append(
        "tool_result",
        {"tool_call_id": "call-2", "content": "E" * 20000, "success": True},
    )
    session.append("user_message", {"content": "third question"})

    assert session.load_projection().synthetic_tool_call_ids == frozenset(
        {"call-unknown"}
    )

    outcome = maybe_compact(session, _compaction_config(), window_tokens=200000)

    assert outcome.appended is not None
    projection = session.load_projection()
    contents = [message.content for message in projection.messages]
    # The synthetic turn's result survived verbatim; the clean turn was pruned.
    assert "D" * 20000 in contents
    assert any(
        "E" * 1000 in content and "[truncated, total 20000 chars]" in content
        for content in contents
    )
    assert projection.synthetic_tool_call_ids == frozenset({"call-unknown"})


def test_disabled_feature_is_a_strict_no_op(conversation_db):
    session = AgentRolloutSession("conv-1", str(uuid4()))
    _seed_session(session)
    before = session.dao.list_by_conv_id("conv-1")

    outcome = maybe_compact(
        session,
        _compaction_config(context_compaction_enabled=False),
        window_tokens=200000,
    )

    assert outcome.status is None
    assert outcome.decision is None
    assert outcome.appended is None
    after = session.dao.list_by_conv_id("conv-1")
    assert [
        (event.event_id, event.event_seq, event.event_type, event.payload)
        for event in after
    ] == [
        (event.event_id, event.event_seq, event.event_type, event.payload)
        for event in before
    ]


@pytest.mark.asyncio
async def test_compacted_view_reaches_the_native_model_request(conversation_db):
    @tool(description="Return a fixed payload")
    def echo_tool() -> str:
        return "ok"

    conv_id = "conv-1"
    rollout = AgentRolloutSession(conv_id, str(uuid4()))
    _seed_session(rollout)

    outcome = maybe_compact(rollout, _compaction_config(), window_tokens=200000)
    assert outcome.appended is not None

    agent = ToolCallingReActAgent()
    agent.agent_context = AgentContext(
        conv_id=conv_id,
        enable_native_function_calling=True,
        enable_rollout_history=True,
    )
    resource = ToolPack([echo_tool._tool])
    agent.resource = resource
    for action in agent.actions:
        action.init_resource(resource)

    class StubClient:
        requests = []

        async def create_with_output(self, **kwargs):
            self.requests.append(kwargs)
            return ModelOutput.build(text="done")

    client = StubClient()
    object.__setattr__(agent, "llm_client", client)
    object.__setattr__(agent, "_a_select_llm_model", AsyncMock(return_value="stub"))

    async def load_history():
        rollout.assert_turn_lease()
        return rollout.load_projection()

    async def append_assistant(payload):
        rollout.append("assistant_message", payload)

    async def append_tool_result(payload):
        rollout.append("tool_result", payload)

    async def assert_tool_owner(_payload):
        rollout.assert_turn_lease()

    async def assert_model_owner():
        rollout.assert_turn_lease()

    agent.configure_rollout_history(
        message_loader=load_history,
        on_assistant_message=append_assistant,
        on_tool_result=append_tool_result,
        on_tool_start=assert_tool_owner,
        on_model_call=assert_model_owner,
    )
    system_message = AgentMessage(
        content="current system instructions", role=ModelMessageRoleType.SYSTEM
    )

    await agent._thinking_native([system_message])

    request_messages = client.requests[0]["messages"]
    tool_messages = [
        message for message in request_messages if message["role"] == "tool"
    ]
    assert len(tool_messages) == 1
    assert "[truncated, total 20000 chars]" in tool_messages[0]["content"]
    assert len(tool_messages[0]["content"]) < 2000
    user_messages = [
        message for message in request_messages if message["role"] == "user"
    ]
    assert user_messages[-1]["content"] == "current question"
