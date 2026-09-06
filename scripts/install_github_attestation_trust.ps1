param(
    [string]$TargetRoot = "C:\ProgramData\ai-dev-os\verification-authority"
)

$ErrorActionPreference = "Stop"
$target = [System.IO.Path]::GetFullPath($TargetRoot)
if ($target -ne "C:\ProgramData\ai-dev-os\verification-authority") {
    throw "只允许安装到固定系统信任目录"
}

if (Test-Path -LiteralPath $target) {
    & "$env:SystemRoot\System32\takeown.exe" /F $target /A | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "无法取得系统信任目录所有权" }
    $managedNames = @("github-oidc-policy.json", "install-receipt.json") + @(
        Get-ChildItem -LiteralPath $target -Filter "github-oidc-policy-*.json" -File -ErrorAction SilentlyContinue |
            ForEach-Object { $_.Name }
    )
    foreach ($name in $managedNames) {
        $existing = Join-Path $target $name
        if (Test-Path -LiteralPath $existing) {
            & "$env:SystemRoot\System32\takeown.exe" /F $existing /A | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "无法取得系统信任文件所有权：$name" }
        }
    }
} else {
    New-Item -ItemType Directory -Path $target -Force | Out-Null
}
$adminSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$maintenanceAcl = [System.Security.AccessControl.DirectorySecurity]::new()
$maintenanceAcl.SetAccessRuleProtection($true, $false)
$maintenanceAcl.SetOwner($adminSid)
$maintenanceAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($adminSid, "FullControl", "ContainerInherit,ObjectInherit", "None", $allow))
Set-Acl -LiteralPath $target -AclObject $maintenanceAcl
foreach ($name in $managedNames) {
    $existing = Join-Path $target $name
    if (Test-Path -LiteralPath $existing) {
        $fileMaintenanceAcl = [System.Security.AccessControl.FileSecurity]::new()
        $fileMaintenanceAcl.SetAccessRuleProtection($true, $false)
        $fileMaintenanceAcl.SetOwner($adminSid)
        $fileMaintenanceAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($adminSid, "FullControl", $allow))
        Set-Acl -LiteralPath $existing -AclObject $fileMaintenanceAcl
    }
}
$policy = @{
    schema_version = 1
    repository = "UirtvalP/ai-dev-os"
    workflow_path = ".github/workflows/phase-4-attestation.yml"
    workflow_ref = "refs/heads/main"
    workflow_sha256 = "19df49cbbdc399bc53ec5a8507318a6e7e9547a5872b3d44af5854049a23ad2d"
    runner_path = "scripts/github_attestation_runner.py"
    runner_sha256 = "4b00dfda01d44ada1fb0d34774b851813d21d59edcda15e71aa4551a16aed1ae"
    attestor_policy_path = ".github/phase-4-attestation-policy.json"
    attestor_policy_fingerprint = "58b1b8b5f69cb1d124dce6cc898f507bbb6b40cd813688e02f666bb66d8c1436"
    action_sha = "977bb373ede98d70efdf65b84cb5f73e068dcc2a"
    gh_path = "C:\Program Files\GitHub CLI\gh.exe"
    gh_sha256 = "2ae2b350c227a618f2d8965b1900aeee13446ff42e17ef0bd5a0b6405c593cfb"
}
$json = $policy | ConvertTo-Json -Depth 4
$policyPath = Join-Path $target "github-oidc-policy.json"
if (Test-Path -LiteralPath $policyPath) {
    $previous = Get-Content -LiteralPath $policyPath -Raw | ConvertFrom-Json
    $previousFingerprint = [string]$previous.attestor_policy_fingerprint
    if ($previousFingerprint -notmatch '^[a-f0-9]{64}$') {
        throw "现有 GitHub OIDC policy fingerprint 无效，拒绝覆盖"
    }
    if ($previousFingerprint -ne $policy.attestor_policy_fingerprint) {
        $historyPath = Join-Path $target "github-oidc-policy-$previousFingerprint.json"
        if (-not (Test-Path -LiteralPath $historyPath)) {
            Copy-Item -LiteralPath $policyPath -Destination $historyPath
        }
    }
}
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

$systemSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-18")
$usersSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-32-545")
$inherit = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
$none = [System.Security.AccessControl.PropagationFlags]::None
$directoryAcl = [System.Security.AccessControl.DirectorySecurity]::new()
$directoryAcl.SetAccessRuleProtection($true, $false)
$directoryAcl.SetOwner($systemSid)
$directoryAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($systemSid, "FullControl", $inherit, $none, $allow))
$directoryAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($adminSid, "ReadAndExecute", $inherit, $none, $allow))
$directoryAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($usersSid, "ReadAndExecute", $inherit, $none, $allow))
Set-Acl -LiteralPath $target -AclObject $directoryAcl
$protectedPaths = @($policyPath, (Join-Path $target "install-receipt.json")) + @(
    Get-ChildItem -LiteralPath $target -Filter "github-oidc-policy-*.json" -File |
        ForEach-Object { $_.FullName }
)
foreach ($path in $protectedPaths) {
    $fileAcl = [System.Security.AccessControl.FileSecurity]::new()
    $fileAcl.SetAccessRuleProtection($true, $false)
    $fileAcl.SetOwner($systemSid)
    $fileAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($systemSid, "FullControl", $allow))
    $fileAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($adminSid, "ReadAndExecute", $allow))
    $fileAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($usersSid, "ReadAndExecute", $allow))
    Set-Acl -LiteralPath $path -AclObject $fileAcl
}
