from types import SimpleNamespace

from forge_agent.model.openai_compatible import OpenAICompatibleClient


def test_parses_tool_call_and_usage() -> None:
    response = SimpleNamespace(
        id="request-1",
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            function=SimpleNamespace(
                                name="read_file",
                                arguments='{"path":"app.py"}',
                            ),
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4),
    )

    parsed = OpenAICompatibleClient._parse_response(response)

    assert parsed.request_id == "request-1"
    assert parsed.tool_calls[0].name == "read_file"
    assert parsed.tool_calls[0].arguments == {"path": "app.py"}
    assert parsed.usage.total_tokens == 14


def test_preserves_invalid_raw_tool_arguments() -> None:
    response = SimpleNamespace(
        id="request-2",
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call-2",
                            function=SimpleNamespace(name="read_file", arguments="{bad json"),
                        )
                    ],
                ),
            )
        ],
        usage=None,
    )

    parsed = OpenAICompatibleClient._parse_response(response)

    assert parsed.tool_calls[0].arguments == {}
    assert parsed.tool_calls[0].raw_arguments == "{bad json"
    assert parsed.usage.estimated
