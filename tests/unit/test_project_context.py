from forge_agent.context import discover_project_context


def test_discovers_python_commands_and_agents_file(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"
        "[tool.ruff]\nline-length = 100\n",
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").write_text("Always run focused tests first.", encoding="utf-8")

    context = discover_project_context(tmp_path)
    rendered = context.render()

    assert context.project_type == "Python"
    assert "python -m pytest -q" in context.verification_commands
    assert "python -m ruff check ." in context.verification_commands
    assert "Always run focused tests first." in rendered


def test_preferred_verification_replaces_suggestions(tmp_path) -> None:
    (tmp_path / "go.mod").write_text("module example", encoding="utf-8")

    rendered = discover_project_context(tmp_path).render("make verify")

    assert "make verify" in rendered
    assert "go test ./..." not in rendered
