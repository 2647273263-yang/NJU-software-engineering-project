"""Human-facing Git checkpoints for a workspace. Not exposed as Agent tools."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from forge_agent.gitbin import resolve_git_executable

_BRANCH_RE = re.compile(r"^(?!-)[A-Za-z0-9._/\-]{1,80}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_TRUNK_NAMES = ("main", "master")
_PROXY_ENV = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
}

_RETRYABLE_PUSH = (
    "proxy",
    "could not connect",
    "failed to connect",
    "connection was reset",
    "connection reset",
    "recv failure",
    "timed out",
    "timeout",
    "empty reply",
    "early eof",
    "ssl",
    "schannel",
    "rpc failed",
    "failed sending",
    "openssl",
    "curl 56",
    "curl 7",
    "curl 28",
    "curl 35",
)

_NOT_RETRYABLE_PUSH = (
    "authentication failed",
    "could not read username",
    "permission denied",
    "non-fast-forward",
    "fetch first",
    "rejected",
)


class GitWorkspaceError(ValueError):
    """User-visible Git error."""


def git_snapshot(workspace: Path) -> dict[str, Any]:
    git = resolve_git_executable()
    if git is None:
        return {
            "available": False,
            "git_path": None,
            "repo": False,
            "reason": "工作区未找到 Git。请确认 tools/MinGit 仍在项目目录中。",
            "branch": "",
            "branches": [],
            "commits": [],
            "dirty": [],
            "remote_url": "",
            "branch_nodes": [],
        }
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise GitWorkspaceError("工作区文件夹不存在")
    inside = _run(git, workspace, ["rev-parse", "--is-inside-work-tree"], check=False)
    if inside.code != 0 or inside.stdout.strip() != "true":
        return {
            "available": True,
            "git_path": git,
            "repo": False,
            "reason": "",
            "branch": "",
            "branches": [],
            "commits": [],
            "dirty": [],
            "remote_url": "",
            "branch_nodes": [],
        }
    branch = _run(git, workspace, ["branch", "--show-current"]).stdout.strip()
    branches_out = _run(git, workspace, ["branch", "--format=%(refname:short)"]).stdout
    branches = [line.strip() for line in branches_out.splitlines() if line.strip()]
    commits = _parse_commits(
        _run(
            git,
            workspace,
            [
                "log",
                "-40",
                "--format=%H%x1f%h%x1f%an%x1f%ad%x1f%s",
                "--date=iso-strict",
            ],
            check=False,
        ).stdout
    )
    dirty = _parse_dirty(_run(git, workspace, ["status", "--porcelain", "-uall"]).stdout)
    remote = _run(git, workspace, ["remote", "get-url", "origin"], check=False)
    return {
        "available": True,
        "git_path": git,
        "repo": True,
        "reason": "",
        "branch": branch,
        "branches": branches,
        "commits": commits,
        "dirty": dirty,
        "remote_url": remote.stdout.strip() if remote.code == 0 else "",
        "branch_nodes": _branch_nodes(git, workspace, branches, branch),
    }


def git_init(workspace: Path) -> dict[str, Any]:
    git = _require_git()
    workspace = _require_dir(workspace)
    existing = _run(git, workspace, ["rev-parse", "--is-inside-work-tree"], check=False)
    if existing.code == 0 and existing.stdout.strip() == "true":
        raise GitWorkspaceError("这个文件夹已经在 Git 仓库里")
    result = _run(git, workspace, ["init", "-b", "main"])
    if result.code != 0:
        raise GitWorkspaceError(result.stderr or result.stdout or "Git 初始化失败")
    return git_snapshot(workspace)


def git_commit(workspace: Path, message: str) -> dict[str, Any]:
    git = _require_git()
    workspace = _require_dir(workspace)
    _require_repo(git, workspace)
    text = message.strip()
    if not text:
        raise GitWorkspaceError("请填写这次保存的说明")
    add = _run(git, workspace, ["add", "-A"])
    if add.code != 0:
        raise GitWorkspaceError(add.stderr or "暂存文件失败")
    status = _run(git, workspace, ["status", "--porcelain"])
    if not status.stdout.strip():
        raise GitWorkspaceError("没有新的改动可以保存")
    name, email = _identity(git, workspace)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".txt",
        delete=False,
        newline="\n",
    ) as handle:
        handle.write(text)
        handle.write("\n")
        msg_path = handle.name
    try:
        result = _run(
            git,
            workspace,
            ["commit", "-F", msg_path],
            config={"user.name": name, "user.email": email},
        )
    finally:
        Path(msg_path).unlink(missing_ok=True)
    if result.code != 0:
        raise GitWorkspaceError(result.stderr or result.stdout or "保存版本失败")
    return git_snapshot(workspace)


def git_create_branch(workspace: Path, name: str) -> dict[str, Any]:
    git = _require_git()
    workspace = _require_dir(workspace)
    _require_repo(git, workspace)
    branch = _validate_branch(name)
    if not _has_commits(git, workspace):
        raise GitWorkspaceError("请先保存一个版本，再开新分支")
    result = _run(git, workspace, ["switch", "-c", branch])
    if result.code != 0:
        raise GitWorkspaceError(_friendly_switch_error(result))
    return git_snapshot(workspace)


def git_switch_branch(workspace: Path, name: str) -> dict[str, Any]:
    git = _require_git()
    workspace = _require_dir(workspace)
    _require_repo(git, workspace)
    branch = _validate_branch(name)
    result = _run(git, workspace, ["switch", branch])
    if result.code != 0:
        raise GitWorkspaceError(_friendly_switch_error(result))
    return git_snapshot(workspace)


def git_set_remote(workspace: Path, url: str) -> dict[str, Any]:
    git = _require_git()
    workspace = _require_dir(workspace)
    _require_repo(git, workspace)
    remote = _validate_github_url(url)
    existing = _run(git, workspace, ["remote"], check=False)
    names = {line.strip() for line in existing.stdout.splitlines() if line.strip()}
    if "origin" in names:
        result = _run(git, workspace, ["remote", "set-url", "origin", remote])
    else:
        result = _run(git, workspace, ["remote", "add", "origin", remote])
    if result.code != 0:
        raise GitWorkspaceError(result.stderr or "保存 GitHub 地址失败")
    return git_snapshot(workspace)


def git_push(workspace: Path) -> dict[str, Any]:
    git = _require_git()
    workspace = _require_dir(workspace)
    _require_repo(git, workspace)
    if not _has_commits(git, workspace):
        raise GitWorkspaceError("请先保存一个版本，再推送到 GitHub")
    branch = _run(git, workspace, ["branch", "--show-current"]).stdout.strip()
    if not branch:
        raise GitWorkspaceError("当前不在任何分支上，无法推送")
    remote = _run(git, workspace, ["remote", "get-url", "origin"], check=False)
    if remote.code != 0 or not remote.stdout.strip():
        raise GitWorkspaceError("请先填写 GitHub 仓库地址")
    pushed = _push_current_branch(git, workspace, branch)
    if pushed.code != 0:
        raise GitWorkspaceError(_friendly_push_error(pushed))
    return git_snapshot(workspace)


def git_restore(workspace: Path, commit: str, *, confirm: bool, clean_untracked: bool) -> dict[str, Any]:
    git = _require_git()
    workspace = _require_dir(workspace)
    _require_repo(git, workspace)
    if not confirm:
        raise GitWorkspaceError("恢复会覆盖当前文件，请确认后再试")
    sha = commit.strip()
    if not _SHA_RE.fullmatch(sha):
        raise GitWorkspaceError("无效的版本号")
    probe = _run(git, workspace, ["rev-parse", "--verify", f"{sha}^{{commit}}"], check=False)
    if probe.code != 0:
        raise GitWorkspaceError("找不到这个版本")
    reset = _run(git, workspace, ["reset", "--hard", sha])
    if reset.code != 0:
        raise GitWorkspaceError(reset.stderr or "恢复失败")
    if clean_untracked:
        cleaned = _run(git, workspace, ["clean", "-fd"])
        if cleaned.code != 0:
            raise GitWorkspaceError(cleaned.stderr or "清理未跟踪文件失败")
    return git_snapshot(workspace)


def _require_git() -> str:
    git = resolve_git_executable()
    if git is None:
        raise GitWorkspaceError("工作区未找到 Git。请确认 tools/MinGit 仍在项目目录中。")
    return git


def _require_dir(workspace: Path) -> Path:
    resolved = workspace.expanduser().resolve()
    if not resolved.is_dir():
        raise GitWorkspaceError("工作区文件夹不存在")
    return resolved


def _require_repo(git: str, workspace: Path) -> None:
    inside = _run(git, workspace, ["rev-parse", "--is-inside-work-tree"], check=False)
    if inside.code != 0 or inside.stdout.strip() != "true":
        raise GitWorkspaceError("这个文件夹还不是 Git 仓库，请先开始版本控制")


def _has_commits(git: str, workspace: Path) -> bool:
    result = _run(git, workspace, ["rev-parse", "--verify", "HEAD"], check=False)
    return result.code == 0


def _validate_branch(name: str) -> str:
    branch = name.strip()
    if not _BRANCH_RE.fullmatch(branch) or ".." in branch:
        raise GitWorkspaceError("分支名只能用字母、数字、斜杠、点和下划线")
    return branch


def _identity(git: str, workspace: Path) -> tuple[str, str]:
    name = os.environ.get("GIT_AUTHOR_NAME", "").strip() or _config(git, workspace, "user.name")
    email = os.environ.get("GIT_AUTHOR_EMAIL", "").strip() or _config(git, workspace, "user.email")
    return name or "ForgeAgent", email or "forge-agent@users.noreply.github.com"


def _config(git: str, workspace: Path, key: str) -> str:
    result = _run(git, workspace, ["config", "--get", key], check=False)
    return result.stdout.strip() if result.code == 0 else ""


def _parse_commits(text: str) -> list[dict[str, str]]:
    commits: list[dict[str, str]] = []
    for line in text.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        sha, short, author, when, subject = parts
        commits.append(
            {
                "sha": sha,
                "short": short,
                "author": author,
                "when": when,
                "message": subject,
            }
        )
    return commits


def _parse_dirty(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if code == "??":
            kind = "untracked"
        elif code.strip() == "D" or code[1] == "D":
            kind = "deleted"
        elif "A" in code:
            kind = "added"
        else:
            kind = "modified"
        rows.append({"path": path.replace("\\", "/"), "kind": kind})
    return rows[:200]


def _trunk_name(branches: list[str]) -> str:
    for name in _TRUNK_NAMES:
        if name in branches:
            return name
    return ""


def _ref_short(value: str) -> str:
    text = value.strip()
    if text.startswith("refs/heads/"):
        return text[len("refs/heads/") :]
    return text


def _moving_from(line: str, dest: str) -> str:
    if "moving from " not in line or " to " not in line:
        return ""
    mid = line.split("moving from ", 1)[1]
    source, _, target = mid.partition(" to ")
    source = _ref_short(source)
    target = _ref_short(target)
    if target == dest and source and source != dest:
        return source
    return ""


def _oldest_moving_to(text: str, dest: str) -> str:
    oldest = ""
    for line in reversed(text.splitlines()):
        found = _moving_from(line, dest)
        if found:
            oldest = found
    return oldest


def _created_from(git: str, workspace: Path, name: str) -> str:
    if name in _TRUNK_NAMES:
        return ""
    own = _run(git, workspace, ["reflog", "show", name, "--format=%gs"], check=False).stdout
    for line in own.splitlines():
        if "Created from" not in line:
            continue
        source = _ref_short(line.split("Created from", 1)[1])
        if source and source not in {"HEAD", name}:
            return source
    oldest_own = _oldest_moving_to(own, name)
    if oldest_own:
        return oldest_own
    head = _run(git, workspace, ["reflog", "show", "HEAD", "--format=%gs"], check=False).stdout
    return _oldest_moving_to(head, name)


def _ahead_count(git: str, workspace: Path, base: str, head: str) -> int:
    result = _run(git, workspace, ["rev-list", "--count", f"{base}..{head}"], check=False)
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return 0


def _branch_nodes(
    git: str,
    workspace: Path,
    branches: list[str],
    current: str,
) -> list[dict[str, Any]]:
    if not branches:
        return []
    tips: dict[str, str] = {}
    for name in branches:
        parsed = _run(git, workspace, ["rev-parse", name], check=False)
        if parsed.code == 0 and parsed.stdout.strip():
            tips[name] = parsed.stdout.strip()
    known = set(tips)
    trunk = _trunk_name(branches)
    parent_of: dict[str, str] = {name: "" for name in branches}
    for name in branches:
        if name == trunk or name in _TRUNK_NAMES:
            parent_of[name] = ""
            continue
        created = _created_from(git, workspace, name)
        if created in known and created != name:
            parent_of[name] = created
        else:
            parent_of[name] = trunk

    for name in list(parent_of):
        if name == trunk or name in _TRUNK_NAMES:
            parent_of[name] = ""
            continue
        seen = {name}
        cursor = parent_of.get(name, "")
        while cursor:
            if cursor in seen:
                parent_of[name] = trunk
                break
            seen.add(cursor)
            cursor = parent_of.get(cursor, "")
        if parent_of[name] == name:
            parent_of[name] = trunk

    children: dict[str, list[str]] = {name: [] for name in branches}
    roots: list[str] = []
    for name in branches:
        parent = parent_of.get(name, "")
        if parent and parent in children:
            children[parent].append(name)
        else:
            roots.append(name)

    def sort_key(name: str) -> tuple[int, str]:
        return (0 if name == trunk else 1, name)

    roots.sort(key=sort_key)
    for name in children:
        children[name].sort(key=sort_key)

    ordered: list[dict[str, Any]] = []

    def walk(name: str, depth: int) -> None:
        parent = parent_of.get(name, "")
        parent_tip = tips.get(parent, "")
        tip = tips.get(name, "")
        same = bool(parent and tip and tip == parent_tip)
        ahead = _ahead_count(git, workspace, parent, name) if parent and tip else 0
        if not parent:
            label = "主干"
        elif same:
            label = f"从 {parent} 分出，还在同一版本"
        elif ahead:
            label = f"从 {parent} 分出，多 {ahead} 个版本"
        else:
            label = f"从 {parent} 分出"
        ordered.append(
            {
                "name": name,
                "parent": parent,
                "depth": depth,
                "current": name == current,
                "same_version": same,
                "ahead": ahead,
                "label": label,
            }
        )
        for child in children.get(name, []):
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    return ordered


def _validate_github_url(url: str) -> str:
    text = url.strip()
    if not text or text.startswith("-"):
        raise GitWorkspaceError("请填写 GitHub 仓库地址")
    if "@" in text.replace("git@github.com:", "", 1) and "://" in text:
        raise GitWorkspaceError("不要把账号或令牌写进地址里")
    https = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/([A-Za-z0-9._-]+?)(?:\.git)?/?",
        text,
        flags=re.IGNORECASE,
    )
    ssh = re.fullmatch(
        r"git@github\.com:([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/([A-Za-z0-9._-]+?)(?:\.git)?",
        text,
        flags=re.IGNORECASE,
    )
    matched = https or ssh
    if matched is None:
        raise GitWorkspaceError("地址格式应为 https://github.com/用户名/仓库名.git")
    owner, repo = matched.group(1), matched.group(2)
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return f"https://github.com/{owner}/{repo}.git"


def _push_current_branch(git: str, workspace: Path, branch: str) -> _Run:
    spec = f"refs/heads/{branch}:refs/heads/{branch}"
    if spec.startswith("+") or "--force" in spec:
        raise GitWorkspaceError("拒绝推送危险引用")
    args = ["push", "-u", "origin", spec]
    attempts: list[tuple[dict[str, str], dict[str, str] | None]] = [
        ({"http.version": "HTTP/1.1"}, None),
        (
            {"http.version": "HTTP/1.1", "http.proxy": "", "https.proxy": ""},
            _env_without_proxy(),
        ),
    ]
    last: _Run | None = None
    for config, env in attempts:
        result = _run(
            git,
            workspace,
            args,
            check=False,
            timeout=60,
            config=config,
            env=env,
        )
        last = result
        if result.code == 0 or not _retryable_push_error(result):
            return result
    assert last is not None
    return last


def _env_without_proxy() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key in _PROXY_ENV or key.upper() in _PROXY_ENV:
            env.pop(key, None)
    return env


def _retryable_push_error(result: "_Run") -> bool:
    text = f"{result.stderr}\n{result.stdout}".lower()
    if any(hint in text for hint in _NOT_RETRYABLE_PUSH):
        return False
    return any(hint in text for hint in _RETRYABLE_PUSH)


def _friendly_push_error(result: "_Run") -> str:
    text = (result.stderr or result.stdout).strip()
    lower = text.lower()
    if "authentication failed" in lower or "could not read username" in lower:
        return "GitHub 登录失败。请在系统里登录 GitHub，或配置凭据后再推送。"
    if "permission denied" in lower or "403" in lower:
        return "没有这个仓库的写入权限，请确认地址和 GitHub 账号是否匹配。"
    if "non-fast-forward" in lower or "fetch first" in lower or "rejected" in lower:
        return "远程已有本地没有的提交。界面不会强推。请换一个空仓库，或先在终端处理后再推。"
    if (
        "connection was reset" in lower
        or "connection reset" in lower
        or "recv failure" in lower
    ):
        return "连 GitHub 时连接被重置，推送没有成功。请检查系统代理或网络后再试。"
    if "could not connect" in lower or "failed to connect" in lower or "timed out" in lower or "timeout" in lower:
        return "连不上 GitHub。请检查网络或系统代理后再试。"
    return text or "推送到 GitHub 失败"


def _friendly_switch_error(result: "_Run") -> str:
    text = (result.stderr or result.stdout).strip()
    lower = text.lower()
    if "already exists" in lower:
        return "这个分支名已经存在"
    if "did not match" in lower or "unknown" in lower:
        return "找不到这个分支"
    if "overwritten" in lower or "please commit" in lower or "uncommitted" in lower:
        return "当前还有未保存的改动。请先保存这个版本，或恢复到某个版本后再切换分支"
    return text or "切换分支失败"


class _Run:
    def __init__(self, code: int, stdout: str, stderr: str) -> None:
        self.code = code
        self.stdout = stdout
        self.stderr = stderr


def _run(
    git: str,
    workspace: Path,
    args: list[str],
    *,
    check: bool = True,
    config: dict[str, str] | None = None,
    timeout: int = 60,
    env: dict[str, str] | None = None,
) -> _Run:
    command = [git]
    for key, value in (config or {}).items():
        command.extend(["-c", f"{key}={value}"])
    command.extend(["-C", str(workspace), *args])
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "timeout": timeout,
        "check": False,
        "cwd": str(workspace),
    }
    if env is not None:
        kwargs["env"] = env
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        completed = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
        result = _Run(124, stdout, stderr or "Git 命令超时")
        if check:
            raise GitWorkspaceError(result.stderr.strip() or "Git 命令超时") from exc
        return result
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    result = _Run(completed.returncode, stdout, stderr)
    if check and result.code != 0:
        raise GitWorkspaceError(stderr.strip() or stdout.strip() or "Git 命令失败")
    return result
