from pathlib import Path

from forge_agent.privacy.redaction import redact_data, redact_text


def test_redacts_credentials_and_workspace() -> None:
    workspace = Path("D:/demo/project")
    text = (  # forge-release: allow
        "Bearer abc.DEF-123 "
        "user@example.com D:/demo/project/app.py "  # forge-release: allow
        "sk-secretvalue123"  # forge-release: allow
    )

    redacted = redact_text(text, workspace=workspace)

    assert "abc.DEF" not in redacted
    assert "user@example.com" not in redacted  # forge-release: allow
    assert "D:/demo/project" not in redacted
    assert "sk-secretvalue123" not in redacted
    assert "$WORKSPACE" in redacted


def test_redacts_sensitive_mapping_values() -> None:
    value = {"api_key": "secret", "nested": {"token": "secret-2"}, "safe": "ok"}

    assert redact_data(value) == {
        "api_key": "[REDACTED]",
        "nested": {"token": "[REDACTED]"},
        "safe": "ok",
    }
