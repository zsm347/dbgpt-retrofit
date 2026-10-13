"""Tests for durable ordered conversation event storage."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import IntegrityError

from dbgpt.agent import AgentContext, AgentMessage
from dbgpt.agent.expand.tool_calling_agent import (
    NativeToolCallAction,
    ToolCallingReActAgent,
)
from dbgpt.agent.resource.tool.base import tool
from dbgpt.agent.resource.tool.pack import ToolPack
from dbgpt.core.interface.llm import ModelOutput
from dbgpt.core.interface.message import ModelMessageRoleType
from dbgpt.storage.chat_history.chat_history_db import ChatHistoryEntity
from dbgpt.storage.metadata import DatabaseManager, db
from dbgpt_serve.agent.db.agent_event_db import (
    ActiveTurnInProgress,
    AgentEventDao,
    AgentEventDraft,
    AgentEventEntity,
    AgentEventIdempotencyConflict,
    AgentRolloutSession,
    ConversationRowNotFound,
    TurnLeaseLost,
)


@pytest.fixture
def conversation_db(tmp_path):
    if db.engine is not None:
        db.engine.dispose()
    db.init_db(
        f"sqlite:///{tmp_path / 'agent_event_test.db'}",
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


def test_appends_batch_atomically_and_reads_in_event_sequence_order(conversation_db):
    dao = AgentEventDao()
    turn_id = str(uuid4())
    events = [
        AgentEventDraft("turn_started", {}),
        AgentEventDraft("user_message", {"content": "hello"}),
        AgentEventDraft(
            "assistant_message",
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "query", "arguments": "{}"},
                    }
                ],
            },
        ),
        AgentEventDraft(
            "tool_result",
            {"tool_call_id": "call-1", "content": "rows=[]", "success": True},
        ),
    ]

    appended = dao.append_events("conv-1", turn_id, events)
    read_back = dao.list_by_conv_id("conv-1")

    assert [event.event_seq for event in appended] == [1, 2, 3, 4]
    assert [event.event_type for event in read_back] == [
        "turn_started",
        "user_message",
        "assistant_message",
        "tool_result",
    ]
    assert read_back[2].payload["tool_calls"][0]["id"] == "call-1"
    assert read_back[3].payload["tool_call_id"] == "call-1"
    assert [
        event.event_seq for event in dao.list_by_conv_id("conv-1", after_seq=2)
    ] == [
        3,
        4,
    ]
    assert [event.event_seq for event in dao.list_by_conv_id("conv-1", limit=2)] == [
        1,
        2,
    ]


def test_append_retry_with_same_full_batch_is_idempotent(conversation_db):
    dao = AgentEventDao()
    turn_id = str(uuid4())
    events = [
        AgentEventDraft("turn_started", {}),
        AgentEventDraft("user_message", {"content": "hello"}),
    ]

    first = dao.append_events("conv-1", turn_id, events)
    retried = dao.append_events("conv-1", turn_id, events)

    assert [(event.event_id, event.event_seq) for event in retried] == [
        (event.event_id, event.event_seq) for event in first
    ]
    assert len(dao.list_by_conv_id("conv-1")) == 2


def test_partial_batch_retry_fails_closed_without_consuming_sequence(conversation_db):
    dao = AgentEventDao()
    turn_id = str(uuid4())
    existing = AgentEventDraft("turn_started", {})
    dao.append_event("conv-1", turn_id, existing)

    with pytest.raises(AgentEventIdempotencyConflict):
        dao.append_events(
            "conv-1",
            turn_id,
            [existing, AgentEventDraft("user_message", {"content": "hello"})],
        )

    next_event = dao.append_event(
        "conv-1", str(uuid4()), AgentEventDraft("turn_started", {})
    )
    assert next_event.event_seq == 2


def test_reusing_event_id_with_different_payload_fails(conversation_db):
    dao = AgentEventDao()
    turn_id = str(uuid4())
    original = AgentEventDraft("user_message", {"content": "first"})
    dao.append_event("conv-1", turn_id, original)

    changed = AgentEventDraft(
        "user_message",
        {"content": "changed"},
        event_id=original.event_id,
    )
    with pytest.raises(AgentEventIdempotencyConflict):
        dao.append_event("conv-1", turn_id, changed)

    assert len(dao.list_by_conv_id("conv-1")) == 1


def test_rejects_unserializable_or_unknown_events_before_writing(conversation_db):
    dao = AgentEventDao()
    with pytest.raises(ValueError, match="JSON-serializable"):
        dao.append_event(
            "conv-1",
            str(uuid4()),
            AgentEventDraft("user_message", {"content": "valid", "bad": object()}),
        )
    with pytest.raises(ValueError, match="unsupported event type"):
        dao.append_event(
            "conv-1",
            str(uuid4()),
            AgentEventDraft("trace_span", {}),
        )
    assert dao.list_by_conv_id("conv-1") == []


def test_rejects_protocol_events_that_cannot_be_recovered(conversation_db):
    dao = AgentEventDao()
    with pytest.raises(ValueError, match="function arguments must be a string"):
        dao.append_event(
            "conv-1",
            str(uuid4()),
            AgentEventDraft(
                "assistant_message",
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "query", "arguments": {}},
                        }
                    ],
                },
            ),
        )
    with pytest.raises(ValueError, match="tool_call_id"):
        dao.append_event(
            "conv-1",
            str(uuid4()),
            AgentEventDraft("tool_result", {"content": "ok"}),
        )
    assert dao.list_by_conv_id("conv-1") == []


def test_rollout_session_projects_committed_provider_history(conversation_db):
    session = AgentRolloutSession("conv-1", str(uuid4()))
    session.start_turn("run a query")
    session.append(
        "assistant_message",
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "query", "arguments": "{}"},
                }
            ],
        },
    )
    session.append(
        "tool_result",
        {"tool_call_id": "call-1", "content": "rows=[]", "success": True},
    )

    projection = session.load_projection()

    assert [message.role for message in projection.messages] == [
        "human",
        "ai",
        "tool",
    ]
    assert projection.messages[-1].tool_call_id == "call-1"
    assert projection.messages[-1].content == "rows=[]"
    assert projection.synthetic_tool_call_ids == frozenset()


def test_rollout_session_marks_missing_tool_result_without_replaying(conversation_db):
    session = AgentRolloutSession("conv-1", str(uuid4()))
    session.start_turn("run a query")
    session.append(
        "assistant_message",
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "query", "arguments": "{}"},
                }
            ],
        },
    )

    projection = session.load_projection()

    assert projection.messages[-1].role == "tool"
    assert projection.messages[-1].tool_call_id == "call-1"
    assert projection.messages[-1].content == "aborted"
    assert projection.synthetic_tool_call_ids == frozenset({"call-1"})
    assert [event.event_type for event in session.dao.list_by_conv_id("conv-1")] == [
        "turn_started",
        "user_message",
        "assistant_message",
    ]


def test_failed_batch_insert_rolls_back_every_event(conversation_db):
    dao = AgentEventDao()
    turn_id = str(uuid4())
    with conversation_db.engine.begin() as connection:
        connection.execute(
            text(
                """CREATE TRIGGER reject_user_event BEFORE INSERT ON agent_event
                WHEN NEW.event_type = 'user_message'
                BEGIN SELECT RAISE(ABORT, 'intentional test failure'); END"""
            )
        )

    with pytest.raises(IntegrityError, match="intentional test failure"):
        dao.append_events(
            "conv-1",
            turn_id,
            [
                AgentEventDraft("turn_started", {}),
                AgentEventDraft("user_message", {"content": "must roll back"}),
            ],
        )

    assert dao.list_by_conv_id("conv-1") == []


def test_requires_an_existing_chat_history_row(conversation_db):
    with pytest.raises(ConversationRowNotFound):
        AgentEventDao().append_event(
            "missing-conversation",
            str(uuid4()),
            AgentEventDraft("turn_started", {}),
        )


def test_sqlite_concurrent_appends_receive_unique_ordered_sequences(conversation_db):
    barrier = Barrier(2)

    def append_one():
        barrier.wait(timeout=5)
        return AgentEventDao().append_event(
            "conv-1",
            str(uuid4()),
            AgentEventDraft("turn_started", {}),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(append_one) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert sorted(event.event_seq for event in results) == [1, 2]
    assert [event.event_seq for event in AgentEventDao().list_by_conv_id("conv-1")] == [
        1,
        2,
    ]


def test_turn_lease_rejects_a_second_live_owner(conversation_db):
    dao = AgentEventDao()
    first_turn = str(uuid4())
    first = dao.acquire_turn_lease("conv-1", first_turn, lease_seconds=30)

    with pytest.raises(ActiveTurnInProgress):
        dao.acquire_turn_lease("conv-1", str(uuid4()), lease_seconds=30)

    dao.release_turn_lease(first)


def test_legacy_chat_history_schema_adds_nullable_lease_columns(tmp_path):
    manager = DatabaseManager()
    manager.init_db(f"sqlite:///{tmp_path / 'legacy_chat_history.db'}")
    try:
        with manager.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE chat_history "
                    "(id INTEGER PRIMARY KEY, conv_uid VARCHAR(255))"
                )
            )

        AgentEventDao(manager)._ensure_turn_lease_columns()

        columns = {
            column["name"]
            for column in inspect(manager.engine).get_columns("chat_history")
        }
        assert {
            "active_agent_turn_id",
            "active_agent_turn_lease_until",
            "active_agent_turn_fence",
        }.issubset(columns)
    finally:
        manager.engine.dispose()


def test_expired_turn_lease_can_be_taken_over_and_fences_old_worker(
    conversation_db,
):
    dao = AgentEventDao()
    started_at = datetime.utcnow()
    old_turn_id = str(uuid4())
    new_turn_id = str(uuid4())
    old_lease = dao.acquire_turn_lease(
        "conv-1", old_turn_id, lease_seconds=10, now=started_at
    )
    new_lease = dao.acquire_turn_lease(
        "conv-1",
        new_turn_id,
        lease_seconds=30,
        now=started_at + timedelta(seconds=11),
    )

    assert new_lease.fence == old_lease.fence + 1
    assert dao.release_turn_lease(old_lease) is False
    with pytest.raises(TurnLeaseLost):
        dao.append_event(
            "conv-1",
            old_turn_id,
            AgentEventDraft("turn_aborted", {}),
            lease=old_lease,
        )

    current_event = dao.append_event(
        "conv-1",
        new_turn_id,
        AgentEventDraft("turn_started", {}),
        lease=new_lease,
    )
    assert current_event.event_seq == 1
    assert dao.release_turn_lease(new_lease) is True


def test_turn_lease_renewal_extends_only_a_live_owner(conversation_db):
    dao = AgentEventDao()
    started_at = datetime.utcnow()
    lease = dao.acquire_turn_lease(
        "conv-1", str(uuid4()), lease_seconds=10, now=started_at
    )

    renewed = dao.renew_turn_lease(
        lease, lease_seconds=20, now=started_at + timedelta(seconds=5)
    )
    assert renewed.lease_until == started_at + timedelta(seconds=25)

    with pytest.raises(TurnLeaseLost):
        dao.renew_turn_lease(
            renewed, lease_seconds=20, now=started_at + timedelta(seconds=26)
        )


def test_new_turn_recovers_persisted_incomplete_turn_without_replaying_tools(
    conversation_db,
):
    dao = AgentEventDao()
    old_turn_id = str(uuid4())
    old_session = AgentRolloutSession("conv-1", old_turn_id, dao)
    old_session.start_turn("query sales by region")
    old_session.append(
        "assistant_message",
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call-done",
                    "type": "function",
                    "function": {"name": "query_sales", "arguments": "{}"},
                },
                {
                    "id": "call-unknown",
                    "type": "function",
                    "function": {"name": "query_region", "arguments": "{}"},
                },
            ],
        },
    )
    old_session.append(
        "tool_result",
        {
            "tool_call_id": "call-done",
            "content": "[{\"region\":\"east\",\"sales\":12}]",
            "success": True,
        },
    )

    # Simulate a crashed owner: the durable lease expires, but its committed
    # events stay intact and no synthetic result is written to the event log.
    with conversation_db.session() as session:
        conversation = (
            session.query(ChatHistoryEntity)
            .filter(ChatHistoryEntity.conv_uid == "conv-1")
            .one()
        )
        conversation.active_agent_turn_lease_until = datetime.utcnow() - timedelta(
            seconds=1
        )

    new_turn_id = str(uuid4())
    new_session = AgentRolloutSession("conv-1", new_turn_id, dao)
    new_session.start_turn("continue using the known result")

    projection = new_session.load_projection()
    events = dao.list_by_conv_id("conv-1")

    assert [message.role for message in projection.messages] == [
        "human",
        "ai",
        "tool",
        "tool",
        "human",
    ]
    assert projection.messages[0].content == "query sales by region"
    assert projection.messages[2].tool_call_id == "call-done"
    assert projection.messages[2].content == '[{"region":"east","sales":12}]'
    assert projection.messages[3].tool_call_id == "call-unknown"
    assert projection.messages[3].content == "aborted"
    assert projection.messages[4].content == "continue using the known result"
    assert projection.synthetic_tool_call_ids == frozenset({"call-unknown"})
    assert [event.event_type for event in events] == [
        "turn_started",
        "user_message",
        "assistant_message",
        "tool_result",
        "turn_started",
        "user_message",
    ]
    assert [event.turn_id for event in events[:4]] == [old_turn_id] * 4
    assert [event.turn_id for event in events[4:]] == [new_turn_id] * 2
    with pytest.raises(TurnLeaseLost):
        old_session.append("turn_aborted", {"reason": "stale owner"})


@pytest.mark.asyncio
async def test_event_store_drives_native_tool_round_trip_and_next_model_input(
    conversation_db,
):
    @tool(description="Add two integers")
    def add_numbers(a: int, b: int) -> int:
        return a + b

    conv_id = "conv-1"
    rollout = AgentRolloutSession(conv_id, str(uuid4()))
    rollout.start_turn("add 1 and 2")
    resource = ToolPack([add_numbers._tool])

    agent = ToolCallingReActAgent()
    agent.agent_context = AgentContext(
        conv_id=conv_id,
        enable_native_function_calling=True,
        enable_rollout_history=True,
    )
    agent.resource = resource
    for action in agent.actions:
        action.init_resource(resource)

    tool_call = {
        "id": "call-add",
        "type": "function",
        "function": {"name": "add_numbers", "arguments": '{"a":1,"b":2}'},
    }
    model_outputs = [
        ModelOutput.build(tool_calls=[tool_call]),
        ModelOutput.build(text="The sum is 3."),
    ]

    class StubClient:
        requests = []

        async def create_with_output(self, **kwargs):
            self.requests.append(kwargs)
            return model_outputs.pop(0)

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
    assert [event.event_type for event in rollout.dao.list_by_conv_id(conv_id)] == [
        "turn_started",
        "user_message",
        "assistant_message",
    ]

    native_action = next(
        action for action in agent.actions if isinstance(action, NativeToolCallAction)
    )
    tool_output = await native_action.run(
        "",
        resource=resource,
        need_vis_render=False,
        tool_calls=[tool_call],
        rollout_on_tool_start=assert_tool_owner,
        rollout_on_tool_result=append_tool_result,
    )
    assert tool_output.is_exe_success is True
    assert tool_output.content == "3"

    await agent._thinking_native([system_message])

    second_request_messages = client.requests[1]["messages"]
    assert [message["role"] for message in second_request_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert second_request_messages[-1]["tool_call_id"] == "call-add"
    assert second_request_messages[-1]["content"] == "3"
    assert [event.event_type for event in rollout.dao.list_by_conv_id(conv_id)] == [
        "turn_started",
        "user_message",
        "assistant_message",
        "tool_result",
        "assistant_message",
    ]


def test_simultaneous_turn_lease_acquisition_has_one_winner(conversation_db):
    barrier = Barrier(2)
    now = datetime.utcnow()

    def acquire(turn_id):
        barrier.wait(timeout=5)
        try:
            return AgentEventDao().acquire_turn_lease(
                "conv-1", turn_id, lease_seconds=30, now=now
            )
        except ActiveTurnInProgress:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(acquire, str(uuid4())) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert sum(result is not None for result in results) == 1


def test_payload_uses_portable_text_with_mysql_longtext_variant():
    payload_type = AgentEventEntity.__table__.c.payload.type

    assert payload_type.dialect_impl(sqlite.dialect()).compile() == "TEXT"
    assert payload_type.dialect_impl(mysql.dialect()).compile() == "LONGTEXT"
