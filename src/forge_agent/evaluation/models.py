"""Provider-neutral data models for offline evaluation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field

from forge_agent.model.base import ModelClient

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type ModelFactory = Callable[[], ModelClient]


@dataclass(slots=True)
class EvaluationOutcome:
    """Outcome supplied by a case callable after executing its scenario."""

    completed: bool
    steps: int
    resumed: bool = False
    output: str | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(slots=True)
class EvaluationContext:
    """Dependencies made available to an evaluation case."""

    case_id: str
    prompt: str
    model: ModelClient | None = None


type CaseCallable = Callable[[EvaluationContext], Awaitable[EvaluationOutcome]]


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """One deterministic task and its injectable execution dependencies."""

    case_id: str
    prompt: str
    execute: CaseCallable
    model_factory: ModelFactory | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id must not be empty")
        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    """Measured result of a single evaluation case."""

    case_id: str
    completed: bool
    model_calls: int
    steps: int
    duration_s: float
    resumed: bool
    total_tokens: int
    output: str | None = None
    error: str | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Aggregate metrics plus the underlying case runs."""

    runs: tuple[EvaluationRun, ...]

    @property
    def total_cases(self) -> int:
        return len(self.runs)

    @property
    def completion_rate(self) -> float:
        return self._ratio(sum(run.completed for run in self.runs), self.total_cases)

    @property
    def average_model_calls(self) -> float:
        return self._average(run.model_calls for run in self.runs)

    @property
    def average_steps(self) -> float:
        return self._average(run.steps for run in self.runs)

    @property
    def average_duration_s(self) -> float:
        return self._average(run.duration_s for run in self.runs)

    @property
    def recovery_rate(self) -> float:
        resumed = tuple(run for run in self.runs if run.resumed)
        return self._ratio(sum(run.completed for run in resumed), len(resumed))

    @property
    def average_tokens(self) -> float:
        return self._average(run.total_tokens for run in self.runs)

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 0.0

    def _average(self, values: Iterable[int | float]) -> float:
        typed_values = tuple(values)
        return sum(typed_values) / len(typed_values) if typed_values else 0.0
