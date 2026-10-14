"""Remote-provider wire-cap assertions (P2-8/F7, deployment form: proxy).

The repo's profiles deploy remote providers through the OpenAI-compatible
proxy client (``proxy/openai`` / ``proxy/tongyi``): an uncapped summary call
must leave ``max_tokens`` off the outbound payload entirely.
"""

from dbgpt.core import ModelRequest
from dbgpt.model.proxy.llms.chatgpt import OpenAILLMClient


def _client() -> OpenAILLMClient:
    return OpenAILLMClient(api_key="test-key", model="test-model")


def test_uncapped_request_omits_max_tokens_from_the_outbound_payload():
    payload = _client()._build_request(
        ModelRequest(model="test-model", messages=[], max_new_tokens=None)
    )

    assert "max_tokens" not in payload


def test_explicit_cap_still_reaches_the_outbound_payload():
    payload = _client()._build_request(
        ModelRequest(model="test-model", messages=[], max_new_tokens=1234)
    )

    assert payload["max_tokens"] == 1234
