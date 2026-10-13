"""Tests for parallel (batch) tool execution and ParallelToolAction."""

import json

import pytest

from dbgpt.agent.expand.actions.react_action import Terminate
from dbgpt.agent.expand.actions.tool_action import (
    ToolCallSpec,
    ToolResultPersistenceError,
    ToolStartCallbackError,
    run_tools_batch,
)
from dbgpt.agent.expand.tool_calling_agent import (
    NativeToolCallAction,
    ParallelToolAction,
)
from dbgpt.agent.resource.tool.base import tool
from dbgpt.agent.resource.tool.pack import ToolPack


@tool(description="Add two numbers")
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@tool(description="Multiply two numbers")
def multiply(a: int, b: int) -> int:
    """Multiply two numbers."""
    return a * b


@tool(description="Explode on purpose")
def explode() -> str:
    """Always fails."""
    raise RuntimeError("boom")


def _pack(*tools):
    return ToolPack([fn._tool for fn in tools])


@pytest.mark.asyncio
async def test_run_tools_batch_success():
    resource = _pack(add, multiply)
    out = await run_tools_batch(
        [
            ToolCallSpec(name="add", args={"a": 1, "b": 2}),
            ToolCallSpec(name="multiply", args={"a": 3, "b": 4}),
        ],
        resource,
        need_vis_render=False,
    )
    assert out.is_exe_success is True
    assert out.terminate is None
    assert out.action == "add, multiply"
    results = json.loads(out.observations)
    assert len(results) == 2
    assert [r["name"] for r in results] == ["add", "multiply"]
    assert "3" in out.content  # 1 + 2
    assert "12" in out.content  # 3 * 4


@pytest.mark.asyncio
async def test_run_tools_batch_single_forwards():
    resource = _pack(add, multiply)
    out = await run_tools_batch(
        [ToolCallSpec(name="add", args={"a": 2, "b": 5})],
        resource,
        need_vis_render=False,
    )
    assert out.is_exe_success is True
    # single-element batch uses the legacy run_tool path: observations is str
    assert out.observations == "7"


@pytest.mark.asyncio
async def test_run_tools_batch_persists_each_native_result():
    persisted = []

    async def append_result(payload):
        persisted.append(payload)

    out = await run_tools_batch(
        [
            ToolCallSpec(name="add", args={"a": 2, "b": 5}, call_id="call-1"),
            ToolCallSpec(name="multiply", args={"a": 3, "b": 4}, call_id="call-2"),
        ],
        _pack(add, multiply),
        need_vis_render=False,
        on_tool_result=append_result,
    )

    assert out.is_exe_success is True
    assert {item["tool_call_id"] for item in persisted} == {"call-1", "call-2"}
    assert {item["content"] for item in persisted} == {"7", "12"}
    assert all(item["success"] for item in persisted)


@pytest.mark.asyncio
async def test_tool_result_persistence_failure_stops_batch_return():
    async def fail_append(_payload):
        raise OSError("database unavailable")

    with pytest.raises(ToolResultPersistenceError, match="was not persisted"):
        await run_tools_batch(
            [ToolCallSpec(name="add", args={"a": 1, "b": 2}, call_id="call-1")],
            _pack(add),
            need_vis_render=False,
            on_tool_result=fail_append,
        )


@pytest.mark.asyncio
async def test_pre_tool_guard_failure_prevents_side_effect_and_fails_closed():
    side_effects = []

    @tool(description="Record an irreversible side effect")
    def write_record() -> str:
        side_effects.append("written")
        return "written"

    async def reject_stale_worker(_payload):
        raise RuntimeError("turn lease was fenced")

    with pytest.raises(ToolStartCallbackError, match="pre-tool guard failed"):
        await run_tools_batch(
            [ToolCallSpec(name="write_record", args={}, call_id="call-stale")],
            _pack(write_record),
            need_vis_render=False,
            on_tool_start=reject_stale_worker,
        )

    assert side_effects == []


