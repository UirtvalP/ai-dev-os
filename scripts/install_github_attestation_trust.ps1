param(
    [string]$TargetRoot = "C:\ProgramData\ai-dev-os\verification-authority"
)

$ErrorActionPreference = "Stop"
$target = [System.IO.Path]::GetFullPath($TargetRoot)
if ($target -ne "C:\ProgramData\ai-dev-os\verification-authority") {
    throw "只允许安装到固定系统信任目录"
}

New-Item -ItemType Directory -Path $target -Force | Out-Null
$workerIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $target /remove:d $workerIdentity /T /C | Out-Null
& icacls.exe $target /grant:r "BUILTIN\Administrators:(OI)(CI)F" /T /C | Out-Null
$policy = @{
    schema_version = 1
    repository = "UirtvalP/ai-dev-os"
    workflow_path = ".github/workflows/phase-4-attestation.yml"
    workflow_ref = "refs/heads/main"
    workflow_sha256 = "97e9c3544076fdfe9ba092792ae8e7c4bb302903064026e654e43bf4e4639b4f"
    runner_path = "scripts/github_attestation_runner.py"
    runner_sha256 = "cc6d755be7742fe7c8979ac8ec148a5187ca219a673084628badc1a0e64bf0f7"
    attestor_policy_path = ".github/phase-4-attestation-policy.json"
    attestor_policy_fingerprint = "e28d6586071f7d613d6b0ecc99c03cefeb30fb369f6968ac9b9ea0ac369ad137"
    action_sha = "977bb373ede98d70efdf65b84cb5f73e068dcc2a"
    gh_path = "C:\Program Files\GitHub CLI\gh.exe"
    gh_sha256 = "2ae2b350c227a618f2d8965b1900aeee13446ff42e17ef0bd5a0b6405c593cfb"
}
$json = $policy | ConvertTo-Json -Depth 4
$policyPath = Join-Path $target "github-oidc-policy.json"
[System.IO.File]::WriteAllText($policyPath, $json + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false))

$receipt = @{
    installed_at = [DateTimeOffset]::UtcNow.ToString("o")
    policy_sha256 = (Get-FileHash -LiteralPath $policyPath -Algorithm SHA256).Hash.ToLowerInvariant()
    target = $policyPath
}
[System.IO.File]::WriteAllText(
    (Join-Path $target "install-receipt.json"),
    ($receipt | ConvertTo-Json -Depth 3) + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false)
)

& icacls.exe $target /inheritance:r | Out-Null
& icacls.exe $target /setowner "BUILTIN\Administrators" /T /C | Out-Null
& icacls.exe $target /grant:r "NT AUTHORITY\SYSTEM:(OI)(CI)F" "BUILTIN\Administrators:(OI)(CI)RX" "BUILTIN\Users:(OI)(CI)RX" /T /C | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "无法收紧系统信任目录 ACL"
}
