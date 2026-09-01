param(
    [string]$RemoteUrl = "",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$platformRoot = Split-Path -Parent $scriptRoot

Push-Location $platformRoot
try {
    python qa/validate_github_workflow.py | Out-Null

    $hasGit = Test-Path (Join-Path $platformRoot ".git")
    $remoteConfigured = $false
    if ($hasGit) {
        try {
            $remoteName = git remote get-url origin 2>$null
            $remoteConfigured = [bool]$remoteName
        } catch {
            $remoteConfigured = $false
        }
    }

    $plan = [ordered]@{
        mode = if ($Apply) { "apply" } else { "dry_run" }
        platform_root = $platformRoot
        git_initialized = $hasGit
        workflow_validated = $true
        remote_configured = $remoteConfigured
        requested_remote = [bool]$RemoteUrl
        next_commands = @(
            "git init -b main",
            "git add <reviewed source allowlist only>",
            "python qa/validate_repository_boundary.py",
            "git commit with Codex co-author trailer"
        )
    }

    if (-not $Apply) {
        $plan | ConvertTo-Json -Depth 5
        return
    }

    if (-not $hasGit) {
        git init -b main | Out-Null
    }

    if ($RemoteUrl) {
        if ($remoteConfigured) {
            throw "origin remote is already configured"
        }
        git remote add origin $RemoteUrl
    }

    $result = [ordered]@{
        mode = "apply"
        platform_root = $platformRoot
        git_initialized = $true
        remote_configured = [bool](git remote 2>$null | Select-String '^origin$')
    }
    $result | ConvertTo-Json -Depth 5
} finally {
    Pop-Location
}
