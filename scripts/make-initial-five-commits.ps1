$ErrorActionPreference = "Stop"
. "D:\nanda project\forge-agent\scripts\forge-git.ps1"

function Commit-Slice([string]$MessageFile) {
    Invoke-ForgeGit commit -F $MessageFile
}

if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
    Invoke-ForgeGit init -b main
}

$msg = Join-Path $PSScriptRoot "commit-messages"

Invoke-ForgeGit add -- `
    .gitignore LICENSE README.md pyproject.toml .pre-commit-config.yaml .env.example `
    scripts `
    src/forge_agent/__init__.py src/forge_agent/types.py src/forge_agent/config.py `
    src/forge_agent/model
Commit-Slice (Join-Path $msg "01.txt")

Invoke-ForgeGit add -- src/forge_agent/tools src/forge_agent/safety `
    src/forge_agent/agent/__init__.py `
    src/forge_agent/agent/prompts.py `
    src/forge_agent/agent/state.py `
    src/forge_agent/agent/state_machine.py `
    src/forge_agent/agent/tool_runtime.py
Commit-Slice (Join-Path $msg "02.txt")

Invoke-ForgeGit add -- `
    src/forge_agent/agent/loop.py `
    src/forge_agent/agent/evidence.py `
    src/forge_agent/agent/completion.py `
    src/forge_agent/agent/hypothesis.py `
    src/forge_agent/context `
    src/forge_agent/storage `
    src/forge_agent/application
Commit-Slice (Join-Path $msg "03.txt")

Invoke-ForgeGit add -- `
    src/forge_agent/cli.py `
    src/forge_agent/ui `
    src/forge_agent/privacy `
    src/forge_agent/evaluation `
    tests/unit/test_agent_loop.py `
    tests/unit/test_application.py `
    tests/unit/test_completion.py `
    tests/unit/test_context.py `
    tests/unit/test_evaluation.py `
    tests/unit/test_evidence.py `
    tests/unit/test_hypothesis.py `
    tests/unit/test_model_adapter.py `
    tests/unit/test_model_retry.py `
    tests/unit/test_privacy_release_check.py `
    tests/unit/test_project_context.py `
    tests/unit/test_redaction.py `
    tests/unit/test_replay.py `
    tests/unit/test_repo_tools.py `
    tests/unit/test_safety.py `
    tests/unit/test_state_machine.py `
    tests/unit/test_storage_sqlite.py `
    tests/unit/test_tools.py `
    tests/integration/test_coding_task.py `
    tests/integration/test_persistent_undo.py `
    tests/integration/test_process_cleanup.py `
    tests/integration/test_resume.py
Commit-Slice (Join-Path $msg "04.txt")

Invoke-ForgeGit add -- `
    src/forge_agent/gui `
    web `
    tests/unit/test_gui_server.py `
    tests/unit/test_gui_viewmodels.py `
    tests/integration/test_gui_flow.py
Commit-Slice (Join-Path $msg "05.txt")

Invoke-ForgeGit status
Invoke-ForgeGit log --oneline
