"""Small, source-aware data model for the independent memory prototype.

The models in this module deliberately describe the thin second-batch path.  Mem0
owns persistence identifiers and timestamps; LangMem owns the temporary
structured extraction objects; this module keeps the two contracts separate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SourceRole = Literal["user", "assistant", "tool", "system"]
WriteAction = Literal["skip", "add", "update"]


def utc_now() -> datetime:
    """Return an aware UTC timestamp for program-recorded events."""

    return datetime.now(timezone.utc)


def _non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


def _aware_datetime(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone; use None when unknown")
    return value


def stable_unique(values: Iterable[str]) -> list[str]:
    """Deduplicate identifiers while preserving their first occurrence."""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = _non_empty(value, "identifier")
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


class MemoryScope(BaseModel):
    """The application-owned isolation boundary for a memory operation."""

    model_config = ConfigDict(extra="forbid")

    scope_type: Literal["user", "project"]
    scope_id: str = Field(min_length=1)

    @field_validator("scope_id")
    @classmethod
    def validate_scope_id(cls, value: str) -> str:
        value = _non_empty(value, "scope_id")
        if any(character.isspace() for character in value):
            raise ValueError("scope_id must not contain whitespace")
        return value


class SourceMessage(BaseModel):
    """A caller-owned message that can support a memory."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    role: SourceRole
    content: str = Field(min_length=1)
    observed_at: datetime | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "source message id")

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return _non_empty(value, "source message content")

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime | None) -> datetime | None:
        return _aware_datetime(value, "observed_at")

    def to_source_ref(self) -> "SourceRef":
        return SourceRef(id=self.id, role=self.role, observed_at=self.observed_at)


class SourceRef(BaseModel):
    """The minimum persisted provenance for one source message."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    role: SourceRole
    observed_at: datetime | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "source reference id")

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime | None) -> datetime | None:
        return _aware_datetime(value, "observed_at")


class MemoryContent(BaseModel):
    """Insert ONE new standalone memory, including a changed state or correction.

    For a state change or explicit correction, use this tool with supersedes_ids
    pointing to the old editable memory; keep that old document unchanged.
    To enrich a still-correct existing fact, use PatchDoc instead of this tool.
    Different independently useful attributes need separate calls to this tool.
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(
        min_length=1,
        description=(
            "One standalone, reusable memory statement. Preserve corrections "
            "(what earlier claim was wrong), uncertainty, who suggested/confirmed "
            "something, and what was unknown before clarification, including on update."
        ),
    )
    source_ids: list[str] = Field(
        min_length=1,
        description="IDs from the source directory visible to the manager.",
    )
    valid_from: str | None = Field(
        default=None,
        description="Only an explicitly supported fact-time value; otherwise null.",
    )
    valid_to: str | None = Field(
        default=None,
        description="Only an explicitly supported fact-time value; otherwise null.",
    )
    supersedes_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Temporary Mem0 IDs from editable_memory_ids, only for an actual "
            "state replacement or explicit correction. Empty for enrichment, "
            "unconfirmed suggestions/plans, and independent tool events. "
            "Never persisted in the memory body or metadata."
        ),
    )

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return _non_empty(value, "memory content")

    @field_validator("source_ids", "supersedes_ids", mode="before")
    @classmethod
    def normalize_id_lists(cls, value: Sequence[str] | None) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (str, bytes)):
            raise TypeError("identifier fields must be lists")
        return stable_unique(value)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def normalize_optional_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _non_empty(value, "fact time")

    @model_validator(mode="after")
    def validate_comparable_time_order(self) -> "MemoryContent":
        """Reject an obviously reversed pair without inventing semantics for free text."""
        if self.valid_from is None or self.valid_to is None:
            return self
        if len(self.valid_from) != len(self.valid_to):
            return self
        try:
            from_value = datetime.fromisoformat(self.valid_from.replace("Z", "+00:00"))
            to_value = datetime.fromisoformat(self.valid_to.replace("Z", "+00:00"))
        except ValueError:
            return self
        if from_value > to_value:
            raise ValueError("valid_from must not be later than valid_to")
        return self


class ExistingMemory(BaseModel):
    """A Mem0 candidate after provider metadata has been decoded and checked."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="The actual Mem0 memory ID.")
    content: MemoryContent
    source_refs: list[SourceRef] = Field(min_length=1)
    score: float | None = None
    recorded_at: datetime | None = None
    created_at: str | None = None
    updated_at: str | None = None
    superseded_by: str | None = None
    # ``None`` means the legacy key was absent.  An empty list means an old
    # record explicitly stored an empty compatibility value; this distinction
    # lets updates preserve old metadata without adding the key to new data.
    legacy_related_memory_ids: list[str] | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "memory id")

    @field_validator("recorded_at")
    @classmethod
    def validate_recorded_at(cls, value: datetime | None) -> datetime | None:
        return _aware_datetime(value, "recorded_at")


class WriteEvent(BaseModel):
    """One actual or deliberately skipped action in a write batch."""

    model_config = ConfigDict(extra="forbid")

    action: WriteAction
    memory_id: str | None = None
    manager_id: str | None = None
    content: str | None = None
    reason: str


class WriteSummary(BaseModel):
    """Observable result of one serialized write batch."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    scope: MemoryScope
    candidate_ids: list[str] = Field(default_factory=list)
    manager_result_count: int = 0
    events: list[WriteEvent] = Field(default_factory=list)
    added_ids: list[str] = Field(default_factory=list)
    updated_ids: list[str] = Field(default_factory=list)
    skipped_ids: list[str] = Field(default_factory=list)
    failed_actions: list[str] = Field(default_factory=list)
    unknown_actions: list[str] = Field(default_factory=list)
    unattempted_actions: list[str] = Field(default_factory=list)


class ForgetItem(BaseModel):
    """The observable result for one requested deletion."""

    memory_id: str
    status: Literal["deleted", "missing", "failed", "unknown"]
    reason: str | None = None


class ForgetSummary(BaseModel):
    """A scope-checked deletion batch; it is not a claim about backups/history."""

    batch_id: str
    scope: MemoryScope
    requested_ids: list[str] = Field(default_factory=list)
    items: list[ForgetItem] = Field(default_factory=list)


def canonical_memory_content(value: MemoryContent | dict) -> MemoryContent:
    """Normalize manager/provider output for comparison and persistence."""

    memory = value if isinstance(value, MemoryContent) else MemoryContent.model_validate(value)
    return MemoryContent(
        content=memory.content,
        source_ids=sorted(set(memory.source_ids)),
        valid_from=memory.valid_from,
        valid_to=memory.valid_to,
        supersedes_ids=sorted(set(memory.supersedes_ids)),
    )


def memory_content_signature(value: MemoryContent | dict) -> tuple:
    """A deterministic full-object signature; list ordering is not semantic."""

    memory = canonical_memory_content(value)
    return (
        memory.content,
        tuple(memory.source_ids),
        memory.valid_from,
        memory.valid_to,
        tuple(memory.supersedes_ids),
    )


def memory_body_signature(value: MemoryContent | dict) -> tuple:
    """A signature used only to prevent an unchanged object becoming a new one."""

    memory = canonical_memory_content(value)
    return (
        memory.content,
        memory.valid_from,
        memory.valid_to,
        tuple(memory.supersedes_ids),
    )


def merge_source_ids(*values: Iterable[str]) -> list[str]:
    """Merge source IDs deterministically for a managed record."""

    merged: list[str] = []
    for value in values:
        merged.extend(value)
    return sorted(set(stable_unique(merged)))
