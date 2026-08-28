# Push local main to YOUR GitHub. Run once after creating an empty repo
# (do not initialize it with README/License on GitHub).
#
# 1) Create repo on github.com, copy the URL.
# 2) Set identity for this repo only (optional, overrides defaults):
#      $env:GIT_AUTHOR_NAME = "YourName"
#      $env:GIT_AUTHOR_EMAIL = "you@users.noreply.github.com"
# 3) Run:
#      powershell -File "D:\nanda project\forge-agent\scripts\push-github.ps1" -Remote "https://github.com/你的用户名/仓库名.git"
#
# After the first successful push, do not rebase/amend/force-push that history.

param(
    [Parameter(Mandatory = $true)]
    [string]$Remote,
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "forge-git.ps1")

$existing = & $GitExe -C $RepoRoot remote
if ($existing -notcontains "origin") {
    Invoke-ForgeGit remote add origin $Remote
}
else {
    Invoke-ForgeGit remote set-url origin $Remote
}

Invoke-ForgeGit branch -M $Branch
Invoke-ForgeGit push -u origin $Branch
Invoke-ForgeGit log --oneline
Write-Output "Pushed to $Remote ($Branch). Do not rewrite pushed commits."
