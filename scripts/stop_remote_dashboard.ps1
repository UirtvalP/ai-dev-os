$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$dashboardProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and
    $_.CommandLine -match 'dashboard serve REQ-020' -and
    $_.CommandLine -match [regex]::Escape($projectRoot)
}
$reverseForward = '127.0.0.1:18765:127.0.0.1:8765'
$sshProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'ssh.exe' -and $_.CommandLine -match [regex]::Escape($reverseForward)
}

$processIds = @($dashboardProcesses.ProcessId) + @($sshProcesses.ProcessId) |
    Where-Object { $_ } |
    Sort-Object -Unique
if ($processIds) {
    Stop-Process -Id $processIds -Force
}

[pscustomobject]@{
    Status = 'stopped'
    ProcessIds = @($processIds)
    PreservedServices = @('homebox-relay', 'codex-cursor Named Tunnel')
}
