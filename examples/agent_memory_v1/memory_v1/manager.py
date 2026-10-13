"""Fixed-version LangMem manager adapter.

Prompt preparation and per-call source constraints are adapted. Trustcall's
structured tool calls, JSON patch application, and validation repair stay upstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage
from langmem import utils
from langmem.knowledge.extraction import Done, ExtractedMemory, MemoryManager
from pydantic import Field, create_model, field_validator

from .prompts import MEMORY_INSTRUCTIONS
from .schema import ExistingMemory, MemoryContent, canonical_memory_content


class SourceAwareMemoryManager(MemoryManager):
    """MemoryManager with explicit source-aware input sections.

    LangMem 0.0.30 appends a generic instruction to update incorrect/outdated
    memories.  That conflicts with this prototype's keep-old-record behavior
    for state changes and corrections, so this small override keeps only the
    caller's instructions and the serialized input partitions.
    """

    def _prepare_messages(
        self, messages: list[AnyMessage], max_steps: int = 1
    ) -> list[dict[str, str]]:
        if max_steps != 1:
            raise ValueError("agent_memory_v1 only supports manager max_steps=1")
        return [
            {"role": "system", "content": self.instructions},
            {"role": "user", "content": utils.get_conversation(messages)},
        ]

    def _for_new_sources(self, input: Mapping) -> "SourceAwareMemoryManager":
        """Use upstream schema repair before a missing source reaches the writer.

        Unchanged old dicts are carried back by MemoryManager without being
        regenerated. Inserted and patched tool outputs must cite this call's
        sources. Build a new schema/manager per call; never mutate shared state.
        """
        new_sources = input.get("new_source_ids")
        if new_sources is None:
            return self
        if not isinstance(new_sources, (list, tuple)) or not new_sources or any(
            not isinstance(source, str) or not source.strip() for source in new_sources
        ):
            raise ValueError("new_source_ids must be a non-empty list of source IDs")
        required = set(new_sources)

        def require_new_source(value: list[str]) -> list[str]:
            if not required.intersection(value):
                raise ValueError(
                    "Inserted or patched memory must cite a New Messages source: "
                    + ", ".join(sorted(required))
                )
            return value

        schema = create_model(
            "MemoryContent", __base__=MemoryContent, __doc__=MemoryContent.__doc__,
            __validators__={"require_new_source": field_validator("source_ids")(require_new_source)},
            source_ids=(list[str], Field(
                min_length=1,
                description="Preserve old sources and cite at least one source from New Messages: "
                            + ", ".join(sorted(required)),
            )),
        )
        return SourceAwareMemoryManager(
            self.model, schemas=[schema, Done], instructions=self.instructions,
            enable_inserts=self.enable_inserts, enable_updates=self.enable_updates,
            enable_deletes=self.enable_deletes,
        )

    def invoke(self, input, config=None, **kwargs):
        return MemoryManager.invoke(self._for_new_sources(input), input, config=config, **kwargs)

    async def ainvoke(self, input, config=None, **kwargs):
        return await MemoryManager.ainvoke(
            self._for_new_sources(input), input, config=config, **kwargs,
        )


@dataclass(frozen=True)
class ManagedMemory:
    """A normalized manager result with LangMem's temporary ID preserved."""

    id: str
    content: MemoryContent


def create_memory_manager(chat_model: BaseChatModel) -> SourceAwareMemoryManager:
    """Construct the fixed LangMem 0.0.30 manager used by the service."""

    return SourceAwareMemoryManager(
        chat_model,
        # Trustcall requires a tool call when existing documents are present.
        # LangMem normally adds its no-op tool only on the second iteration;
        # expose that same tool on our single iteration as well. MemoryManager
        # consumes Done without producing a stored memory.
        schemas=[MemoryContent, Done],
        instructions=MEMORY_INSTRUCTIONS,
        enable_inserts=True,
        enable_updates=True,
        enable_deletes=False,
    )


def _is_remove_document(value: object) -> bool:
    repr_name = getattr(value, "__repr_name__", None)
    if callable(repr_name) and repr_name() == "RemoveDoc":
        return True
    return value.__class__.__name__ == "RemoveDoc"


def _split_result(item: object) -> tuple[object, object]:
    if isinstance(item, ExtractedMemory):
        return item.id, item.content
    if isinstance(item, Mapping):
        if "id" not in item:
            raise ValueError("LangMem result is missing its internal id")
        content = item.get("content", item.get("value"))
        if content is None:
            raise ValueError(f"LangMem result {item['id']!r} is missing content")
        return item["id"], content
    if isinstance(item, (tuple, list)) and len(item) == 2:
        return item[0], item[1]
    item_id = getattr(item, "id", None)
    content = getattr(item, "content", None)
    if item_id is None or content is None:
        raise TypeError(f"unsupported LangMem result type: {type(item).__name__}")
    return item_id, content


def normalize_manager_results(raw_results: Sequence[object]) -> list[ManagedMemory]:
    """Normalize LangMem's Pydantic or dict-shaped results before validation."""

    results: list[ManagedMemory] = []
    for item in raw_results:
        item_id, raw_content = _split_result(item)
        if _is_remove_document(raw_content):
            raise ValueError("LangMem returned a deletion object, but deletes are disabled")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("LangMem result IDs must be non-empty strings")
        item_id = item_id.strip()
        # Keep duplicate IDs for the writer: identical patches can be
        # coalesced with source merging, while competing patches are rejected
        # before any persistence action.  Dropping one here would make the
        # result depend on manager ordering and hide a conflict.
        content = (
            raw_content
            if isinstance(raw_content, MemoryContent)
            else MemoryContent.model_validate(
                raw_content.model_dump(mode="json")
                if hasattr(raw_content, "model_dump")
                else raw_content
            )
        )
        results.append(ManagedMemory(item_id, canonical_memory_content(content)))
    return results


async def invoke_memory_manager(
    manager: SourceAwareMemoryManager,
    *,
    manager_message: AnyMessage,
    existing: Sequence[ExistingMemory],
    new_source_ids: Sequence[str] | None = None,
) -> list[ManagedMemory]:
    """Expose current records as editable; history remains in the message context."""

    existing_documents = [
        (
            memory.id,
            "MemoryContent",
            memory.content.model_dump(mode="json"),
        )
        for memory in existing
        if not memory.superseded_by
    ]
    payload = {
            "messages": [manager_message],
            "existing": existing_documents,
            "max_steps": 1,
    }
    if new_source_ids is not None:
        payload["new_source_ids"] = list(new_source_ids)
    raw_results = await manager.ainvoke(payload)
    return normalize_manager_results(raw_results)
