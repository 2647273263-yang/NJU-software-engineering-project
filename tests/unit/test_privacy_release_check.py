from __future__ import annotations

import json
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from forge_agent.privacy import (
    RiskKind,
    export_data_jsonl,
    export_events_jsonl,
    finding_fingerprint,
    redacted_jsonl_lines,
    scan_repository,
)


def test_scan_repository_reports_file_and_content_risks_without_values(tmp_path: Path) -> None:
    repository = tmp_path / "public-repository"
    repository.mkdir()
    secret = "sk-proj-AbCdEfGhIjKlMnOpQrStUv"  # forge-release: allow
    email = "private.person@example.com"  # forge-release: allow
    user_path = r"C:\Users\PrivatePerson\Documents\notes.txt"  # forge-release: allow
    (repository / ".env").write_text(  # forge-release: allow
        f"API_KEY={secret}\n", encoding="utf-8"
    )
    (repository / "state.sqlite3").write_bytes(b"SQLite format 3\x00")
    (repository / "brief.pdf").write_bytes(b"%PDF-1.7")
    (repository / "settings.txt").write_text(
        f"contact={email}\nsource={user_path}\n", encoding="utf-8"
    )

    result = scan_repository(repository)
    serialized = json.dumps(result.to_dict())
    kinds = {finding.kind for finding in result.findings}

    assert result.clean is False
    assert result.repository == "public-repository"
    assert {
        RiskKind.ENV_FILE,
        RiskKind.DATABASE,
        RiskKind.PDF,
        RiskKind.SECRET,
        RiskKind.EMAIL,
        RiskKind.WINDOWS_USER_PATH,
    } <= kinds
    assert secret not in serialized
    assert email not in serialized
    assert "PrivatePerson" not in serialized
    assert all(not Path(finding.path).is_absolute() for finding in result.findings)
    assert len({finding_fingerprint(item) for item in result.findings}) == len(
        result.findings
    )


def test_scan_ignores_metadata_directories_and_env_templates(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        "password=should-not-be-scanned", encoding="utf-8"  # forge-release: allow
    )
    (tmp_path / ".env.example").write_text("API_KEY=example", encoding="utf-8")
    (tmp_path / "safe.py").write_text("answer = 42\n", encoding="utf-8")

    result = scan_repository(tmp_path)

    assert result.clean
    assert result.files_scanned == 2


@dataclass
class ExampleEvent:
    kind: str
    payload: dict[str, object]


def test_export_events_jsonl_redacts_dataclass_payload_and_paths(tmp_path: Path) -> None:
    workspace = Path(r"C:\Users\PrivatePerson\project")  # forge-release: allow
    token = "ghp_abcdefghijklmnopqrstuvwxyz123456"  # forge-release: allow
    email = "owner@example.com"  # forge-release: allow
    stream = StringIO()
    events = [
        ExampleEvent(
            kind="tool.finished",
            payload={
                "api_key": token,
                "message": f"Bearer {token} by {email}",
                "path": workspace / "main.py",
            },
        )
    ]

    count = export_events_jsonl(events, stream, workspace=workspace)
    exported = stream.getvalue()
    parsed = json.loads(exported)

    assert count == 1
    assert exported.endswith("\n")
    assert token not in exported
    assert email not in exported
    assert "PrivatePerson" not in exported
    assert parsed["payload"]["api_key"] == "[REDACTED]"
    assert parsed["payload"]["path"] == "$WORKSPACE/main.py"


def test_export_data_and_line_iterator_emit_valid_redacted_jsonl() -> None:
    records = [
        {  # forge-release: allow
            "password": "not-for-publication",
            "safe": "保留",
        },
        {"message": "contact me at hidden@example.org"},  # forge-release: allow
    ]
    stream = StringIO()

    count = export_data_jsonl(records, stream)
    lines = stream.getvalue().splitlines()

    assert count == 2
    assert lines == list(redacted_jsonl_lines(records))
    assert json.loads(lines[0]) == {"password": "[REDACTED]", "safe": "保留"}
    assert json.loads(lines[1]) == {"message": "contact me at [REDACTED_EMAIL]"}
