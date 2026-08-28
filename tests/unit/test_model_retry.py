from types import SimpleNamespace

import pytest

from forge_agent.model.base import ModelError
from forge_agent.model.openai_compatible import OpenAICompatibleClient
from forge_agent.types import Message


class ScriptedCompletions:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0

    async def create(self, **_kwargs):
        step = self.steps[self.calls]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        return step


class AsyncChunks:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        self._iterator = iter(self.chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class RetryableClient(OpenAICompatibleClient):
    @staticmethod
    def _normalize_error(exc: Exception) -> ModelError:
        return ModelError(str(exc), code="temporary", retryable=True)


class NonRetryableClient(OpenAICompatibleClient):
    @staticmethod
    def _normalize_error(exc: Exception) -> ModelError:
        return ModelError(str(exc), code="authentication", retryable=False)


def successful_response():
    return SimpleNamespace(
        id="ok",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="done", tool_calls=[]),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


@pytest.mark.asyncio
async def test_retries_bounded_transient_failures(monkeypatch) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("forge_agent.model.openai_compatible.asyncio.sleep", no_sleep)
    completions = ScriptedCompletions(
        [RuntimeError("temporary-1"), RuntimeError("temporary-2"), successful_response()]
    )
    client = RetryableClient(api_key="test", model="fake", max_retries=2)
    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=completions)
    )

    response = await client.complete(
        [Message(role="user", content="hello")],
        [],
        timeout_s=1,
    )

    assert response.text == "done"
    assert completions.calls == 3


@pytest.mark.asyncio
async def test_does_not_retry_non_retryable_failure() -> None:
    completions = ScriptedCompletions([RuntimeError("bad key")])
    client = NonRetryableClient(api_key="test", model="fake", max_retries=3)
    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=completions)
    )

    with pytest.raises(ModelError, match="bad key"):
        await client.complete(
            [Message(role="user", content="hello")],
            [],
            timeout_s=1,
        )

    assert completions.calls == 1


@pytest.mark.asyncio
async def test_streams_text_and_reassembles_tool_arguments() -> None:
    chunks = AsyncChunks(
        [
            SimpleNamespace(
                id="stream",
                choices=[
                    SimpleNamespace(
                        finish_reason=None,
                        delta=SimpleNamespace(content="正在", tool_calls=[]),
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                id="stream",
                choices=[
                    SimpleNamespace(
                        finish_reason=None,
                        delta=SimpleNamespace(
                            content="处理",
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call",
                                    function=SimpleNamespace(
                                        name="write_file",
                                        arguments='{"path":"a',
                                    ),
                                )
                            ],
                        ),
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                id="stream",
                choices=[
                    SimpleNamespace(
                        finish_reason="tool_calls",
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    function=SimpleNamespace(
                                        name=None,
                                        arguments='.txt","content":"x"}',
                                    ),
                                )
                            ],
                        ),
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4),
            ),
        ]
    )
    deltas: list[str] = []
    client = OpenAICompatibleClient(
        api_key="test",
        model="fake",
        stream=True,
        on_delta=deltas.append,
    )
    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=ScriptedCompletions([chunks]))
    )

    response = await client.complete(
        [Message(role="user", content="hello")],
        [],
        timeout_s=1,
    )

    assert response.text == "正在处理"
    assert deltas == ["正在", "处理"]
    assert response.tool_calls[0].arguments == {"path": "a.txt", "content": "x"}
    assert response.usage.total_tokens == 14
