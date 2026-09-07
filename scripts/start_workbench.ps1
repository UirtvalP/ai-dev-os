$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$localExecutable = Join-Path $projectRoot '.venv\Scripts\ai-dev-os.exe'
if (Test-Path -LiteralPath $localExecutable -PathType Leaf) {
    $executable = [pscustomobject]@{ Source = $localExecutable }
} else {
    $executable = Get-Command ai-dev-os -ErrorAction SilentlyContinue
}
if (-not $executable -or -not (Test-Path -LiteralPath $executable.Source -PathType Leaf)) {
    throw '未找到 ai-dev-os，请先在仓库运行 uv sync --extra dev'
}

& $executable.Source workbench serve --root $projectRoot
