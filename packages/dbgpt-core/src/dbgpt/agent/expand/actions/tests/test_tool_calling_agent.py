"""Integration tests for ToolCallingReActAgent action dispatch.

These drive the agent's ``act()`` through the real ``ConversableAgent.act``
dispatch loop (the same loop ``generate_reply`` — and thus the
``/api/v1/chat/react-agent`` endpoint — uses) but with a stubbed resource, so
no LLM/network is required.
"""

import json
from unittest.mock import AsyncMock

import pytest

from dbgpt.agent import AgentContext, AgentMessage
from dbgpt.agent.core.rollout import RolloutEvent, project_rollout_events
from dbgpt.agent.expand.tool_calling_agent import ToolCallingReActAgent
from dbgpt.agent.resource.tool.base import tool
from dbgpt.agent.resource.tool.pack import ToolPack
from dbgpt.core.interface.llm import ModelOutput
from dbgpt.core.interface.message import ModelMessageRoleType


@tool(description="Add two numbers")
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@tool(description="Multiply two numbers")
def multiply(a: int, b: int) -> int:
    """Multiply two numbers."""
    return a * b


MULTI_STEP = (
    "Thought: compute both results.\n"
    "Action: add\n"
    'Action Input: {"a": 1, "b": 2}\n'
    "Thought: also multiply.\n"
    "Action: multiply\n"
    'Action Input: {"a": 3, "b": 4}\n'
)


def _make_agent(
    enable_parallel: bool, enable_native: bool = False
) -> ToolCallingReActAgent:
    agent = ToolCallingReActAgent()
    agent.agent_context = AgentContext(
        conv_id="test",
        enable_parallel_tool_execution=enable_parallel,
        enable_native_function_calling=enable_native,
    )
    pack = ToolPack([add._tool, multiply._tool])
    agent.resource = pack
    for action in agent.actions:
        action.init_resource(pack)
    return agent


def _act_kwargs(agent, enable_parallel):
    """Simulate the kwargs generate_reply passes to act() via prepare_act_param."""
    return {
        "parser": agent.parser,
        "enable_parallel": enable_parallel,
        "tool_calls": None,
    }


@pytest.mark.asyncio
async def test_act_parallel_executes_multiple_steps():
    agent = _make_agent(enable_parallel=True)
    out = await agent.act(
        AgentMessage(content=MULTI_STEP),
        sender=agent,
        **_act_kwargs(agent, True),
    )
    assert out.is_exe_success is True
    # Both tools ran in one turn and were aggregated.
    assert out.action == "add, multiply"
    results = json.loads(out.observations)
    assert [r["name"] for r in results] == ["add", "multiply"]


@pytest.mark.asyncio
async def test_act_parallel_disabled_preserves_single_step_behaviour():
    agent = _make_agent(enable_parallel=False)
    out = await agent.act(
        AgentMessage(content=MULTI_STEP),
        sender=agent,
        **_act_kwargs(agent, False),
    )
    # Legacy behaviour: only the first action runs (single action per round).
    assert out.is_exe_success is True
    assert out.action == "add"
    assert out.observations == "3"


@pytest.mark.asyncio
async def test_native_thinking_loads_rollout_messages_and_records_raw_call():
    agent = _make_agent(enable_parallel=False)
    projection = project_rollout_events(
        [
            RolloutEvent("user_message", {"content": "prior question"}),
            RolloutEvent("assistant_message", {"content": "prior answer"}),
        ]
    )
    load_history = AsyncMock(return_value=projection)
    record_assistant = AsyncMock()
    record_tool = AsyncMock()
    assert_model_lease = AsyncMock()
    agent.configure_rollout_history(
        message_loader=load_history,
        on_assistant_message=record_assistant,
        on_tool_result=record_tool,
        on_model_call=assert_model_lease,
    )
    model_output = ModelOutput.build(
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "add", "arguments": '{"a":1,"b":2}'},
            }
        ]
    )

    class StubClient:
        request = None

        async def create_with_output(self, **kwargs):
            self.request = kwargs
            return model_output

    client = StubClient()
    object.__setattr__(agent, "llm_client", client)
    object.__setattr__(agent, "_a_select_llm_model", AsyncMock(return_value="stub"))
    system_message = AgentMessage(
        content="current system instructions", role=ModelMessageRoleType.SYSTEM
    )
    current_user_message = AgentMessage(
        content="current user question", role=ModelMessageRoleType.HUMAN
    )

    await agent._thinking_native([system_message, current_user_message])

    load_history.assert_awaited_once()
    assert_model_lease.assert_awaited_once()
    assert client.request["messages"] == [
        {
            "content": "current system instructions",
            "context": None,
            "role": "system",
        },
        {"role": "user", "content": "prior question"},
        {"role": "assistant", "content": "prior answer"},
        {"role": "user", "content": "current user question"},
    ]
    record_assistant.assert_awaited_once_with(
        {
            "content": "",
            "tool_calls": model_output.tool_calls,
            "finish_reason": None,
        }
    )
    assert record_tool.await_count == 0


