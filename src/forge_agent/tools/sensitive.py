"""Block content access to secrets that still sit inside the workspace."""

from __future__ import annotations

_SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist")
_CREDENTIAL_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".gpg", ".docker"})
_PRIVATE_KEY_NAMES = frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})
_PRIVATE_KEY_SUFFIXES = frozenset({".pem", ".p12", ".pfx", ".jks", ".ppk"})


def sensitive_read_reason(relative_posix: str) -> str | None:
    """Return why this workspace-relative path must not be read, or None."""

    relative = relative_posix.replace("\\", "/").strip("/")
    if not relative:
        return None
    parts = [part.lower() for part in relative.split("/") if part and part != "."]
    if not parts:
        return None
    name = parts[-1]

    if parts[0] == ".git":
        return ".git internals"
    if any(part in _CREDENTIAL_DIRS for part in parts):
        return "credential directory"
    if name == ".env" or (
        name.startswith(".env.")
        and not any(name.endswith(suffix) for suffix in _SAFE_ENV_SUFFIXES)
    ):
        return ".env"
    if name in _PRIVATE_KEY_NAMES:
        return "private key"
    suffix = ""
    if "." in name:
        suffix = "." + name.rsplit(".", 1)[-1]
    if suffix in _PRIVATE_KEY_SUFFIXES:
        return "private key"
    return None
