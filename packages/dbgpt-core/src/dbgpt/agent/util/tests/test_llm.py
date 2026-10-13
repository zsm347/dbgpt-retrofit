"""Tests for the agent LLM request boundary."""

from dbgpt.agent.util.llm.llm import _build_model_request
from dbgpt.core.interface.llm import DefaultMessageConverter
from dbgpt.core.interface.message import ModelMessageRoleType


def test_build_model_request_normalizes_openai_roles_for_default_converter():
    request = _build_model_request(
        {
            "model": "stub",
            "messages": [
                {"role": "system", "content": "instructions"},
                {"role": "user", "content": "current question"},
            ],
        }
    )

    assert [message.role for message in request.get_messages()] == [
        ModelMessageRoleType.SYSTEM,
        ModelMessageRoleType.HUMAN,
    ]
    converted = DefaultMessageConverter().convert(request.get_messages())
    assert [message.role for message in converted] == [
        ModelMessageRoleType.SYSTEM,
        ModelMessageRoleType.HUMAN,
    ]
    assert converted[-1].content == "current question"
