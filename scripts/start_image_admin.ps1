param(
    [ValidatePattern('^[A-Za-z0-9_-]{1,100}$')]
    [string]$RunId = '2026-09-03-incremental-review-500-v1',
    [ValidateRange(1, 65535)]
    [int]$Port = 8964,
    [string]$SeedDecisions = '',
    [switch]$Start
)

$ErrorActionPreference = 'Stop'
$archiveRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$serverScript = Join-Path $archiveRoot 'src\serve_image_admin.py'
$pythonExe = (Get-Command python -CommandType Application | Select-Object -First 1).Source
$serverArguments = @('-B', '-X', 'utf8', $serverScript, '--run-id', $RunId, '--port', "$Port")
if ($SeedDecisions) {
    $resolvedSeed = (Resolve-Path -LiteralPath $SeedDecisions).Path
    $serverArguments += @('--seed-decisions', $resolvedSeed)
}
if (-not $Start) {
    & $pythonExe @serverArguments
    exit $LASTEXITCODE
}

# Do not stop or replace any other process using the requested port.
$portProbe = New-Object System.Net.Sockets.TcpClient
try {
    $portProbe.Connect('127.0.0.1', $Port)
    throw "Port $Port is already in use. Use the running manager or choose another port."
} catch [System.Net.Sockets.SocketException] {
    # Expected when no process is listening. The server bind still checks races.
} finally {
    $portProbe.Dispose()
}

$runtimeDirectory = Join-Path $archiveRoot 'data\private-research\image-rag-admin\runtime'
New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
$launchStamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-ffff')
$stdoutPath = Join-Path $runtimeDirectory "$launchStamp.stdout.log"
$stderrPath = Join-Path $runtimeDirectory "$launchStamp.stderr.log"
$serverArguments += '--serve'
# PowerShell Start-Process joins arguments: quote paths without invoking another shell.
$quotedArguments = $serverArguments | ForEach-Object { '"' + $_ + '"' }
$launched = Start-Process -FilePath $pythonExe -ArgumentList $quotedArguments -WorkingDirectory $archiveRoot `
    -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
[PSCustomObject]@{
    status = 'starting'
    process_id = $launched.Id
    url = "http://127.0.0.1:$Port/"
    run_id = $RunId
    stdout = $stdoutPath
    stderr = $stderrPath
    note = 'Local-only process; no Windows startup task is installed.'
} | ConvertTo-Json
