"""Tests for retiring legacy manage_context in rollout mode (C4, §2.4)."""

import pytest

from dbgpt.agent import AgentContext, AgentMessage
from dbgpt.agent.core.context import ContextBudgetConfig
from dbgpt.agent.expand.tool_calling_agent import ToolCallingReActAgent


class _StubAgent(ToolCallingReActAgent):
    """ToolCallingReActAgent with prompt/resource plumbing stubbed out."""

    async def load_resource(self, *args, **kwargs):
        return "", []

    async def generate_resource_variables(self, resource_prompt_str):
        return {}

    async def build_system_prompt(self, **kwargs):
        return "system prompt"

    async def build_prompt(self, **kwargs):
        return "user prompt"

    @property
    def task_progress_summary(self):
        return ""

    async def read_memories(self, *args, **kwargs):
        return []


async def _run_thinking(config, *, rollout, statuses):
    agent = _StubAgent()
    agent.agent_context = AgentContext(
        conv_id="legacy-skip-test", enable_rollout_history=rollout
    )

    async def on_status(status):
        statuses.append(status)

    agent.init_context_management(config=config, on_status_event=on_status)
    await agent._load_thinking_messages(AgentMessage(content="hello"), sender=agent)


@pytest.mark.asyncio
async def test_rollout_mode_with_compaction_enabled_skips_legacy_manage_context():
    statuses = []

    await _run_thinking(
        ContextBudgetConfig(
            context_compaction_enabled=True, max_context_tokens=100000
        ),
        rollout=True,
        statuses=statuses,
    )

    # The coordinator owns compaction and status once the feature is on.
    assert statuses == []


@pytest.mark.asyncio
async def test_flag_off_keeps_legacy_manage_context_in_rollout_mode():
    statuses = []

    await _run_thinking(
        ContextBudgetConfig(
            context_compaction_enabled=False, max_context_tokens=100000
        ),
        rollout=True,
        statuses=statuses,
    )

    # Zero behavior change while the flag stays off: the legacy layer still
    # runs and still emits its status event.
    assert len(statuses) == 1
    assert statuses[0]["state"] == "normal"


@pytest.mark.asyncio
async def test_non_rollout_path_still_runs_manage_context_when_enabled():
    statuses = []

    await _run_thinking(
        ContextBudgetConfig(
            context_compaction_enabled=True, max_context_tokens=100000
        ),
        rollout=False,
        statuses=statuses,
    )

    assert len(statuses) == 1
