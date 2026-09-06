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
    foreach ($name in @("github-oidc-policy.json", "install-receipt.json")) {
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
foreach ($name in @("github-oidc-policy.json", "install-receipt.json")) {
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
    workflow_sha256 = "c5883681616c6685e175aa7f694681093c46ce3918b0546a7eb2658c983a79c3"
    runner_path = "scripts/github_attestation_runner.py"
    runner_sha256 = "9df1aab2f90601144ac5d586eace68425565d2ef5b56ad14966713cffdfddfee"
    attestor_policy_path = ".github/phase-4-attestation-policy.json"
    attestor_policy_fingerprint = "6d4f4a1127e93bb3d35896fe7cca0eb98955b655a2331e7b9293913d640b39de"
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
foreach ($path in @($policyPath, (Join-Path $target "install-receipt.json"))) {
    $fileAcl = [System.Security.AccessControl.FileSecurity]::new()
    $fileAcl.SetAccessRuleProtection($true, $false)
    $fileAcl.SetOwner($systemSid)
    $fileAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($systemSid, "FullControl", $allow))
    $fileAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($adminSid, "ReadAndExecute", $allow))
    $fileAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($usersSid, "ReadAndExecute", $allow))
    Set-Acl -LiteralPath $path -AclObject $fileAcl
}
