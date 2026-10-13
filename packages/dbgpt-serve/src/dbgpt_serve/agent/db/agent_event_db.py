"""Database persistence for the ordered Agent conversation event log."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    inspect,
    text,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Session

from dbgpt.agent.core.rollout import (
    ChatCompletionHistoryProjection,
    RolloutEvent,
    project_rollout_events,
)
from dbgpt.storage.chat_history.chat_history_db import ChatHistoryEntity
from dbgpt.storage.metadata import BaseDao, Model, ensure_column

_LARGE_TEXT = Text().with_variant(LONGTEXT(), "mysql")
_EVENT_TYPES = {
    "turn_started",
    "user_message",
    "assistant_message",
    "tool_result",
    "turn_completed",
    "turn_aborted",
}


class AgentEventIdempotencyConflict(ValueError):
    """Raised when an event ID is reused for different event data."""


class ConversationRowNotFound(LookupError):
    """Raised when events are appended before the conversation row exists."""


class ActiveTurnInProgress(RuntimeError):
    """Raised when another live request owns the conversation turn lease."""


class TurnLeaseLost(RuntimeError):
    """Raised when a worker tries to write after its turn lease was fenced out."""


@dataclass(frozen=True)
class AgentTurnLease:
    """Fenced ownership token for one in-flight user turn."""

    conv_id: str
    turn_id: str
    fence: int
    lease_until: datetime


@dataclass(frozen=True)
class AgentEventDraft:
    """One event prepared for durable append.

    Create the draft before attempting persistence and retain it across a retry;
    its event ID is the idempotency key for that logical event.
    """

    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    event_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class StoredAgentEvent:
    """Detached value object returned by append/read operations."""

    event_id: str
    conv_id: str
    event_seq: int
    turn_id: str
    event_type: str
    schema_version: int
    payload: dict[str, Any]
    created_at: datetime

    def to_rollout_event(self) -> RolloutEvent:
        """Return the storage-independent event consumed by the projector."""
        return RolloutEvent(event_type=self.event_type, payload=self.payload)


class AgentEventEntity(Model):
    """An immutable protocol/lifecycle fact in the conversation rollout."""

    __tablename__ = "agent_event"
    __table_args__ = (
        UniqueConstraint("conv_id", "event_seq", name="uk_agent_event_conv_seq"),
    )

    event_id = Column(String(36), primary_key=True, comment="Stable idempotency ID")
    conv_id = Column(String(255), nullable=False, comment="Conversation ID")
    event_seq = Column(BigInteger, nullable=False, comment="Conversation event order")
    turn_id = Column(String(64), nullable=False, comment="User turn grouping ID")
    event_type = Column(String(40), nullable=False, comment="Closed event type")
    schema_version = Column(Integer, nullable=False, comment="Payload schema version")
    payload = Column(_LARGE_TEXT, nullable=False, comment="JSON event payload")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AgentEventDao(BaseDao):
    """Append and read ordered conversation events.

    Sequence allocation is protected by the existing chat_history row:
    SQLite takes a short BEGIN IMMEDIATE write reservation, while databases
    with row locks use SELECT FOR UPDATE. The lock, MAX+1 allocation,
    and event inserts share one transaction. No second event/turn table is used.
    """

    def append_event(
        self,
        conv_id: str,
        turn_id: str,
        event: AgentEventDraft,
        *,
        lease: Optional[AgentTurnLease] = None,
    ) -> StoredAgentEvent:
        """Append one event and return its committed sequence."""
        return self.append_events(conv_id, turn_id, [event], lease=lease)[0]

    def append_events(
        self,
        conv_id: str,
        turn_id: str,
        events: Sequence[AgentEventDraft],
        *,
        lease: Optional[AgentTurnLease] = None,
    ) -> list[StoredAgentEvent]:
        """Append a batch atomically, assigning consecutive conversation seqs.

        Replaying the exact same full batch with the same event IDs is
        idempotent. Partial overlap or reusing an ID for different data fails
        closed instead of silently skipping or duplicating events.
        """
        if not conv_id:
            raise ValueError("conv_id must be non-empty")
        if not turn_id:
            raise ValueError("turn_id must be non-empty")
        if not events:
            return []

        self._ensure_turn_lease_columns()
        prepared = [self._prepare_draft(event) for event in events]
        event_ids = [item[0] for item in prepared]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("event IDs in a batch must be unique")

        with self._db_manager.session() as session:
            conversation = self._lock_conversation_for_append(session, conv_id)
            if lease is not None:
                self._validate_active_lease(conversation, lease, datetime.utcnow())

            existing_rows = (
                session.query(AgentEventEntity)
                .filter(AgentEventEntity.event_id.in_(event_ids))
                .all()
            )
            if existing_rows:
                if len(existing_rows) != len(prepared):
                    raise AgentEventIdempotencyConflict(
                        "an append batch only partially matches existing event IDs"
                    )
                existing_by_id = {row.event_id: row for row in existing_rows}
                for event_id, event_type, version, payload_json in prepared:
                    row = existing_by_id[event_id]
                    if (
                        row.conv_id != conv_id
                        or row.turn_id != turn_id
                        or row.event_type != event_type
                        or row.schema_version != version
                        or row.payload != payload_json
                    ):
                        raise AgentEventIdempotencyConflict(
                            f"event ID {event_id} already stores different data"
                        )
                return [
                    self._to_stored(existing_by_id[event_id]) for event_id in event_ids
                ]

            latest_seq = (
                session.query(func.max(AgentEventEntity.event_seq))
                .filter(AgentEventEntity.conv_id == conv_id)
                .scalar()
                or 0
            )
            rows = [
                AgentEventEntity(
                    event_id=event_id,
                    conv_id=conv_id,
                    event_seq=latest_seq + offset,
                    turn_id=turn_id,
                    event_type=event_type,
                    schema_version=version,
                    payload=payload_json,
                )
                for offset, (event_id, event_type, version, payload_json) in enumerate(
                    prepared, start=1
                )
            ]
            session.add_all(rows)
            session.flush()
            # Copy before the context manager commits/closes the session.
            return [self._to_stored(row) for row in rows]

    def acquire_turn_lease(
        self,
        conv_id: str,
        turn_id: str,
        *,
        lease_seconds: int = 90,
        now: Optional[datetime] = None,
    ) -> AgentTurnLease:
        """Claim the conversation's single active turn with a fenced lease.

        The lease lives on the pre-existing ``chat_history`` row; it is only
        coordination metadata, not a second transcript or turn table. Expired
        owners may be replaced after a process crash. Every event append from
        an AgentRolloutSession checks the fence under the same conversation-row
        lock, so a stale worker cannot continue writing after takeover.
        """
        if not conv_id or not turn_id:
            raise ValueError("conv_id and turn_id must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._ensure_turn_lease_columns()
        current_time = now or datetime.utcnow()
        expires_at = current_time + timedelta(seconds=lease_seconds)
        with self._db_manager.session() as session:
            conversation = self._lock_conversation_for_append(session, conv_id)
            active_turn_id = conversation.active_agent_turn_id
            active_until = conversation.active_agent_turn_lease_until
            if active_turn_id and (active_until is None or active_until > current_time):
                raise ActiveTurnInProgress(
                    f"conversation {conv_id!r} already has an active turn"
                )
            fence = int(conversation.active_agent_turn_fence or 0) + 1
            conversation.active_agent_turn_id = turn_id
            conversation.active_agent_turn_lease_until = expires_at
            conversation.active_agent_turn_fence = fence
            session.flush()
            return AgentTurnLease(conv_id, turn_id, fence, expires_at)

    def renew_turn_lease(
        self,
        lease: AgentTurnLease,
        *,
        lease_seconds: int = 90,
        now: Optional[datetime] = None,
    ) -> AgentTurnLease:
        """Extend a still-live lease, rejecting expired or superseded owners."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._ensure_turn_lease_columns()
        current_time = now or datetime.utcnow()
        expires_at = current_time + timedelta(seconds=lease_seconds)
        with self._db_manager.session() as session:
            conversation = self._lock_conversation_for_append(session, lease.conv_id)
            self._validate_active_lease(conversation, lease, current_time)
            conversation.active_agent_turn_lease_until = expires_at
            session.flush()
        return AgentTurnLease(lease.conv_id, lease.turn_id, lease.fence, expires_at)

    def release_turn_lease(self, lease: AgentTurnLease) -> bool:
        """Release only the matching owner; stale cleanup cannot release a new lease."""
        self._ensure_turn_lease_columns()
        with self._db_manager.session() as session:
            conversation = self._lock_conversation_for_append(session, lease.conv_id)
            if (
                conversation.active_agent_turn_id != lease.turn_id
                or int(conversation.active_agent_turn_fence or 0) != lease.fence
            ):
                return False
            conversation.active_agent_turn_id = None
            conversation.active_agent_turn_lease_until = None
            session.flush()
            return True

    def assert_turn_lease(self, lease: AgentTurnLease) -> None:
        """Fail if this worker no longer owns a live turn lease."""
        self._ensure_turn_lease_columns()
        current_time = datetime.utcnow()
        with self._db_manager.session() as session:
            conversation = self._lock_conversation_for_append(session, lease.conv_id)
            self._validate_active_lease(conversation, lease, current_time)

    def list_by_conv_id(
        self,
        conv_id: str,
        *,
        after_seq: int = 0,
        limit: Optional[int] = None,
    ) -> list[StoredAgentEvent]:
        """Read committed events strictly in their persisted sequence order."""
        if not conv_id:
            raise ValueError("conv_id must be non-empty")
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")

        with self._db_manager.session(commit=False) as session:
            query = (
                session.query(AgentEventEntity)
                .filter(
                    AgentEventEntity.conv_id == conv_id,
                    AgentEventEntity.event_seq > after_seq,
                )
                .order_by(AgentEventEntity.event_seq.asc())
            )
            if limit is not None:
                query = query.limit(limit)
            return [self._to_stored(row) for row in query.all()]

    @staticmethod
    def _lock_conversation_for_append(
        session: Session, conv_id: str
    ) -> ChatHistoryEntity:
        """Acquire and return the conversation row used for append/lease fencing."""
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "sqlite":
            # SQLite ignores SELECT FOR UPDATE. Acquire its single-writer slot
            # before reading MAX(event_seq), for this brief append transaction.
            session.execute(text("BEGIN IMMEDIATE"))
            query = session.query(ChatHistoryEntity).filter(
                ChatHistoryEntity.conv_uid == conv_id
            )
        else:
            query = (
                session.query(ChatHistoryEntity)
                .filter(ChatHistoryEntity.conv_uid == conv_id)
                .with_for_update()
            )
        conversation = query.first()
        if conversation is None:
            raise ConversationRowNotFound(
                f"conversation {conv_id!r} must exist before appending events"
            )
        return conversation

    @staticmethod
    def _validate_active_lease(
        conversation: ChatHistoryEntity,
        lease: AgentTurnLease,
        now: datetime,
    ) -> None:
        if (
            conversation.conv_uid != lease.conv_id
            or conversation.active_agent_turn_id != lease.turn_id
            or int(conversation.active_agent_turn_fence or 0) != lease.fence
            or conversation.active_agent_turn_lease_until is None
            or conversation.active_agent_turn_lease_until <= now
        ):
            raise TurnLeaseLost(
                f"turn lease for conversation {lease.conv_id!r} is expired or fenced"
            )

    def _ensure_turn_lease_columns(self) -> None:
        """Add nullable coordination columns to existing metadata databases."""
        columns = (
            ChatHistoryEntity.active_agent_turn_id,
            ChatHistoryEntity.active_agent_turn_lease_until,
            ChatHistoryEntity.active_agent_turn_fence,
        )
        for column in columns:
            ensure_column(
                self._db_manager,
                ChatHistoryEntity.__tablename__,
                column,
            )
        if self._db_manager.engine is None:
            raise RuntimeError("metadata database engine is not initialized")
        existing = {
            item["name"]
            for item in inspect(self._db_manager.engine).get_columns(
                ChatHistoryEntity.__tablename__
            )
        }
        missing = [column.name for column in columns if column.name not in existing]
        if missing:
            raise RuntimeError(
                "could not initialize Agent turn lease columns: "
                + ", ".join(missing)
            )

    @staticmethod
    def _prepare_draft(
        event: AgentEventDraft,
    ) -> tuple[str, str, int, str]:
        if event.event_type not in _EVENT_TYPES:
            raise ValueError(f"unsupported event type: {event.event_type!r}")
        if not isinstance(event.schema_version, int) or event.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        try:
            event_id = str(UUID(event.event_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("event_id must be a UUID string") from exc
        if not isinstance(event.payload, Mapping):
            raise ValueError("event payload must be a JSON object")
        AgentEventDao._validate_payload(event.event_type, event.payload)
        try:
            payload_json = json.dumps(
                dict(event.payload),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("event payload must be JSON-serializable") from exc
        return event_id, event.event_type, event.schema_version, payload_json

    @staticmethod
    def _validate_payload(event_type: str, payload: Mapping[str, Any]) -> None:
        """Reject events that cannot be projected back into protocol messages."""
        if event_type == "user_message":
            if not isinstance(payload.get("content"), str):
                raise ValueError("user_message payload must contain string 'content'")
        elif event_type == "assistant_message":
            content = payload.get("content")
            tool_calls = payload.get("tool_calls") or []
            if content is not None and not isinstance(content, str):
                raise ValueError("assistant_message 'content' must be a string or null")
            if not isinstance(tool_calls, list):
                raise ValueError("assistant_message 'tool_calls' must be a list")
            if content is None and not tool_calls:
                raise ValueError(
                    "assistant_message without tool_calls must contain 'content'"
                )
            call_ids: set[str] = set()
            for tool_call in tool_calls:
                if not isinstance(tool_call, Mapping):
                    raise ValueError("each assistant tool_call must be an object")
                call_id = tool_call.get("id")
                function = tool_call.get("function")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError("each assistant tool_call needs a non-empty id")
                if call_id in call_ids:
                    raise ValueError(f"duplicate tool_call id: {call_id}")
                if not isinstance(function, Mapping) or not isinstance(
                    function.get("name"), str
                ):
                    raise ValueError("each assistant tool_call needs a function name")
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    raise ValueError("tool_call function arguments must be a string")
                call_ids.add(call_id)
        elif event_type == "tool_result":
            if not isinstance(payload.get("tool_call_id"), str) or not payload.get(
                "tool_call_id"
            ):
                raise ValueError("tool_result needs a non-empty 'tool_call_id'")
            if not isinstance(payload.get("content"), str):
                raise ValueError("tool_result payload must contain string 'content'")
            if "success" in payload and not isinstance(payload["success"], bool):
                raise ValueError("tool_result 'success' must be a boolean")

    @staticmethod
    def _to_stored(row: AgentEventEntity) -> StoredAgentEvent:
        try:
            payload = json.loads(row.payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON payload for event {row.event_id}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"event {row.event_id} payload must be a JSON object")
        return StoredAgentEvent(
            event_id=row.event_id,
            conv_id=row.conv_id,
            event_seq=row.event_seq,
            turn_id=row.turn_id,
            event_type=row.event_type,
            schema_version=row.schema_version,
            payload=payload,
            created_at=row.created_at,
        )


class AgentRolloutSession:
    """Small conversation-scoped facade for appending and projecting events.

    Callers must check conversation ownership before reading. ``start_turn``
    acquires a fenced single-active-turn lease on the existing conversation row;
    that coordination state is separate from the sole ``agent_event`` transcript.
    """

    def __init__(
        self,
        conv_id: str,
        turn_id: str,
        dao: Optional[AgentEventDao] = None,
    ) -> None:
        if not conv_id or not turn_id:
            raise ValueError("conv_id and turn_id must be non-empty")
        self.conv_id = conv_id
        self.turn_id = turn_id
        self.dao = dao or AgentEventDao()
        self.lease: Optional[AgentTurnLease] = None

    def append(self, event_type: str, payload: Mapping[str, Any]) -> StoredAgentEvent:
        return self.dao.append_event(
            self.conv_id,
            self.turn_id,
            AgentEventDraft(event_type, payload),
            lease=self.lease,
        )

    def start_turn(self, user_content: str) -> list[StoredAgentEvent]:
        self.lease = self.dao.acquire_turn_lease(self.conv_id, self.turn_id)
        try:
            return self.dao.append_events(
                self.conv_id,
                self.turn_id,
                [
                    AgentEventDraft("turn_started", {}),
                    AgentEventDraft("user_message", {"content": user_content}),
                ],
                lease=self.lease,
            )
        except Exception:
            self.dao.release_turn_lease(self.lease)
            self.lease = None
            raise

    def renew_turn_lease(self) -> None:
        if self.lease is None:
            raise TurnLeaseLost("this rollout session has no active turn lease")
        self.lease = self.dao.renew_turn_lease(self.lease)

    def release_turn_lease(self) -> bool:
        if self.lease is None:
            return False
        lease = self.lease
        self.lease = None
        return self.dao.release_turn_lease(lease)

    def assert_turn_lease(self) -> None:
        if self.lease is None:
            raise TurnLeaseLost("this rollout session has no active turn lease")
        self.dao.assert_turn_lease(self.lease)

    def load_projection(self) -> ChatCompletionHistoryProjection:
        events = self.dao.list_by_conv_id(self.conv_id)
        sequences = [event.event_seq for event in events]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError(f"conversation {self.conv_id!r} has a sequence gap")
        return project_rollout_events(event.to_rollout_event() for event in events)
