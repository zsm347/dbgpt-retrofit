"""Orchestration for the independent memory write/read/delete chain."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Sequence

from .manager import ManagedMemory, create_memory_manager, invoke_memory_manager
from .prompts import InputPartitions, build_manager_message, build_source_catalog, build_write_query, prepare_partitions
from .schema import (
    ExistingMemory, ForgetItem, ForgetSummary, MemoryContent, MemoryScope,
    SourceRef, WriteEvent, WriteSummary, canonical_memory_content,
    memory_body_signature, memory_content_signature, merge_source_ids,
)
from .store import MemoryStore, MemoryStoreConfig, create_chat_model, scope_key


@dataclass(frozen=True)
class _WritePlan:
    action: str
    manager_id: str
    memory_id: str | None
    content: MemoryContent | None
    source_refs: tuple[SourceRef, ...]
    reason: str
    event_index: int
    supersedes_ids: tuple[str, ...] = ()


class MemoryWriteError(RuntimeError):
    """A storage failure after some plan actions may already have completed."""

    def __init__(self, message: str, summary: WriteSummary, cause: BaseException):
        super().__init__(message)
        self.summary = summary
        self.cause = cause


class MemoryService:
    """Independent async memory facade; no DB-GPT agent path is touched."""

    def __init__(self, store: MemoryStore, manager: Any):
        self.store = store
        self.manager = manager
        self._locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def from_config(cls, config: MemoryStoreConfig | None = None) -> "MemoryService":
        config = config or MemoryStoreConfig.from_env()
        return cls(MemoryStore.from_config(config), create_memory_manager(create_chat_model(config)))

    def _lock_for(self, scope: MemoryScope) -> asyncio.Lock:
        key = scope_key(scope)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def add(self, scope: MemoryScope, new_messages: Sequence,
                  recent_messages: Sequence | None = None, *, batch_id: str | None = None,
                  include_tool_in_query: bool = False) -> WriteSummary:
        return await self.write(scope, new_messages, recent_messages,
                                batch_id=batch_id, include_tool_in_query=include_tool_in_query)

    async def write(self, scope: MemoryScope, new_messages: Sequence,
                    recent_messages: Sequence | None = None, *, batch_id: str | None = None,
                    include_tool_in_query: bool = False) -> WriteSummary:
        """Search, consolidate, validate, then write; relation patches follow ADDs."""
        batch_id = batch_id or uuid.uuid4().hex
        if not batch_id.strip():
            raise ValueError("batch_id must not be empty")
        async with self._lock_for(scope):
            partitions = prepare_partitions(new_messages, recent_messages,
                                             recent_limit=self.store.config.recent_limit)
            query = build_write_query(partitions.new_messages, include_tool=include_tool_in_query)
            candidates = await self.store.search(scope, query,
                                                 top_k=self.store.config.write_candidate_limit)
            source_catalog = build_source_catalog(partitions, candidates)
            message = build_manager_message(batch_id=batch_id, partitions=partitions,
                                            scope=scope.model_dump(), existing=candidates)
            results = await invoke_memory_manager(self.manager, manager_message=message,
                                                  existing=candidates,
                                                  new_source_ids=[m.id for m in partitions.new_messages])
            plans, events = self._validate_and_plan(results, candidates, partitions, source_catalog)
            summary = self._summary(batch_id=batch_id, scope=scope, candidates=candidates,
                                    manager_result_count=len(results), events=events)
            await self._execute_plans(summary, plans, candidates, scope)
            return summary

    def _validate_and_plan(self, manager_results: Sequence[ManagedMemory],
                           candidates: Sequence[ExistingMemory], partitions: InputPartitions,
                           source_catalog: dict[str, SourceRef]) -> tuple[list[_WritePlan], list[WriteEvent]]:
        old_by_id = {memory.id: memory for memory in candidates}
        candidate_ids = set(old_by_id)
        current_source_ids = {message.id for message in partitions.new_messages}
        # Validate references before reconciling overlapping outputs.  An
        # ignored duplicate/patch must not hide an unknown source or target.
        normalized: list[ManagedMemory] = []
        events: list[WriteEvent] = []
        for result in manager_results:
            content = canonical_memory_content(result.content)
            if set(content.source_ids) - set(source_catalog):
                raise ValueError(f"manager result {result.id!r} cites unknown source IDs")
            targets = set(content.supersedes_ids)
            if targets - candidate_ids:
                raise ValueError(f"manager result {result.id!r} cites unknown replacement IDs")
            if result.id in targets:
                raise ValueError(f"manager result {result.id!r} cannot replace itself")
            if targets and result.id in old_by_id:
                raise ValueError("an existing Mem0 object cannot carry a replacement request")
            if result.id not in old_by_id and not (set(content.source_ids) & current_source_ids):
                raise ValueError(f"new manager result {result.id!r} must cite a New Messages source")
            redundant = self._redundant_replacement_targets(targets, old_by_id)
            for target in sorted(redundant):
                events.append(WriteEvent(
                    action="skip", memory_id=target, manager_id=result.id,
                    content=old_by_id[target].content.content,
                    reason="historical replacement target omitted; its current target is explicitly requested",
                ))
            content = canonical_memory_content(content.model_copy(update={
                "supersedes_ids": sorted(targets - redundant),
            }))
            normalized.append(ManagedMemory(result.id, content))

        replacing_ids = {
            target for result in normalized for target in result.content.supersedes_ids
        }
        replacement_bodies = {
            target: {r.content.content for r in normalized if target in r.content.supersedes_ids}
            for target in replacing_ids
        }
        # Plan existing patches first so a new object can be compared with
        # their final bodies, not just with the pre-update search snapshot.
        normalized.sort(key=lambda result: result.id not in old_by_id)
        plans: list[_WritePlan] = []
        new_plan_indexes: dict[tuple, int] = {}
        update_plan_indexes: dict[str, int] = {}
        replacement_plan_indexes: dict[tuple, int] = {}
        replacement_targets: dict[str, tuple] = {}

        for result in normalized:
            content = canonical_memory_content(result.content)
            replacement_ids = tuple(sorted(set(content.supersedes_ids)))
            if replacement_ids:
                signature = memory_body_signature(content)
                for target in replacement_ids:
                    old = old_by_id[target]
                    if old.superseded_by:
                        raise ValueError(f"replacement target {target!r} already points to {old.superseded_by!r}")
                    previous = replacement_targets.get(target)
                    if previous is not None and previous != signature:
                        raise ValueError(f"different new objects compete for replacement target {target!r}")
                    replacement_targets[target] = signature
                key = (memory_body_signature(content), replacement_ids)
                if key in replacement_plan_indexes:
                    plan_index = replacement_plan_indexes[key]
                    previous_plan = plans[plan_index]
                    assert previous_plan.content is not None
                    merged_content = canonical_memory_content(MemoryContent(
                        content=previous_plan.content.content,
                        source_ids=merge_source_ids(
                            previous_plan.content.source_ids, content.source_ids
                        ),
                        valid_from=previous_plan.content.valid_from,
                        valid_to=previous_plan.content.valid_to,
                        supersedes_ids=list(replacement_ids),
                    ))
                    plans[plan_index] = replace(
                        previous_plan,
                        content=merged_content,
                        source_refs=tuple(self._refs_for(merged_content, source_catalog)),
                        reason="replacement request coalesced with merged sources",
                    )
                    events.append(WriteEvent(action="skip", manager_id=result.id, content=content.content,
                                             reason="duplicate replacement request coalesced with source merge"))
                    continue
                event_index = len(events)
                events.append(WriteEvent(action="add", manager_id=result.id, content=content.content,
                                         reason="replacement must be added before old records are patched"))
                plans.append(_WritePlan("add", result.id, None, content,
                                        tuple(self._refs_for(content, source_catalog)),
                                        "replacement ADD", event_index, replacement_ids))
                replacement_plan_indexes[key] = len(plans) - 1
                continue

            if result.id in old_by_id:
                old = old_by_id[result.id]
                if old.superseded_by and content.content != old.content.content:
                    raise ValueError(
                        f"superseded memory {old.id!r} cannot have its body rewritten; "
                        "target its current replacement or create a new replacement"
                    )
                unchanged = memory_content_signature(content) == memory_content_signature(old.content)
                if not unchanged and not (set(content.source_ids) & current_source_ids):
                    raise ValueError(f"updated memory {result.id!r} is not supported by a new message source")
                if old.id in replacing_ids:
                    if content.content != old.content.content and content.content not in replacement_bodies[old.id]:
                        raise ValueError(
                            f"ordinary patch conflicts with replacement of {old.id!r}; "
                            "cannot discard different information"
                        )
                    events.append(WriteEvent(
                        action="skip", memory_id=old.id, manager_id=result.id,
                        content=old.content.content,
                        reason="replacement preserves original target; concurrent ordinary patch ignored",
                    ))
                    continue
                merged = canonical_memory_content(MemoryContent(
                    content=content.content,
                    source_ids=merge_source_ids(old.content.source_ids, content.source_ids),
                    # A LangMem patch may omit optional time fields.  An omitted
                    # field must not erase an already supported fact-time bound.
                    valid_from=content.valid_from or old.content.valid_from,
                    valid_to=content.valid_to or old.content.valid_to,
                    supersedes_ids=[],
                ))
                if memory_content_signature(merged) == memory_content_signature(old.content):
                    events.append(WriteEvent(action="skip", memory_id=old.id, manager_id=result.id,
                                             content=old.content.content, reason="unchanged existing object"))
                    continue
                existing_plan_index = update_plan_indexes.get(old.id)
                if existing_plan_index is not None:
                    previous_plan = plans[existing_plan_index]
                    assert previous_plan.content is not None
                    if memory_body_signature(previous_plan.content) != memory_body_signature(merged):
                        raise ValueError(
                            f"different patches compete for existing memory {old.id!r}"
                        )
                    coalesced = canonical_memory_content(MemoryContent(
                        content=previous_plan.content.content,
                        source_ids=merge_source_ids(
                            previous_plan.content.source_ids, merged.source_ids
                        ),
                        valid_from=previous_plan.content.valid_from,
                        valid_to=previous_plan.content.valid_to,
                        supersedes_ids=[],
                    ))
                    plans[existing_plan_index] = replace(
                        previous_plan,
                        content=coalesced,
                        source_refs=tuple(self._refs_for(coalesced, source_catalog)),
                        reason="duplicate update coalesced with source merge",
                    )
                    events.append(WriteEvent(
                        action="skip", memory_id=old.id, manager_id=result.id,
                        content=merged.content,
                        reason="duplicate update coalesced with source merge",
                    ))
                    continue
                event_index = len(events)
                events.append(WriteEvent(action="update", memory_id=old.id, manager_id=result.id,
                                         content=merged.content, reason="validated enrichment patch"))
                plans.append(_WritePlan("update", result.id, old.id, merged,
                                        tuple(self._refs_for(merged, source_catalog)),
                                        "existing Mem0 ID has a validated difference", event_index))
                update_plan_indexes[old.id] = len(plans) - 1
                continue

            update_matches = [
                (index, plan) for index, plan in enumerate(plans)
                if plan.action == "update" and plan.content is not None
                and memory_body_signature(plan.content) == memory_body_signature(content)
            ]
            if len(update_matches) > 1:
                raise ValueError(f"new result {result.id!r} ambiguously matches planned updates")
            if update_matches:
                plan_index, plan = update_matches[0]
                assert plan.content is not None
                merged = canonical_memory_content(plan.content.model_copy(update={
                    "source_ids": merge_source_ids(plan.content.source_ids, content.source_ids),
                }))
                plans[plan_index] = replace(
                    plan, content=merged,
                    source_refs=tuple(self._refs_for(merged, source_catalog)),
                    reason="identical insert coalesced into planned update with merged sources",
                )
                events.append(WriteEvent(
                    action="skip", memory_id=plan.memory_id, manager_id=result.id,
                    content=content.content,
                    reason="new object duplicates planned update; no additional ADD",
                ))
                continue
            exact_matches = [old for old in candidates if memory_content_signature(old.content) == memory_content_signature(content)]
            if exact_matches:
                old = exact_matches[0]
                events.append(WriteEvent(action="skip", memory_id=old.id, manager_id=result.id,
                                         content=old.content.content, reason="unchanged new object"))
                continue
            body_matches = [
                old for old in candidates if not old.superseded_by
                and memory_body_signature(old.content) == memory_body_signature(content)
            ]
            if len(body_matches) > 1:
                raise ValueError(f"new result {result.id!r} ambiguously matches existing memories")
            if body_matches:
                old = body_matches[0]
                if old.id in replacing_ids:
                    events.append(WriteEvent(
                        action="skip", memory_id=old.id, manager_id=result.id,
                        content=old.content.content,
                        reason="duplicate old body retained by replacement; no ordinary update",
                    ))
                    continue
                merged = canonical_memory_content(MemoryContent(
                    content=content.content,
                    source_ids=merge_source_ids(old.content.source_ids, content.source_ids),
                    valid_from=content.valid_from or old.content.valid_from,
                    valid_to=content.valid_to or old.content.valid_to,
                    supersedes_ids=[],
                ))
                if memory_content_signature(merged) == memory_content_signature(old.content):
                    events.append(WriteEvent(action="skip", memory_id=old.id, manager_id=result.id,
                                             content=old.content.content, reason="unchanged body reconciliation"))
                    continue
                existing_plan_index = update_plan_indexes.get(old.id)
                if existing_plan_index is not None:
                    previous_plan = plans[existing_plan_index]
                    assert previous_plan.content is not None
                    if memory_body_signature(previous_plan.content) != memory_body_signature(merged):
                        raise ValueError(
                            f"different patches compete for existing memory {old.id!r}"
                        )
                    coalesced = canonical_memory_content(MemoryContent(
                        content=previous_plan.content.content,
                        source_ids=merge_source_ids(
                            previous_plan.content.source_ids, merged.source_ids
                        ),
                        valid_from=previous_plan.content.valid_from,
                        valid_to=previous_plan.content.valid_to,
                        supersedes_ids=[],
                    ))
                    plans[existing_plan_index] = replace(
                        previous_plan,
                        content=coalesced,
                        source_refs=tuple(self._refs_for(coalesced, source_catalog)),
                        reason="duplicate update coalesced with source merge",
                    )
                    events.append(WriteEvent(
                        action="skip", memory_id=old.id, manager_id=result.id,
                        content=merged.content,
                        reason="duplicate update coalesced with source merge",
                    ))
                    continue
                event_index = len(events)
                events.append(WriteEvent(action="update", memory_id=old.id, manager_id=result.id,
                                         content=merged.content, reason="validated source enrichment"))
                plans.append(_WritePlan("update", result.id, old.id, merged,
                                        tuple(self._refs_for(merged, source_catalog)),
                                        "same managed object with new source", event_index))
                update_plan_indexes[old.id] = len(plans) - 1
                continue
            signature = memory_body_signature(content)
            if signature in new_plan_indexes:
                plan_index = new_plan_indexes[signature]
                previous_plan = plans[plan_index]
                assert previous_plan.content is not None
                merged_content = canonical_memory_content(MemoryContent(
                    content=previous_plan.content.content,
                    source_ids=merge_source_ids(
                        previous_plan.content.source_ids, content.source_ids
                    ),
                    valid_from=previous_plan.content.valid_from,
                    valid_to=previous_plan.content.valid_to,
                    supersedes_ids=[],
                ))
                plans[plan_index] = replace(
                    previous_plan,
                    content=merged_content,
                    source_refs=tuple(self._refs_for(merged_content, source_catalog)),
                    reason="duplicate new object coalesced with source merge",
                )
                events.append(WriteEvent(
                    action="skip", manager_id=result.id, content=content.content,
                    reason="duplicate new object coalesced with source merge",
                ))
                continue
            event_index = len(events)
            events.append(WriteEvent(action="add", manager_id=result.id, content=content.content,
                                     reason="validated new object"))
            plans.append(_WritePlan("add", result.id, None, content,
                                    tuple(self._refs_for(content, source_catalog)),
                                    "validated ADD", event_index))
            new_plan_indexes[signature] = len(plans) - 1
        return plans, events

    @staticmethod
    def _redundant_replacement_targets(
        requested: set[str], candidates: dict[str, ExistingMemory]
    ) -> set[str]:
        """Omit an ancestor only when its visible current target is also requested.

        This does not redirect a replacement to an inferred or unreturned ID.
        A historical target without an explicitly requested current endpoint
        still fails the regular replacement validation.
        """
        redundant: set[str] = set()
        for target in requested:
            cursor = target
            seen: set[str] = set()
            while cursor in candidates and candidates[cursor].superseded_by:
                if cursor in seen:
                    raise ValueError("replacement candidates contain a cycle")
                seen.add(cursor)
                cursor = candidates[cursor].superseded_by
            if cursor in seen:
                raise ValueError("replacement candidates contain a cycle")
            if cursor != target and cursor in requested and cursor in candidates:
                redundant.add(target)
        return redundant

    @staticmethod
    def _refs_for(content: MemoryContent, source_catalog: dict[str, SourceRef]) -> list[SourceRef]:
        return [source_catalog[source_id] for source_id in content.source_ids]

    @staticmethod
    def _summary(*, batch_id: str, scope: MemoryScope, candidates: Sequence[ExistingMemory],
                 manager_result_count: int, events: Sequence[WriteEvent]) -> WriteSummary:
        return WriteSummary(batch_id=batch_id, scope=scope,
                            candidate_ids=[memory.id for memory in candidates],
                            manager_result_count=manager_result_count, events=list(events),
                            skipped_ids=[e.memory_id for e in events if e.action == "skip" and e.memory_id])

    async def _execute_plans(self, summary: WriteSummary, plans: Sequence[_WritePlan],
                             candidates: Sequence[ExistingMemory], scope: MemoryScope) -> None:
        """Run ADDs first, then relation patches, then ordinary updates."""
        completed: set[int] = set()
        try:
            adds = [plan for plan in plans if plan.action == "add"]
            updates = [plan for plan in plans if plan.action == "update"]
            relation_actions = [
                (plan, old_id)
                for plan in adds
                for old_id in plan.supersedes_ids
            ]
            actual_by_plan: dict[int, str] = {}
            for plan_index, plan in enumerate(adds):
                try:
                    actual_id = await self.store.add(scope, plan.content, plan.source_refs)  # type: ignore[arg-type]
                except Exception as exc:
                    summary.unknown_actions.append(plan.manager_id)
                    summary.unattempted_actions.extend(p.manager_id for p in adds[plan_index + 1:])
                    summary.unattempted_actions.extend(
                        f"{relation_plan.manager_id}:relation:{relation_old_id}"
                        for relation_plan, relation_old_id in relation_actions
                    )
                    summary.unattempted_actions.extend(p.manager_id for p in updates)
                    raise MemoryWriteError("ADD outcome is unknown; old records were not relation-patched", summary, exc) from exc
                actual_by_plan[id(plan)] = actual_id
                summary.added_ids.append(actual_id)
                summary.events[plan.event_index] = summary.events[plan.event_index].model_copy(update={"memory_id": actual_id})
                completed.add(id(plan))

            for relation_index, (plan, old_id) in enumerate(relation_actions):
                if not plan.supersedes_ids:
                    continue
                actual_id = actual_by_plan[id(plan)]
                action_key = f"{plan.manager_id}:relation:{old_id}"
                old = await self.store.get(scope, old_id)
                if old is None:
                    summary.failed_actions.append(action_key)
                    summary.unattempted_actions.extend(
                        f"{next_plan.manager_id}:relation:{next_old_id}"
                        for next_plan, next_old_id in relation_actions[relation_index + 1:]
                    )
                    summary.unattempted_actions.extend(
                        next_plan.manager_id for next_plan in updates
                    )
                    raise MemoryWriteError(
                        f"replacement target {old_id!r} disappeared after ADD",
                        summary,
                        RuntimeError(f"replacement target {old_id!r} disappeared after ADD"),
                    )
                if old.superseded_by and old.superseded_by != actual_id:
                    summary.failed_actions.append(action_key)
                    summary.unattempted_actions.extend(
                        f"{next_plan.manager_id}:relation:{next_old_id}"
                        for next_plan, next_old_id in relation_actions[relation_index + 1:]
                    )
                    summary.unattempted_actions.extend(
                        next_plan.manager_id for next_plan in updates
                    )
                    raise MemoryWriteError(
                        f"replacement target {old_id!r} already points elsewhere",
                        summary,
                        RuntimeError(f"replacement target {old_id!r} already points elsewhere"),
                    )
                merged = canonical_memory_content(MemoryContent(
                    content=old.content.content,
                    # The replacement message supports the new object and the
                    # transition boundary; it does not re-prove the old
                    # object's body.  Keep the old provenance on the old
                    # record instead of attributing the new state to it.
                    source_ids=old.content.source_ids,
                    valid_from=old.content.valid_from,
                    valid_to=old.content.valid_to or plan.content.valid_from,
                    supersedes_ids=[],
                ))
                try:
                    await self.store.update(
                        scope,
                        old_id,
                        merged,
                        old.source_refs,
                        superseded_by=actual_id,
                    )
                except Exception as exc:
                    summary.unknown_actions.append(action_key)
                    summary.unattempted_actions.extend(
                        f"{next_plan.manager_id}:relation:{next_old_id}"
                        for next_plan, next_old_id in relation_actions[relation_index + 1:]
                    )
                    summary.unattempted_actions.extend(
                        next_plan.manager_id for next_plan in updates
                    )
                    raise MemoryWriteError(
                        f"replacement relation update outcome is unknown for {old_id!r}",
                        summary,
                        exc,
                    ) from exc
                summary.updated_ids.append(old_id)
                summary.events.append(WriteEvent(
                    action="update", memory_id=old_id, manager_id=plan.manager_id,
                    content=old.content.content,
                    reason=f"patched superseded_by={actual_id!r} after successful ADD",
                ))

            for update_index, plan in enumerate(updates):
                try:
                    await self.store.update(scope, plan.memory_id, plan.content, plan.source_refs)  # type: ignore[arg-type]
                except Exception as exc:
                    summary.unknown_actions.append(plan.manager_id)
                    summary.unattempted_actions.extend(
                        next_plan.manager_id for next_plan in updates[update_index + 1:]
                    )
                    raise MemoryWriteError(
                        f"ordinary update outcome is unknown for {plan.memory_id!r}",
                        summary,
                        exc,
                    ) from exc
                summary.updated_ids.append(plan.memory_id)  # type: ignore[arg-type]
                completed.add(id(plan))
        except MemoryWriteError:
            raise
        except Exception as exc:
            summary.unknown_actions.extend(p.manager_id for p in plans if id(p) not in completed)
            raise MemoryWriteError("write partially completed; inspect summary before targeted repair", summary, exc) from exc

    async def search(self, scope: MemoryScope, query: str, *, top_k: int | None = None) -> list[ExistingMemory]:
        return await self.store.search(scope, query, top_k=top_k)

    async def get(self, scope: MemoryScope, memory_id: str) -> ExistingMemory | None:
        return await self.store.get(scope, memory_id)

    async def forget(self, scope: MemoryScope, memory_ids: Sequence[str], *, batch_id: str | None = None) -> ForgetSummary:
        """Delete IDs only after the complete request passes scope validation."""
        batch_id = batch_id or uuid.uuid4().hex
        if isinstance(memory_ids, (str, bytes)):
            raise ValueError("memory_ids must be a sequence of IDs, not one string")
        if any(not isinstance(value, str) for value in memory_ids):
            raise ValueError("memory_ids must contain strings")
        ids = [value.strip() for value in memory_ids]
        if not ids or any(not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("memory_ids must be a non-empty list of unique non-empty IDs")
        async with self._lock_for(scope):
            records = {memory_id: await self.store.get(scope, memory_id) for memory_id in ids}
            items: list[ForgetItem] = []
            for memory_id in ids:
                record = records[memory_id]
                if record is None:
                    items.append(ForgetItem(memory_id=memory_id, status="missing"))
                    continue
                try:
                    status = await self.store.delete(scope, memory_id)
                    items.append(ForgetItem(memory_id=memory_id, status=status))
                except Exception as exc:
                    items.append(ForgetItem(memory_id=memory_id, status="unknown", reason=str(exc)))
            return ForgetSummary(batch_id=batch_id, scope=scope, requested_ids=ids, items=items)

    async def forget_from_search(self, scope: MemoryScope, query: str,
                                 select_ids: Callable[[list[ExistingMemory]], Sequence[str]], *,
                                 top_k: int | None = None) -> ForgetSummary:
        """Agent seam: search first, then pass selected real IDs to ``forget``."""
        candidates = await self.search(scope, query, top_k=top_k)
        return await self.forget(scope, list(select_ids(candidates)))
