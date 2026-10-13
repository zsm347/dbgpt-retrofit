from dbgpt.model.utils.llm_utils import parse_model_request


def test_parse_model_request_preserves_native_tools():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "terminate",
                "parameters": {"type": "object"},
            },
        }
    ]

    request = parse_model_request(
        {
            "messages": [{"role": "user", "content": "完成任务"}],
            "tools": tools,
            "tool_choice": "auto",
        },
        "qwen3.8-max",
    )

    assert request.tools == tools
    assert request.tool_choice == "auto"
