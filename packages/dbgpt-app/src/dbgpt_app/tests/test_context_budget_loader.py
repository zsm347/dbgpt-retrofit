"""Tests for the API-layer context budget loader (P1-19 (1))."""

import asyncio
from types import SimpleNamespace

from dbgpt_app.openapi.api_v1 import agentic_data_api
from dbgpt_app.openapi.api_v1.agentic_data_api import _load_context_budget_config


def _fake_cfg(max_context_tokens):
    agent_context = SimpleNamespace(max_context_tokens=max_context_tokens)
    app_config = SimpleNamespace(
        service=SimpleNamespace(
            web=SimpleNamespace(agent_context=agent_context),
        )
    )
    return SimpleNamespace(
        SYSTEM_APP=SimpleNamespace(
            config=SimpleNamespace(configs={"app_config": app_config}),
        )
    )


def test_small_window_scales_tool_result_thresholds(monkeypatch):
    monkeypatch.setattr(agentic_data_api, "CFG", _fake_cfg(32000))

    config = asyncio.run(_load_context_budget_config())

    assert config.max_context_tokens == 32000
    # 32000 tokens * 4 chars * 0.15 / 0.30 fractions.
    assert config.tool_result_threshold == 19200
    assert config.turn_result_budget == 38400


def test_large_window_keeps_historical_defaults(monkeypatch):
    monkeypatch.setattr(agentic_data_api, "CFG", _fake_cfg(200000))

    config = asyncio.run(_load_context_budget_config())

    # The proportional value is clamped to the historical defaults as a cap,
    # so large models stay byte-identical to the pre-P1-19 behavior.
    assert config.tool_result_threshold == 100_000
    assert config.turn_result_budget == 200_000


def test_loader_scales_defaults_when_app_config_is_unavailable(monkeypatch):
    # Without a live SYSTEM_APP the loader falls back to the built-in
    # defaults; those must still be window-scaled.
    monkeypatch.setattr(agentic_data_api, "CFG", SimpleNamespace())

    config = asyncio.run(_load_context_budget_config())

    assert config.tool_result_threshold == 72000
    assert config.turn_result_budget == 144000
