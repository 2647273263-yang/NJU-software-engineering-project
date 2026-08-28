"""Redacted JSON Lines export helpers."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, TextIO, cast

from forge_agent.privacy.redaction import redact_data, redact_text


def redacted_jsonl_lines(
    records: Iterable[object], *, workspace: Path | None = None
) -> Iterator[str]:
    """Yield one compact, redacted JSON object or value per input record."""

    for record in records:
        normalized = _normalize(record, workspace=workspace)
        redacted = redact_data(normalized, workspace=workspace)
        yield json.dumps(
            redacted,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


def export_redacted_jsonl(
    records: Iterable[object],
    destination: str | Path | TextIO,
    *,
    workspace: Path | None = None,
) -> int:
    """Write redacted records as JSONL and return the number of exported rows."""

    if isinstance(destination, (str, Path)):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            return _write_lines(records, stream, workspace=workspace)
    return _write_lines(records, destination, workspace=workspace)


def export_events_jsonl(
    events: Iterable[object],
    destination: str | Path | TextIO,
    *,
    workspace: Path | None = None,
) -> int:
    """Export event records after recursively redacting their fields."""

    return export_redacted_jsonl(events, destination, workspace=workspace)


def export_data_jsonl(
    data: Iterable[object],
    destination: str | Path | TextIO,
    *,
    workspace: Path | None = None,
) -> int:
    """Export arbitrary structured data after recursively redacting it."""

    return export_redacted_jsonl(data, destination, workspace=workspace)


def _write_lines(
    records: Iterable[object],
    stream: TextIO,
    *,
    workspace: Path | None,
) -> int:
    count = 0
    for line in redacted_jsonl_lines(records, workspace=workspace):
        stream.write(line)
        stream.write("\n")
        count += 1
    return count


def _normalize(value: object, *, workspace: Path | None) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize(asdict(cast(Any, value)), workspace=workspace)
    if isinstance(value, Mapping):
        return {
            str(key): _normalize(item, workspace=workspace) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(item, workspace=workspace) for item in value]
    if isinstance(value, Path):
        return redact_text(value.as_posix(), workspace=workspace)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported JSONL value: {type(value).__name__}")
