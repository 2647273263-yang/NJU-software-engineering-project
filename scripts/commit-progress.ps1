# Periodic progress commit. Run from anywhere:
#   powershell -File "D:\nanda project\forge-agent\scripts\commit-progress.ps1" -Message "gui: 调整核对页文案"
#
# Add -Push after you have origin and GitHub login.
# Never amend or force-push commits that are already on GitHub.

param(
    [Parameter(Mandatory = $true)]
    [string]$Message,
    [switch]$Push
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "forge-git.ps1")

Invoke-ForgeGit status
Invoke-ForgeGit add -A
$status = & $GitExe -C $RepoRoot status --porcelain
if (-not $status) {
    Write-Output "没有需要提交的改动。"
    exit 0
}

Invoke-ForgeGit commit -m $Message
Invoke-ForgeGit status
Invoke-ForgeGit log -5 --oneline

if ($Push) {
    Invoke-ForgeGit push -u origin HEAD
    Write-Output "已推送到 origin。"
}
else {
    Write-Output "已提交到本地。确认无误后再运行：powershell -File `"$PSScriptRoot\push-github.ps1`""
}
