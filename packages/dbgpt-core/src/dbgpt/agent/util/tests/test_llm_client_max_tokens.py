"""Tests for explicit None max_new_tokens passthrough (P2-8/F7)."""

import pytest

from dbgpt.agent.util.llm.llm_client import AIWrapper
from dbgpt.core.interface.llm import ModelOutput


class _CapturingClient:
    """Minimal LLMClient stub: records ModelRequests and yields one output."""

    def __init__(self):
        self.requests = []

    async def generate_stream(self, request):
        self.requests.append(request)
        yield ModelOutput.build(text="ok")


@pytest.mark.asyncio
async def test_explicit_none_max_new_tokens_reaches_the_request_uncapped():
    underlying = _CapturingClient()
    wrapper = AIWrapper(llm_client=underlying)

    text = await wrapper.create(
        messages=[{"role": "user", "content": "hi"}],
        llm_model="stub-model",
        max_new_tokens=None,
        temperature=0.0,
        stream_out=False,
    )

    assert text == "ok"
    request = underlying.requests[0]
    assert request.max_new_tokens is None
    # The worker RPC drops None fields entirely, so remote proxies receive a
    # request without any output cap (verified on the proxy payload side too).
    assert "max_new_tokens" not in request.to_dict()


@pytest.mark.asyncio
async def test_numeric_max_new_tokens_still_reaches_the_request():
    underlying = _CapturingClient()
    wrapper = AIWrapper(llm_client=underlying)

    await wrapper.create(
        messages=[{"role": "user", "content": "hi"}],
        llm_model="stub-model",
        max_new_tokens=1234,
        temperature=0.0,
        stream_out=False,
    )

    assert underlying.requests[0].max_new_tokens == 1234
    assert underlying.requests[0].to_dict()["max_new_tokens"] == 1234
