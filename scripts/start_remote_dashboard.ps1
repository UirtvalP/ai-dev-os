$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $env:USERPROFILE '.ai-dev-os\runtime\req-020-dashboard'
$tokenFile = Join-Path $env:USERPROFILE '.ai-dev-os\secrets\req-020-dashboard.token'
$identityFile = Join-Path $env:USERPROFILE '.ssh\homebox-relay\id_ed25519'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$publicUrl = 'https://game.homebox2026.online/ai-dev-os/'

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
foreach ($path in @($python, $identityFile)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "缺少远程 Dashboard 依赖：$path"
    }
}

$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
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
        'dashboard', 'serve', 'REQ-020', '--new-session',
        '--root', ('"{0}"' -f $projectRoot), '--port', '8765',
        '--token-file', ('"{0}"' -f $tokenFile),
        '--run-id', 'remote-req020-dedicated'
    ) -join ' '
    Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $runtimeRoot 'server-cloud.out.log') `
        -RedirectStandardError (Join-Path $runtimeRoot 'server-cloud.err.log') | Out-Null

    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 500
        $listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    } until ($listener -or (Get-Date) -ge $deadline)
    if (-not $listener) {
        throw 'Dashboard 未能在 127.0.0.1:8765 启动'
    }
}

$reverseForward = '127.0.0.1:18765:127.0.0.1:8765'
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
    LocalEndpoint = 'http://127.0.0.1:8765'
    CloudEndpoint = 'http://127.0.0.1:18765'
    TokenFile = $tokenFile
}
