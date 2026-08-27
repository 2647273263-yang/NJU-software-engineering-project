# Local Git helper. Uses MinGit under the workspace tools folder.
# Does not write global git config.

$RepoRoot = Split-Path $PSScriptRoot -Parent
$WorkspaceRoot = Split-Path $RepoRoot -Parent
$GitExe = Join-Path $WorkspaceRoot "tools\MinGit\cmd\git.exe"

if (-not (Test-Path $GitExe)) {
    throw "找不到工作区 Git：$GitExe"
}

$script:ForgeGitName = if ($env:GIT_AUTHOR_NAME) { $env:GIT_AUTHOR_NAME } else { $env:USERNAME }
$script:ForgeGitEmail = if ($env:GIT_AUTHOR_EMAIL) { $env:GIT_AUTHOR_EMAIL } else { "$($env:USERNAME)@users.noreply.github.com" }

function Invoke-ForgeGit {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$GitArgs
    )
    Push-Location $RepoRoot
    try {
        & $GitExe `
            -c "user.name=$($script:ForgeGitName)" `
            -c "user.email=$($script:ForgeGitEmail)" `
            @GitArgs
        if ($LASTEXITCODE -ne 0) {
            throw "git $($GitArgs -join ' ') failed with exit $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}
