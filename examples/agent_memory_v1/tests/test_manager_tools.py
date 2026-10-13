"""Exercise the pinned LangMem/Trustcall tool path without external APIs."""

from __future__ import annotations

import unittest

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from memory_v1.manager import create_memory_manager


def memory(text, *, sources=("new",), targets=()):
    return {
        "content": text, "source_ids": list(sources),
        "valid_from": None, "valid_to": None, "supersedes_ids": list(targets),
    }


def call(name, arguments, call_id):
    return {"name": name, "args": arguments, "id": call_id, "type": "tool_call"}


class ToolChatModel(BaseChatModel):
    """Return supplied tool calls while the real extractor processes them."""

    response: AIMessage
    responses: list[AIMessage] = Field(default_factory=list)
    generation_count: int = 0
    bindings: list[dict] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "test-tool-chat-model"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        self.bindings.append({
            "tools": [tool.__name__ if isinstance(tool, type) else str(tool) for tool in tools],
            "tool_choice": tool_choice,
        })
        return self.bind()

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.generation_count += 1
        message = self.responses.pop(0) if self.responses else self.response
        return ChatResult(generations=[ChatGeneration(message=message)])


class ManagerToolsTests(unittest.IsolatedAsyncioTestCase):
    async def run_manager(self, calls, *, existing=()):
        model = ToolChatModel(response=AIMessage(content="", tool_calls=calls))
        manager = create_memory_manager(model)
        output = await manager.ainvoke({
            "messages": [HumanMessage(content="New source-backed input.")],
            "existing": list(existing), "max_steps": 1,
        })
        return model, output

    async def test_done_is_available_on_first_step_and_not_persisted(self):
        old = memory("Existing fact.", sources=("old-source",))
        model, output = await self.run_manager(
            [call("Done", {}, "done")], existing=[("old", "MemoryContent", old)],
        )
        self.assertTrue(any("Done" in binding["tools"] and binding["tool_choice"] == "any"
                            for binding in model.bindings))
        self.assertEqual([(item.id, item.content) for item in output], [("old", old)])

    async def test_done_without_existing_produces_no_memories(self):
        _, output = await self.run_manager([call("Done", {}, "done")])
        self.assertEqual(output, [])

    async def test_multiple_independent_inserts_use_upstream_extraction(self):
        _, output = await self.run_manager([
            call("MemoryContent", memory("Occupation."), "occupation"),
            call("MemoryContent", memory("Answer preference."), "preference"),
        ])
        self.assertEqual({item.content.content for item in output},
                         {"Occupation.", "Answer preference."})
        self.assertEqual(len({item.id for item in output}), 2)

    async def test_patch_and_independent_replacement_preserve_upstream_ids(self):
        state = memory("Uses MySQL.", sources=("old-state",))
        pref = memory("Answer briefly.", sources=("old-pref",))
        _, output = await self.run_manager([
            call("MemoryContent", memory("Uses PostgreSQL.", targets=("state",)), "new-state"),
            call("PatchDoc", {
                "json_doc_id": "pref", "planned_edits": "Add supported detail and source.",
                "patches": [
                    {"op": "replace", "path": "/content", "value": "Answer briefly with commands."},
                    {"op": "add", "path": "/source_ids/-", "value": "new"},
                ],
            }, "patch-pref"),
        ], existing=[("state", "MemoryContent", state), ("pref", "MemoryContent", pref)])
        by_id = {item.id: item.content for item in output}
        self.assertEqual(by_id["state"], state)
        self.assertEqual(by_id["pref"].content, "Answer briefly with commands.")
        new = [item for item in output if item.id not in {"state", "pref"}]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0].content.supersedes_ids, ["state"])

    async def test_missing_patch_source_is_repaired_by_upstream_trustcall(self):
        first = AIMessage(content="", tool_calls=[call("PatchDoc", {
            "json_doc_id": "old", "planned_edits": "Add useful detail.",
            "patches": [{"op": "replace", "path": "/content", "value": "Fact with detail."}],
        }, "patch-old")])
        repair = AIMessage(content="", tool_calls=[call("PatchFunctionErrors", {
            "json_doc_id": "patch-old", "planned_edits": "Cite the new source.",
            "patches": [{"op": "add", "path": "/source_ids/-", "value": "new"}],
        }, "repair-source")])
        model = ToolChatModel(response=repair, responses=[first, repair])
        manager = create_memory_manager(model)
        output = await manager.ainvoke({
            "messages": [HumanMessage(content="New source: new")],
            "existing": [("old", "MemoryContent", memory("Fact.", sources=("old-source",)))],
            "max_steps": 1, "new_source_ids": ["new"],
        })
        self.assertEqual(model.generation_count, 2)
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0].id, "old")
        self.assertEqual(output[0].content.content, "Fact with detail.")
        self.assertEqual(output[0].content.source_ids, ["old-source", "new"])

    async def test_per_call_source_schema_does_not_mutate_shared_manager(self):
        model = ToolChatModel(response=AIMessage(content="", tool_calls=[]))
        manager = create_memory_manager(model)
        one = manager._for_new_sources({"new_source_ids": ["one"]})
        two = manager._for_new_sources({"new_source_ids": ["two"]})
        self.assertEqual(manager.schemas[0].model_json_schema()["properties"]["source_ids"].get("contains"), None)
        self.assertEqual(one.schemas[0].model_validate(memory("Fact.", sources=("one",))).source_ids, ["one"])
        self.assertEqual(two.schemas[0].model_validate(memory("Fact.", sources=("two",))).source_ids, ["two"])
        with self.assertRaisesRegex(ValueError, "New Messages source"):
            one.schemas[0].model_validate(memory("Fact.", sources=("two",)))

    async def test_source_constrained_done_keeps_old_sources_unchanged(self):
        model = ToolChatModel(response=AIMessage(content="", tool_calls=[call("Done", {}, "done")]))
        manager = create_memory_manager(model)
        old = memory("Existing fact.", sources=("old-source",))
        output = await manager.ainvoke({
            "messages": [HumanMessage(content="New greeting: new")],
            "existing": [("old", "MemoryContent", old)],
            "max_steps": 1, "new_source_ids": ["new"],
        })
        self.assertEqual([(item.id, item.content) for item in output], [("old", old)])


if __name__ == "__main__":
    unittest.main()
