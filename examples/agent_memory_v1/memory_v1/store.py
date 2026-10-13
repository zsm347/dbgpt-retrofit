"""Mem0 2.2.1 storage adapter for the independent prototype."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from mem0 import AsyncMemory
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .schema import (
    ExistingMemory,
    MemoryContent,
    MemoryScope,
    SourceRef,
    canonical_memory_content,
    utc_now,
)


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class MemoryStoreConfig(BaseModel):
    """Explicit/env-backed runtime configuration; no credential has a default."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    api_key: SecretStr | None = Field(default=None, repr=False)
    base_url: str = DEFAULT_BASE_URL
    chat_model: str = "deepseek-v4.1-flash"
    # DeepSeek-V4.1-Flash supports the function-calling/structured-output
    # path used by LangMem/TrustCall. Keep its thinking mode enabled by
    # default; callers can explicitly override it through the environment.
    enable_thinking: bool = True
    embedding_model: str = "text-embedding-v3"
    embedding_dims: int = Field(default=1024, gt=0)
    collection_name: str = "agent_memory_v1"
    data_dir: Path = DEFAULT_DATA_DIR
    qdrant_path: Path | None = None
    history_db_path: Path | None = None
    write_candidate_limit: int = Field(default=10, ge=1)
    recent_limit: int = Field(default=10, ge=1)

    @field_validator("base_url", "chat_model", "embedding_model", "collection_name")
    @classmethod
    def validate_text_config(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("configuration values must not be empty")
        return value

    @field_validator("data_dir", "qdrant_path", "history_db_path", mode="before")
    @classmethod
    def expand_paths(cls, value: Path | str | None) -> Path | None:
        if value is None:
            return None
        return Path(value).expanduser()

    @classmethod
    def from_env(cls, **overrides: Any) -> "MemoryStoreConfig":
        """Read configuration from explicit overrides and documented env vars."""

        data_dir = Path(
            overrides.pop("data_dir", os.getenv("AGENT_MEMORY_DATA_DIR", str(DEFAULT_DATA_DIR)))
        ).expanduser()
        values: dict[str, Any] = {
            "api_key": overrides.pop("api_key", os.getenv("DASHSCOPE_API_KEY")),
            "base_url": overrides.pop(
                "base_url", os.getenv("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL)
            ),
            "chat_model": overrides.pop(
                "chat_model", os.getenv("AGENT_MEMORY_CHAT_MODEL", "deepseek-v4.1-flash")
            ),
            "enable_thinking": str(
                overrides.pop(
                    "enable_thinking", os.getenv("AGENT_MEMORY_ENABLE_THINKING", "1")
                )
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            "embedding_model": overrides.pop(
                "embedding_model",
                os.getenv("AGENT_MEMORY_EMBEDDING_MODEL", "text-embedding-v3"),
            ),
            "embedding_dims": int(
                overrides.pop(
                    "embedding_dims", os.getenv("AGENT_MEMORY_EMBEDDING_DIMS", "1024")
                )
            ),
            "collection_name": overrides.pop(
                "collection_name", os.getenv("AGENT_MEMORY_COLLECTION", "agent_memory_v1")
            ),
            "data_dir": data_dir,
            "qdrant_path": overrides.pop(
                "qdrant_path", os.getenv("AGENT_MEMORY_QDRANT_PATH", str(data_dir / "qdrant"))
            ),
            "history_db_path": overrides.pop(
                "history_db_path",
                os.getenv("AGENT_MEMORY_HISTORY_DB_PATH", str(data_dir / "history.db")),
            ),
            "write_candidate_limit": int(
                overrides.pop(
                    "write_candidate_limit",
                    os.getenv("AGENT_MEMORY_WRITE_CANDIDATE_LIMIT", "10"),
                )
            ),
            "recent_limit": int(
                overrides.pop(
                    "recent_limit", os.getenv("AGENT_MEMORY_RECENT_LIMIT", "10")
                )
            ),
        }
        values.update(overrides)
        return cls(**values)

    def require_api_key(self) -> str:
        if self.api_key is None or not self.api_key.get_secret_value().strip():
            raise RuntimeError(
                "No model API key configured. Set DASHSCOPE_API_KEY or pass api_key explicitly."
            )
        return self.api_key.get_secret_value()

    def resolved_qdrant_path(self) -> Path:
        return (self.qdrant_path or self.data_dir / "qdrant").expanduser().resolve()

    def resolved_history_db_path(self) -> Path:
        return (self.history_db_path or self.data_dir / "history.db").expanduser().resolve()

    def to_mem0_config(self) -> dict[str, Any]:
        """Build the exact Mem0 2.2.1 configuration without logging secrets."""

        api_key = self.require_api_key()
        self.resolved_qdrant_path().parent.mkdir(parents=True, exist_ok=True)
        self.resolved_history_db_path().parent.mkdir(parents=True, exist_ok=True)
        return {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": self.chat_model,
                    "api_key": api_key,
                    "openai_base_url": self.base_url,
                    "temperature": 0,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": self.embedding_model,
                    "api_key": api_key,
                    "openai_base_url": self.base_url,
                    "embedding_dims": self.embedding_dims,
                },
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": self.collection_name,
                    "path": str(self.resolved_qdrant_path()),
                    "embedding_model_dims": self.embedding_dims,
                },
            },
            "history_db_path": str(self.resolved_history_db_path()),
        }


