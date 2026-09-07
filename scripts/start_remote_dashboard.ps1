$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $env:USERPROFILE '.ai-dev-os\runtime\workbench'
$tokenFile = Join-Path $env:USERPROFILE '.ai-dev-os\secrets\req-020-dashboard.token'
$username = 'admin'
$identityFile = Join-Path $env:USERPROFILE '.ssh\homebox-relay\id_ed25519'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$publicUrl = 'https://game.homebox2026.online/ai-dev-os/'

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
foreach ($path in @($python, $identityFile)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "缺少远程 Dashboard 依赖：$path"
    }
}

$localPort = 8767
$listener = Get-NetTCPConnection -LocalPort $localPort -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    try {
        $existing = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$localPort/" -TimeoutSec 3
    } catch {
        throw "$localPort 端口已被 PID $($listener.OwningProcess) 占用，且不是可识别的 Workbench"
    }
    $existingServer = [string]$existing.Headers['Server']
    $existingAuth = [string]$existing.Headers['X-AI-Dev-OS-Auth']
    if ($existingServer -notlike 'AI-Dev-OS-Workbench/*' -or
        $existingAuth -ne 'basic') {
        throw "$localPort 端口已被 PID $($listener.OwningProcess) 占用，但不是启用远程认证的 Workbench"
    }
}
if (-not $listener) {
    $codex = Get-ChildItem -Path "$env:LOCALAPPDATA\OpenAI\Codex\bin\*\codex.exe" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $codex) {
        throw '未找到 Codex 桌面应用内置 CLI'
    }
    $env:AI_DEV_OS_CODEX = $codex.FullName
    $arguments = @(
        '-m', 'workspace_orchestrator.product_cli',
        'workbench', 'serve', '--no-open', '--remote-access',
        '--root', ('"{0}"' -f $projectRoot), '--port', $localPort,
        '--token-file', ('"{0}"' -f $tokenFile), '--username', $username
    ) -join ' '
    Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $runtimeRoot 'server-cloud.out.log') `
        -RedirectStandardError (Join-Path $runtimeRoot 'server-cloud.err.log') | Out-Null

    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 500
        $listener = Get-NetTCPConnection -LocalPort $localPort -State Listen -ErrorAction SilentlyContinue
    } until ($listener -or (Get-Date) -ge $deadline)
    if (-not $listener) {
        throw "Dashboard 未能在 127.0.0.1:$localPort 启动"
    }
    $started = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$localPort/" -TimeoutSec 3
    $startedServer = [string]$started.Headers['Server']
    $startedAuth = [string]$started.Headers['X-AI-Dev-OS-Auth']
    if ($startedServer -notlike 'AI-Dev-OS-Workbench/*' -or
        $startedAuth -ne 'basic') {
        throw '新启动的 Workbench 未启用远程认证，拒绝建立公网隧道'
    }
}

$reverseForward = "127.0.0.1:18765:127.0.0.1:$localPort"
$sshProcess = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'ssh.exe' -and $_.CommandLine -match [regex]::Escape($reverseForward)
}
if (-not $sshProcess) {
    $ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
    $arguments = @(
        '-NT', '-i', ('"{0}"' -f $identityFile),
        '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
        '-o', 'StrictHostKeyChecking=yes', '-R', $reverseForward,
        'root@112.74.15.57'
    ) -join ' '
    Start-Process -FilePath $ssh -ArgumentList $arguments -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $runtimeRoot 'ssh.out.log') `
        -RedirectStandardError (Join-Path $runtimeRoot 'ssh.err.log') | Out-Null
}

[pscustomobject]@{
    Status = 'running'
    PublicUrl = $publicUrl
    LocalEndpoint = "http://127.0.0.1:$localPort"
    CloudEndpoint = 'http://127.0.0.1:18765'
    TokenFile = $tokenFile
    Username = $username
    Mode = 'Requirement Space Workbench'
}
