"""Asynchronous, dependency-injected offline evaluation runner."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from typing import Any

from forge_agent.evaluation.models import (
    EvaluationCase,
    EvaluationContext,
    EvaluationReport,
    EvaluationRun,
    ModelFactory,
)
from forge_agent.model.base import ModelClient
from forge_agent.types import Message, ModelResponse


class _MeasuredModel:
    def __init__(self, model: ModelClient) -> None:
        self._model = model
        self.calls = 0
        self.total_tokens = 0

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        timeout_s: float,
    ) -> ModelResponse:
        self.calls += 1
        response = await self._model.complete(messages, tools, timeout_s=timeout_s)
        self.total_tokens += response.usage.total_tokens
        return response


class EvaluationRunner:
    """Run isolated cases concurrently without depending on the agent loop."""

    def __init__(
        self,
        *,
        model_factory: ModelFactory | None = None,
        max_concurrency: int = 1,
        case_timeout_s: float | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if case_timeout_s is not None and case_timeout_s <= 0:
            raise ValueError("case_timeout_s must be positive")
        self._model_factory = model_factory
        self._max_concurrency = max_concurrency
        self._case_timeout_s = case_timeout_s
        self._clock = clock

    async def run(self, cases: Sequence[EvaluationCase]) -> EvaluationReport:
        """Execute all cases and return results in input order."""

        case_ids = [case.case_id for case in cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_id values must be unique")
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def guarded(case: EvaluationCase) -> EvaluationRun:
            async with semaphore:
                return await self._run_case(case)

        runs = await asyncio.gather(*(guarded(case) for case in cases))
        return EvaluationReport(tuple(runs))

    async def _run_case(self, case: EvaluationCase) -> EvaluationRun:
        started_at = self._clock()
        measured_model: _MeasuredModel | None = None
        try:
            factory = case.model_factory or self._model_factory
            measured_model = _MeasuredModel(factory()) if factory is not None else None
            context = EvaluationContext(
                case_id=case.case_id,
                prompt=case.prompt,
                model=measured_model,
            )
            pending = case.execute(context)
            if self._case_timeout_s is None:
                outcome = await pending
            else:
                outcome = await asyncio.wait_for(pending, timeout=self._case_timeout_s)
            return EvaluationRun(
                case_id=case.case_id,
                completed=outcome.completed,
                model_calls=measured_model.calls if measured_model else 0,
                steps=outcome.steps,
                duration_s=max(0.0, self._clock() - started_at),
                resumed=outcome.resumed,
                total_tokens=measured_model.total_tokens if measured_model else 0,
                output=outcome.output,
                metadata=dict(outcome.metadata),
            )
        except Exception as exc:
            return EvaluationRun(
                case_id=case.case_id,
                completed=False,
                model_calls=measured_model.calls if measured_model else 0,
                steps=0,
                duration_s=max(0.0, self._clock() - started_at),
                resumed=False,
                total_tokens=measured_model.total_tokens if measured_model else 0,
                error=f"{type(exc).__name__}: {exc}",
            )


async def run_evaluation(
    cases: Sequence[EvaluationCase],
    *,
    model_factory: ModelFactory | None = None,
    max_concurrency: int = 1,
    case_timeout_s: float | None = None,
) -> EvaluationReport:
    """Convenience API for future CLI and GUI adapters."""

    runner = EvaluationRunner(
        model_factory=model_factory,
        max_concurrency=max_concurrency,
        case_timeout_s=case_timeout_s,
    )
    return await runner.run(cases)