def create_chat_model(config: MemoryStoreConfig):
    """Construct LangMem's LangChain chat model from the same explicit config."""

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=config.chat_model,
        api_key=config.require_api_key(),
        base_url=config.base_url,
        temperature=0,
        extra_body={"enable_thinking": config.enable_thinking},
    )


def create_async_memory(config: MemoryStoreConfig) -> AsyncMemory:
    """Construct the fixed Mem0 async client using local Qdrant persistence."""

    # Mem0 2.2.1 lazily asks for its optional spaCy model from both the
    # entity extractor and the BM25 lemmatizer.  DB-GPT already has spaCy in
    # some root environments, so Mem0 can otherwise start a network download
    # even though this prototype deliberately does not enable the optional
    # NLP extras.  Keep the boundary explicit: the core path is semantic
    # retrieval, and installing/enabling NLP is a separate experiment.
    if os.getenv("AGENT_MEMORY_ENABLE_NLP", "0").lower() not in {"1", "true", "yes"}:
        from mem0.utils import spacy_models

        spacy_models.get_nlp_full = lambda: None
        spacy_models.get_nlp_lemma = lambda: None

    return AsyncMemory.from_config(config.to_mem0_config())


def scope_key(scope: MemoryScope) -> str:
    """Map an application scope to one deterministic Mem0 user_id."""

    return f"{scope.scope_type}:{scope.scope_id}"


