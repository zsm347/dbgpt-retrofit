"""Regressions for observed duplicate writes, history loss, and invalid targets.

Run in this example's independent environment; no models or databases are used.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from memory_v1 import (
    ExistingMemory,
    MemoryContent,
    MemoryScope,
    MemoryService,
    MemoryWriteError,
    SourceMessage,
    SourceRef,
    build_answer_context,
)


AT = datetime(2026, 10, 9, tzinfo=timezone.utc)
SCOPE = MemoryScope(scope_type="project", scope_id="regression")


def old_record(memory_id, text, *, superseded_by=None, valid_from=None):
    ref = SourceRef(id=f"source-{memory_id}", role="user", observed_at=AT)
    return ExistingMemory(
        id=memory_id,
        content=MemoryContent(
            content=text, source_ids=[ref.id], valid_from=valid_from,
        ),
        source_refs=[ref],
        recorded_at=AT,
        superseded_by=superseded_by,
    )


def result(memory_id, text, sources=("new-1",), *, targets=(), valid_from=None):
    return {
        "id": memory_id,
        "content": MemoryContent(
            content=text, source_ids=list(sources),
            supersedes_ids=list(targets), valid_from=valid_from,
        ).model_dump(mode="json"),
    }


class FakeStore:
    def __init__(self, records, *, fail_add=False):
        self.records = {r.id: r.model_copy(deep=True) for r in records}
        self.config = SimpleNamespace(write_candidate_limit=10, recent_limit=10)
        self.calls = []
        self.fail_add = fail_add

    async def search(self, scope, query, *, top_k=None):
        return list(self.records.values())

    async def get(self, scope, memory_id):
        return self.records.get(memory_id)

    async def add(self, scope, content, source_refs):
        if self.fail_add:
            raise RuntimeError("simulated ADD failure")
        memory_id = f"actual-{len(self.records) + 1}"
        self.calls.append(("add", memory_id))
        self.records[memory_id] = ExistingMemory(
            id=memory_id,
            content=content.model_copy(update={"supersedes_ids": []}),
            source_refs=list(source_refs),
            recorded_at=AT,
        )
        return memory_id

    async def update(self, scope, memory_id, content, source_refs, *, superseded_by=None):
        old = self.records[memory_id]
        self.calls.append(("update", memory_id))
        self.records[memory_id] = old.model_copy(update={
            "content": content.model_copy(update={"supersedes_ids": []}),
            "source_refs": list(source_refs),
            "superseded_by": superseded_by or old.superseded_by,
        })
        return memory_id


class StubManager:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def ainvoke(self, payload):
        self.calls.append(payload)
        return self.results


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def write(self, records, results, *, fail_add=False):
        store = FakeStore(records, fail_add=fail_add)
        manager = StubManager(results)
        service = MemoryService(store, manager)
        messages = [
            SourceMessage(id="new-1", role="user", content="New evidence.", observed_at=AT),
            SourceMessage(id="new-2", role="assistant", content="New context.", observed_at=AT),
        ]
        summary = await service.add(SCOPE, messages)
        return store, manager, service, summary

    async def test_insert_identical_to_updated_body_is_merged_in_both_orders(self):
        # Compare against the final patch, not just the old search snapshot.
        patch = result("old", "Screenshot was unconfirmed; now staging is confirmed.")
        insert = result("temporary", patch["content"]["content"], ("new-2",))
        for results in ([patch, insert], [insert, patch]):
            with self.subTest(order=[r["id"] for r in results]):
                store, _, _, summary = await self.write(
                    [old_record("old", "Screenshot environment was unknown.")], results,
                )
                self.assertEqual(store.calls, [("update", "old")])
                self.assertEqual(summary.added_ids, [])
                self.assertEqual(set(store.records["old"].content.source_ids),
                                 {"source-old", "new-1", "new-2"})

    async def test_different_new_information_is_not_coalesced(self):
        store, _, _, summary = await self.write(
            [old_record("old", "Library uses MySQL.")],
            [result("old", "Library uses MySQL; ClickHouse is an unconfirmed suggestion."),
             result("new", "Logs must exclude access credentials.")],
        )
        self.assertEqual(len(summary.added_ids), 1)
        self.assertEqual(summary.updated_ids, ["old"])
        self.assertEqual(len(store.records), 2)

    async def test_different_fact_dates_are_not_coalesced(self):
        _, _, _, summary = await self.write(
            [old_record("old", "A dated event.")],
            [result("old", "A dated event.", valid_from="2026-10-01"),
             result("new", "A dated event.", valid_from="2026-10-02")],
        )
        self.assertEqual(len(summary.added_ids), 1)

    async def test_replacement_preserves_old_body_and_sources_in_both_orders(self):
        patch = result("old", "Staging uses 600 seconds.")
        replacement = result("temporary", "Staging uses 600 seconds.", targets=("old",))
        for results in ([patch, replacement], [replacement, patch]):
            with self.subTest(order=[r["id"] for r in results]):
                store, _, _, summary = await self.write(
                    [old_record("old", "600 seconds; environment was unknown.")], results,
                )
                new_id = summary.added_ids[0]
                old = store.records["old"]
                self.assertEqual(old.content.content, "600 seconds; environment was unknown.")
                self.assertEqual(old.content.source_ids, ["source-old"])
                self.assertEqual([r.id for r in old.source_refs], ["source-old"])
                self.assertEqual(old.superseded_by, new_id)
                self.assertEqual(store.calls, [("add", new_id), ("update", "old")])
                self.assertEqual(summary.updated_ids, ["old"])

    async def test_unrelated_patch_survives_a_replacement(self):
        _, _, _, summary = await self.write(
            [old_record("state", "Old state."), old_record("other", "Other fact.")],
            [result("state", "New state."),
             result("new", "New state.", targets=("state",)),
             result("other", "Other fact with more detail.")],
        )
        self.assertEqual(set(summary.updated_ids), {"state", "other"})
        self.assertEqual(len(summary.added_ids), 1)

    async def test_different_patch_to_replacement_target_is_not_silently_discarded(self):
        with self.assertRaisesRegex(ValueError, "cannot discard different information"):
            await self.write(
                [old_record("old", "Old state.")],
                [result("old", "Old state plus an independent constraint."),
                 result("new", "New state.", targets=("old",))],
            )

    async def test_explicit_replacement_is_not_swallowed_by_equal_text(self):
        _, _, _, summary = await self.write(
            [old_record("old", "Repeated state.")],
            [result("new", "Repeated state.", targets=("old",))],
        )
        self.assertEqual(len(summary.added_ids), 1)
        self.assertEqual(summary.updated_ids, ["old"])

    async def test_redundant_historical_target_keeps_only_explicit_current_target(self):
        ancestor = old_record("ancestor", "Earlier state.", superseded_by="current")
        current = old_record("current", "Current state.")
        store, manager, _, summary = await self.write(
            [ancestor, current],
            [result("new", "New state.", targets=("ancestor", "current"),
                    valid_from="2026-10-08")],
        )
        new_id = summary.added_ids[0]
        self.assertEqual(store.records["ancestor"].superseded_by, "current")
        self.assertIsNone(store.records["ancestor"].content.valid_to)
        self.assertEqual(store.records["current"].superseded_by, new_id)
        self.assertEqual(summary.updated_ids, ["current"])
        self.assertEqual([r[0] for r in manager.calls[0]["existing"]], ["current"])
        # History is still available for understanding, but is not editable.
        self.assertIn('"read_only_memory_ids"', manager.calls[0]["messages"][0].content)
        self.assertIn("Earlier state.", manager.calls[0]["messages"][0].content)

    async def test_visible_multistep_chain_can_omit_requested_ancestors(self):
        store, _, _, summary = await self.write(
            [old_record("a", "A.", superseded_by="b"),
             old_record("b", "B.", superseded_by="c"), old_record("c", "C.")],
            [result("new", "D.", targets=("a", "b", "c"))],
        )
        self.assertEqual(summary.updated_ids, ["c"])
        self.assertEqual(store.records["a"].superseded_by, "b")
        self.assertEqual(store.records["b"].superseded_by, "c")

    async def test_historical_target_alone_is_not_redirected(self):
        with self.assertRaisesRegex(ValueError, "already points"):
            await self.write(
                [old_record("old", "Old.", superseded_by="current"),
                 old_record("current", "Current.")],
                [result("new", "New.", targets=("old",))],
            )

    async def test_missing_current_endpoint_is_not_inferred(self):
        with self.assertRaisesRegex(ValueError, "already points"):
            await self.write(
                [old_record("old", "Old.", superseded_by="not-returned")],
                [result("new", "New.", targets=("old",))],
            )

    async def test_cycle_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cycle"):
            await self.write(
                [old_record("a", "A.", superseded_by="b"),
                 old_record("b", "B.", superseded_by="a")],
                [result("new", "New.", targets=("a", "b"))],
            )

    async def test_unknown_source_cannot_hide_in_ignored_patch(self):
        with self.assertRaisesRegex(ValueError, "unknown source"):
            await self.write(
                [old_record("old", "Old.")],
                [result("old", "New.", ("unknown-source",)),
                 result("new", "New.", targets=("old",))],
            )

    async def test_unknown_target_and_competing_replacements_are_rejected(self):
        for results, message in (
            ([result("new", "New.", targets=("missing",))], "unknown replacement"),
            ([result("n1", "One.", targets=("old",)),
              result("n2", "Another.", targets=("old",))], "compete"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    await self.write([old_record("old", "Old.")], results)

    async def test_new_body_matching_history_does_not_update_history(self):
        _, _, _, summary = await self.write(
            [old_record("history", "Revisited state.", superseded_by="current"),
             old_record("current", "Other state.")],
            [result("new", "Revisited state.")],
        )
        self.assertEqual(len(summary.added_ids), 1)
        self.assertEqual(summary.updated_ids, [])

    async def test_add_failure_does_not_apply_concurrent_old_patch(self):
        old = old_record("old", "Original state.")
        store = FakeStore([old], fail_add=True)
        service = MemoryService(store, StubManager([
            result("old", "New state."),
            result("new", "New state.", targets=("old",)),
        ]))
        with self.assertRaises(MemoryWriteError):
            await service.add(SCOPE, [
                SourceMessage(id="new-1", role="user", content="New evidence."),
            ])
        self.assertEqual(store.calls, [])
        self.assertEqual(store.records["old"], old)

    async def test_read_and_answer_context_still_include_history(self):
        records = [old_record("history", "Old.", superseded_by="current"),
                   old_record("current", "Current.")]
        service = MemoryService(FakeStore(records), StubManager([]))
        recalled = await service.search(SCOPE, "before and now")
        context = build_answer_context(recalled, reference_time=AT)
        self.assertEqual([r["id"] for r in context["memory_candidates"]],
                         ["history", "current"])
        self.assertEqual(context["memory_candidates"][0]["superseded_by"], "current")


if __name__ == "__main__":
    unittest.main()