@pytest.mark.asyncio
async def test_model_lease_guard_failure_prevents_provider_request():
    agent = _make_agent(enable_parallel=False, enable_native=True)
    assert_model_lease = AsyncMock(side_effect=RuntimeError("turn lease was fenced"))
    record_assistant = AsyncMock()
    agent.configure_rollout_history(
        message_loader=AsyncMock(return_value=[]),
        on_assistant_message=record_assistant,
        on_tool_result=AsyncMock(),
        on_model_call=assert_model_lease,
    )

    class StubClient:
        calls = 0

        async def create_with_output(self, **kwargs):
            del kwargs
            self.calls += 1
            return ModelOutput.build(text="should not be returned")

    client = StubClient()
    object.__setattr__(agent, "llm_client", client)
    object.__setattr__(agent, "_a_select_llm_model", AsyncMock(return_value="stub"))

    with pytest.raises(RuntimeError, match="turn lease was fenced"):
        await agent._thinking_native([])

    assert client.calls == 0
    record_assistant.assert_not_awaited()


@pytest.mark.asyncio
async def test_assistant_event_persistence_failure_stops_native_response():
    agent = _make_agent(enable_parallel=False, enable_native=True)
    record_assistant = AsyncMock(side_effect=OSError("database unavailable"))
    agent.configure_rollout_history(
        message_loader=AsyncMock(
            return_value=project_rollout_events(
                [RolloutEvent("user_message", {"content": "question"})]
            )
        ),
        on_assistant_message=record_assistant,
        on_tool_result=AsyncMock(),
    )
    model_output = ModelOutput.build(
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "add", "arguments": '{"a":1,"b":2}'},
            }
        ]
    )

    class StubClient:
        async def create_with_output(self, **kwargs):
            del kwargs
            return model_output

    object.__setattr__(agent, "llm_client", StubClient())
    object.__setattr__(agent, "_a_select_llm_model", AsyncMock(return_value="stub"))

    with pytest.raises(OSError, match="database unavailable"):
        await agent._thinking_native([])

    assert getattr(agent, "_pending_native_tool_calls") is None
    record_assistant.assert_awaited_once()


@pytest.mark.asyncio
async def test_rollout_plain_assistant_text_is_final_answer():
    agent = _make_agent(enable_parallel=False, enable_native=True)
    agent.configure_rollout_history(
        message_loader=AsyncMock(return_value=[]),
        on_assistant_message=AsyncMock(),
        on_tool_result=AsyncMock(),
    )

    out = await agent.act(
        AgentMessage(content="The answer is 42."),
        sender=agent,
        **{
            **_act_kwargs(agent, False),
            "rollout_on_tool_result": AsyncMock(),
        },
    )

    assert out.is_exe_success is True
    assert out.terminate is True
    assert out.content == "The answer is 42."


@pytest.mark.asyncio
async def test_rollout_rejects_textual_react_tool_call_without_execution():
    agent = _make_agent(enable_parallel=False, enable_native=True)
    tool_result_callback = AsyncMock()
    agent.configure_rollout_history(
        message_loader=AsyncMock(return_value=[]),
        on_assistant_message=AsyncMock(),
        on_tool_result=tool_result_callback,
    )
    textual_action = AgentMessage(
        content='Thought: add numbers.\nAction: add\nAction Input: {"a": 1, "b": 2}'
    )

    with pytest.raises(ValueError, match="textual ReAct action was not executed"):
        await agent.act(
            textual_action,
            sender=agent,
            **{
                **_act_kwargs(agent, False),
                "rollout_on_tool_result": tool_result_callback,
            },
        )

    tool_result_callback.assert_not_awaited()