def _json_string(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def encode_metadata(
    *,
    scope: MemoryScope,
    content: MemoryContent,
    source_refs: Sequence[SourceRef],
    recorded_at: datetime,
    superseded_by: str | None = None,
    legacy_related_memory_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Encode the managed fields as scalar metadata values accepted by Mem0."""

    memory = canonical_memory_content(content)
    ref_by_id = {ref.id: ref for ref in source_refs}
    missing = sorted(set(memory.source_ids) - set(ref_by_id))
    if missing:
        raise ValueError(
            "source_refs is missing IDs required by the memory: " + ", ".join(missing)
        )
    ordered_refs = [ref_by_id[source_id] for source_id in memory.source_ids]
    encoded = {
        "scope_type": scope.scope_type,
        "scope_id": scope.scope_id,
        "source_refs_json": _json_string(
            [ref.model_dump(mode="json") for ref in ordered_refs]
        ),
        # These keys are deliberately sent even when their value is None on update.
        "valid_from": memory.valid_from,
        "valid_to": memory.valid_to,
        "recorded_at": recorded_at.isoformat(),
        "superseded_by": superseded_by,
    }
    # Old first-batch records may have this compatibility field.  Preserve it
    # when updating such a record, but never create it for new records.
    if legacy_related_memory_ids is not None:
        encoded["related_memory_ids_json"] = _json_string(legacy_related_memory_ids)
    return encoded


def _decode_json(value: Any, field_name: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON metadata in {field_name}") from exc
    if isinstance(value, (list, dict)):
        # This is tolerant of a provider returning already-decoded metadata,
        # while all writes still use the documented JSON-string representation.
        return value
    raise ValueError(f"missing or invalid metadata field {field_name}")


def _decode_record(raw: Mapping[str, Any], expected_scope: MemoryScope) -> ExistingMemory:
    raw_id = raw.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ValueError("Mem0 record has no valid string ID")
    expected_key = scope_key(expected_scope)
    actual_key = raw.get("user_id")
    if actual_key != expected_key:
        raise PermissionError(
            f"Mem0 record {raw_id!r} belongs to {actual_key!r}, not {expected_key!r}"
        )

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise ValueError(f"Mem0 record {raw_id!r} has non-object metadata")
    if metadata.get("scope_type") != expected_scope.scope_type:
        raise PermissionError("Mem0 record scope_type does not match the requested scope")
    if metadata.get("scope_id") != expected_scope.scope_id:
        raise PermissionError("Mem0 record scope_id does not match the requested scope")

    raw_refs = _decode_json(metadata.get("source_refs_json"), "source_refs_json")
    if not isinstance(raw_refs, list) or not raw_refs:
        raise ValueError(f"Mem0 record {raw_id!r} has no source references")
    source_refs = [SourceRef.model_validate(item) for item in raw_refs]
    source_ids = sorted({source.id for source in source_refs})

    raw_related: list[str] | None = None
    if "related_memory_ids_json" in metadata:
        decoded_related = _decode_json(
            metadata["related_memory_ids_json"], "related_memory_ids_json"
        )
        if not isinstance(decoded_related, list):
            raise ValueError("related_memory_ids_json must contain a JSON list")
        raw_related = decoded_related
    content = MemoryContent(
        content=raw.get("memory") or raw.get("data") or "",
        source_ids=source_ids,
        valid_from=metadata.get("valid_from"),
        valid_to=metadata.get("valid_to"),
        supersedes_ids=[],
    )

    recorded_at_value = metadata.get("recorded_at")
    if not isinstance(recorded_at_value, str):
        raise ValueError(f"Mem0 record {raw_id!r} has no recorded_at metadata")
    try:
        recorded_at = datetime.fromisoformat(recorded_at_value)
    except ValueError as exc:
        raise ValueError(f"invalid recorded_at for Mem0 record {raw_id!r}") from exc
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError(f"recorded_at for Mem0 record {raw_id!r} has no timezone")

    return ExistingMemory(
        id=raw_id,
        content=canonical_memory_content(content),
        source_refs=source_refs,
        score=raw.get("score"),
        recorded_at=recorded_at,
        created_at=raw.get("created_at"),
        updated_at=raw.get("updated_at"),
        superseded_by=metadata.get("superseded_by"),
        legacy_related_memory_ids=raw_related,
    )


class MemoryStore:
    """Thin async wrapper around Mem0's public AsyncMemory methods."""

    def __init__(self, memory: Any, config: MemoryStoreConfig):
        self.memory = memory
        self.config = config

    @classmethod
    def from_config(cls, config: MemoryStoreConfig | None = None) -> "MemoryStore":
        config = config or MemoryStoreConfig.from_env()
        return cls(create_async_memory(config), config)

    async def search(
        self, scope: MemoryScope, query: str, *, top_k: int | None = None
    ) -> list[ExistingMemory]:
        """Search only the requested scope using the public AsyncMemory.search."""

        effective_top_k = self.config.write_candidate_limit if top_k is None else top_k
        if effective_top_k < 1:
            raise ValueError("top_k must be positive")
        response = await self.memory.search(
            query=query,
            filters={"user_id": scope_key(scope)},
            top_k=effective_top_k,
            rerank=False,
        )
        raw_results = response.get("results") if isinstance(response, Mapping) else None
        if not isinstance(raw_results, list):
            raise RuntimeError("Mem0 search returned no list-valued 'results' collection")
        return [_decode_record(item, scope) for item in raw_results]

    async def get(self, scope: MemoryScope, memory_id: str) -> ExistingMemory | None:
        """Read a record by actual Mem0 ID and enforce the requested scope."""

        raw = await self.memory.get(memory_id)
        if raw is None:
            return None
        return _decode_record(raw, scope)

    async def add(
        self,
        scope: MemoryScope,
        content: MemoryContent,
        source_refs: Sequence[SourceRef],
        *,
        recorded_at: datetime | None = None,
    ) -> str:
        """Store exactly one already-structured memory with infer=False."""

        memory = canonical_memory_content(content)
        response = await self.memory.add(
            memory.content,
            user_id=scope_key(scope),
            metadata=encode_metadata(
                scope=scope,
                content=memory,
                source_refs=source_refs,
                recorded_at=recorded_at or utc_now(),
                superseded_by=None,
            ),
            infer=False,
        )
        raw_results = response.get("results") if isinstance(response, Mapping) else None
        if not isinstance(raw_results, list) or len(raw_results) != 1:
            raise RuntimeError("Mem0 add(infer=False) did not return exactly one result")
        memory_id = raw_results[0].get("id") if isinstance(raw_results[0], Mapping) else None
        if not isinstance(memory_id, str) or not memory_id:
            raise RuntimeError("Mem0 add(infer=False) returned no actual memory ID")
        return memory_id

    async def update(
        self,
        scope: MemoryScope,
        memory_id: str,
        content: MemoryContent,
        source_refs: Sequence[SourceRef],
        *,
        superseded_by: str | None = None,
        preserve_superseded_by: bool = True,
    ) -> str:
        """Update only an existing, scope-verified record with complete metadata."""

        existing = await self.get(scope, memory_id)
        if existing is None:
            raise ValueError(f"memory {memory_id!r} no longer exists in the requested scope")
        await self.memory.update(
            memory_id,
            text=canonical_memory_content(content).content,
            metadata=encode_metadata(
                scope=scope,
                content=content,
                source_refs=source_refs,
                recorded_at=existing.recorded_at or utc_now(),
                superseded_by=(
                    existing.superseded_by
                    if preserve_superseded_by and superseded_by is None
                    else superseded_by
                ),
                legacy_related_memory_ids=existing.legacy_related_memory_ids,
            ),
        )
        return memory_id

    async def delete(self, scope: MemoryScope, memory_id: str) -> str:
        """Delete one scope-verified record; return ``missing`` when absent."""

        existing = await self.get(scope, memory_id)
        if existing is None:
            return "missing"
        await self.memory.delete(memory_id)
        return "deleted"
