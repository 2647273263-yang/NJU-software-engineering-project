"""Small, deterministic project-context discovery."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectContext:
    project_type: str
    verification_commands: tuple[str, ...] = ()
    instructions: str | None = None
    detected_files: tuple[str, ...] = field(default_factory=tuple)

    def render(self, preferred_verification: str | None = None) -> str:
        lines = [f"Detected project type: {self.project_type}"]
        if self.detected_files:
            lines.append("Detected configuration: " + ", ".join(self.detected_files))
        commands = (
            (preferred_verification,)
            if preferred_verification
            else self.verification_commands
        )
        if commands:
            lines.append("Suggested verification commands:")
            lines.extend(f"- {command}" for command in commands)
        if self.instructions:
            lines.append("[project instructions from AGENTS.md]")
            lines.append(self.instructions)
        return "\n".join(lines)


def discover_project_context(workspace: Path) -> ProjectContext:
    detected: list[str] = []
    commands: list[str] = []
    project_types: list[str] = []

    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        detected.append("pyproject.toml")
        project_types.append("Python")
        commands.append("python -m pytest -q")
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            if "ruff" in data.get("tool", {}):
                commands.append("python -m ruff check .")
            if "mypy" in data.get("tool", {}):
                commands.append("python -m mypy src")
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            pass

    package_json = workspace / "package.json"
    if package_json.is_file():
        detected.append("package.json")
        project_types.append("Node.js")
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = package.get("scripts", {})
            runner = "pnpm" if (workspace / "pnpm-lock.yaml").exists() else "npm"
            for script in ("test", "lint", "typecheck", "build"):
                if script in scripts:
                    commands.append(
                        f"{runner} {script}" if runner == "pnpm" else f"npm run {script}"
                    )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass

    conventional = (
        ("Cargo.toml", "Rust", "cargo test"),
        ("go.mod", "Go", "go test ./..."),
        ("pom.xml", "Java/Maven", "mvn test"),
        ("build.gradle", "Java/Gradle", "gradle test"),
    )
    for filename, project_type, command in conventional:
        if (workspace / filename).is_file():
            detected.append(filename)
            project_types.append(project_type)
            commands.append(command)

    agents_file = workspace / "AGENTS.md"
    instructions = None
    if agents_file.is_file():
        detected.append("AGENTS.md")
        try:
            instructions = agents_file.read_text(encoding="utf-8")[:12_000]
        except (OSError, UnicodeDecodeError):
            instructions = None

    unique_commands = tuple(dict.fromkeys(commands))
    return ProjectContext(
        project_type=" / ".join(project_types) if project_types else "unknown",
        verification_commands=unique_commands,
        instructions=instructions,
        detected_files=tuple(detected),
    )