@pytest.mark.asyncio
async def test_parallel_pre_tool_guard_failure_prevents_that_tool_side_effect():
    side_effects = []

    @tool(description="Record an irreversible side effect")
    def write_record() -> str:
        side_effects.append("written")
        return "written"

    async def reject_stale_worker(payload):
        if payload["tool_call_id"] == "call-stale":
            raise RuntimeError("turn lease was fenced")

    with pytest.raises(ToolStartCallbackError, match="pre-tool guard failed"):
        await run_tools_batch(
            [
                ToolCallSpec(name="add", args={"a": 1, "b": 2}, call_id="call-ok"),
                ToolCallSpec(
                    name="write_record", args={}, call_id="call-stale"
                ),
            ],
            _pack(add, write_record),
            need_vis_render=False,
            on_tool_start=reject_stale_worker,
        )

    # The other concurrently scheduled call is independent; the guarded tool
    # itself must never start after its lease check fails.
    assert side_effects == []


@pytest.mark.asyncio
async def test_run_tools_batch_partial_failure():
    resource = _pack(add, explode)
    out = await run_tools_batch(
        [
            ToolCallSpec(name="add", args={"a": 1, "b": 1}),
            ToolCallSpec(name="explode", args={}),
        ],
        resource,
        need_vis_render=False,
    )
    assert out.is_exe_success is False
    results = json.loads(out.observations)
    assert results[0]["success"] is True
    assert results[1]["success"] is False
    assert "explode" in out.content


@pytest.mark.asyncio
async def test_run_tools_batch_terminate_aggregation():
    resource = ToolPack([add._tool, Terminate()])
    out = await run_tools_batch(
        [
            ToolCallSpec(name="add", args={"a": 1, "b": 2}),
            ToolCallSpec(name="terminate", args={"output": "final answer"}),
        ],
        resource,
        need_vis_render=False,
    )
    assert out.terminate is True
    assert "final answer" in out.content


@pytest.mark.asyncio
async def test_run_tools_batch_empty():
    out = await run_tools_batch([], None, need_vis_render=False)
    assert out.is_exe_success is False


PARALLEL_TEXT = (
    "Thought: compute the sum.\n"
    "Action: add\n"
    'Action Input: {"a": 1, "b": 2}\n'
    "Thought: compute the product.\n"
    "Action: multiply\n"
    'Action Input: {"a": 3, "b": 4}\n'
)


@pytest.mark.asyncio
async def test_parallel_tool_action_multi_step():
    action = ParallelToolAction()
    action.init_resource(_pack(add, multiply))
    out = await action.run(PARALLEL_TEXT, enable_parallel=True, need_vis_render=False)
    assert out.is_exe_success is True
    assert out.action == "add, multiply"
    assert out.thoughts == "compute the sum.\ncompute the product."
    results = json.loads(out.observations)
    assert len(results) == 2


@pytest.mark.asyncio
async def test_parallel_tool_action_single_step_delegates():
    single = 'Thought: add them.\nAction: add\nAction Input: {"a": 2, "b": 3}\n'
    action = ParallelToolAction()
    action.init_resource(_pack(add, multiply))
    out = await action.run(single, need_vis_render=False)
    assert out.is_exe_success is True
    assert out.action == "add"
    assert out.observations == "5"


@pytest.mark.asyncio
async def test_native_tool_call_action_batch():
    action = NativeToolCallAction()
    action.init_resource(_pack(add, multiply))
    tool_calls = [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "add", "arguments": '{"a": 1, "b": 2}'},
        },
        {
            "id": "c2",
            "type": "function",
            "function": {"name": "multiply", "arguments": '{"a": 3, "b": 4}'},
        },
    ]
    out = await action.run("", tool_calls=tool_calls, need_vis_render=False)
    assert out.is_exe_success is True
    assert out.action == "add, multiply"
    results = json.loads(out.observations)
    assert [r["name"] for r in results] == ["add", "multiply"]
    assert [r["call_id"] for r in results] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_native_tool_call_action_falls_back_to_text():
    action = NativeToolCallAction()
    action.init_resource(_pack(add, multiply))
    out = await action.run(PARALLEL_TEXT, enable_parallel=True, need_vis_render=False)
    assert out.is_exe_success is True
    assert out.action == "add, multiply"
