"""Tests for window-adaptive tool-result budgets (P1-19 (1))."""

from dbgpt.agent import AgentContext
from dbgpt.agent.core.context import ContextBudgetConfig
from dbgpt.agent.expand.tool_calling_agent import ToolCallingReActAgent


def test_init_context_management_derives_budgets_from_agent_context_window():
    agent = ToolCallingReActAgent()
    agent.agent_context = AgentContext(conv_id="derive-32k", max_context_tokens=32000)

    agent.init_context_management()

    config = agent._context_manager.config
    # 32000 tokens * 4 chars * 0.15 / 0.30 fractions.
    assert config.tool_result_threshold == 19200
    assert config.turn_result_budget == 38400
    # The Layer 2 storage must consume the derived per-result threshold.
    assert agent._result_storage._config.default_result_size == 19200


def test_init_context_management_derives_budgets_from_default_window():
    agent = ToolCallingReActAgent()

    agent.init_context_management()

    config = agent._context_manager.config
    # 120000 tokens * 4 chars * 0.15 / 0.30 fractions.
    assert config.tool_result_threshold == 72000
    assert config.turn_result_budget == 144000
    assert agent._result_storage._config.default_result_size == 72000


def test_explicit_budget_config_is_not_rescaled():
    agent = ToolCallingReActAgent()
    agent.agent_context = AgentContext(
        conv_id="derive-explicit", max_context_tokens=32000
    )

    agent.init_context_management(
        config=ContextBudgetConfig(
            max_context_tokens=32000,
            tool_result_threshold=50000,
            turn_result_budget=60000,
        )
    )

    config = agent._context_manager.config
    assert config.tool_result_threshold == 50000
    assert config.turn_result_budget == 60000
    assert agent._result_storage._config.default_result_size == 50000
