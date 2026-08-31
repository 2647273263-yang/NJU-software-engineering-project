from pathlib import Path

from forge_agent.context.user_rules import RULES_PREAMBLE, load_user_rules


def test_load_user_rules_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_user_rules(tmp_path) is None
    assert load_user_rules(tmp_path, override="  ") is None


def test_load_user_rules_reads_workspace_file(tmp_path: Path) -> None:
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "rules.md").write_text("Prefer pytest.\n", encoding="utf-8")
    rendered = load_user_rules(tmp_path)
    assert rendered is not None
    assert rendered.startswith("[user rules]")
    assert RULES_PREAMBLE in rendered
    assert "Prefer pytest." in rendered


def test_session_override_wins_over_file(tmp_path: Path) -> None:
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "rules.md").write_text("from file", encoding="utf-8")
    rendered = load_user_rules(tmp_path, override="from session")
    assert rendered is not None
    assert "from session" in rendered
    assert "from file" not in rendered
