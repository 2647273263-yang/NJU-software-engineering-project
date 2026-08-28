"""Privacy checks for repositories that are about to be made public."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

_IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}
_DATABASE_SUFFIXES = {".accdb", ".db", ".duckdb", ".mdb", ".sqlite", ".sqlite3"}
_ENV_SAFE_SUFFIXES = {".example", ".sample", ".template"}
_TEXT_SAMPLE_BYTES = 2 * 1024 * 1024
_ALLOW_MARKER = "forge-release: allow"


class RiskKind(StrEnum):
    """Kinds of private material that should block a public release."""

    ENV_FILE = "env_file"
    DATABASE = "database"
    PDF = "pdf"
    SECRET = "secret"
    EMAIL = "email"
    WINDOWS_USER_PATH = "windows_user_path"


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"


@dataclass(frozen=True, slots=True)
class PrivacyFinding:
    """A finding containing location metadata, never matched source text."""

    kind: RiskKind
    severity: Severity
    path: str
    detector: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReleaseCheckResult:
    """Structured result of a repository privacy scan."""

    repository: str
    files_scanned: int
    files_skipped: int
    findings: tuple[PrivacyFinding, ...]

    @property
    def clean(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
            "clean": self.clean,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True, slots=True)
class _ContentDetector:
    kind: RiskKind
    severity: Severity
    name: str
    pattern: re.Pattern[str]


_CONTENT_DETECTORS = (
    _ContentDetector(
        RiskKind.SECRET,
        Severity.HIGH,
        "private_key",
        re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    ),
    _ContentDetector(
        RiskKind.SECRET,
        Severity.HIGH,
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    _ContentDetector(
        RiskKind.SECRET,
        Severity.HIGH,
        "service_token",
        re.compile(
            r"\b(?:gh[opusr]_[A-Za-z0-9]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{16,})\b"
        ),
    ),
    _ContentDetector(
        RiskKind.SECRET,
        Severity.HIGH,
        "credential_assignment",
        re.compile(
            r"""(?ix)
            \b(?:api[-_]?key|access[-_]?token|auth[-_]?token|client[-_]?secret|
            password|passwd|secret)\b
            [ \t]*(?::|=)[ \t]*
            (?:
                ["'](?!change[-_]?me|example|placeholder|redacted|test|xxx)
                [^"'\r\n]{8,}["']
                |
                (?!change[-_]?me|example|placeholder|redacted|test|xxx)
                [A-Za-z0-9_+/=-]{12,}
            )
            """
        ),
    ),
    _ContentDetector(
        RiskKind.EMAIL,
        Severity.MEDIUM,
        "email_address",
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    ),
    _ContentDetector(
        RiskKind.WINDOWS_USER_PATH,
        Severity.MEDIUM,
        "windows_user_directory",
        re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\s:\"<>|?*]+"),
    ),
)


def scan_repository(
    repository: str | Path, *, max_file_bytes: int = _TEXT_SAMPLE_BYTES
) -> ReleaseCheckResult:
    """Scan a local repository before publication without returning secret values."""

    root = Path(repository)
    if not root.is_dir():
        raise NotADirectoryError(root)
    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes must be positive")

    findings: list[PrivacyFinding] = []
    files_scanned = 0
    files_skipped = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in _IGNORED_DIRECTORIES for part in relative.parts[:-1]):
            continue

        relative_name = relative.as_posix()
        findings.extend(_filename_findings(path, relative_name))
        try:
            size = path.stat().st_size
        except OSError:
            files_skipped += 1
            continue
        if size > max_file_bytes or path.suffix.lower() in _DATABASE_SUFFIXES | {".pdf"}:
            files_skipped += 1
            continue
        try:
            content = path.read_bytes()
        except OSError:
            files_skipped += 1
            continue
        if b"\x00" in content:
            files_skipped += 1
            continue

        files_scanned += 1
        text = content.decode("utf-8", errors="replace")
        findings.extend(_content_findings(text, relative_name))

    findings.sort(key=lambda item: (item.path, item.line or 0, item.kind, item.detector))
    return ReleaseCheckResult(
        repository=root.name,
        files_scanned=files_scanned,
        files_skipped=files_skipped,
        findings=tuple(findings),
    )


def _filename_findings(path: Path, relative_name: str) -> list[PrivacyFinding]:
    name = path.name.lower()
    suffix = path.suffix.lower()
    findings: list[PrivacyFinding] = []
    if name == ".env" or (
        name.startswith(".env.") and not any(name.endswith(item) for item in _ENV_SAFE_SUFFIXES)
    ):
        findings.append(
            PrivacyFinding(
                RiskKind.ENV_FILE, Severity.HIGH, relative_name, "environment_file"
            )
        )
    if suffix in _DATABASE_SUFFIXES:
        findings.append(
            PrivacyFinding(RiskKind.DATABASE, Severity.HIGH, relative_name, "database_file")
        )
    if suffix == ".pdf":
        findings.append(
            PrivacyFinding(RiskKind.PDF, Severity.MEDIUM, relative_name, "pdf_document")
        )
    return findings


def _content_findings(text: str, relative_name: str) -> list[PrivacyFinding]:
    findings: list[PrivacyFinding] = []
    seen: set[tuple[RiskKind, int]] = set()
    for line, content in enumerate(text.splitlines(), 1):
        if _ALLOW_MARKER in content:
            continue
        for detector in _CONTENT_DETECTORS:
            if detector.pattern.search(content):
                key = (detector.kind, line)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    PrivacyFinding(
                        detector.kind,
                        detector.severity,
                        relative_name,
                        detector.name,
                        line,
                    )
                )
    return findings


def finding_fingerprint(finding: PrivacyFinding) -> str:
    """Return a stable fingerprint derived only from non-secret finding metadata."""

    metadata = (
        f"{finding.kind}\0{finding.path}\0{finding.detector}\0{finding.line or 0}"
    ).encode()
    return hashlib.sha256(metadata).hexdigest()[:16]
