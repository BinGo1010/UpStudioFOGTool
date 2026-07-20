$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

Write-Host "Project: $ProjectRoot"
$ExistingDist = Join-Path $ProjectRoot "dist\UpStudioFOGTool"
$ExistingData = Join-Path $ExistingDist "data"
if (Test-Path -LiteralPath $ExistingData) {
    $DataFile = Get-ChildItem -LiteralPath $ExistingData -Force -Recurse -File -ErrorAction Stop |
        Select-Object -First 1
    if ($null -ne $DataFile) {
        throw "Build cancelled: experimental data exists under $ExistingData. Move it to a safe location before rebuilding."
    }
}

Write-Host "Cleaning previous build outputs..."
if (Test-Path -LiteralPath "build") {
    Remove-Item -LiteralPath "build" -Recurse -Force
}
if (Test-Path -LiteralPath $ExistingDist) {
    Remove-Item -LiteralPath $ExistingDist -Recurse -Force
}

Write-Host "Building UpStudioFOGTool.exe..."
python -m PyInstaller --clean --noconfirm upstudio_fog_tool.spec

$ExePath = Join-Path $ProjectRoot "dist\UpStudioFOGTool\UpStudioFOGTool.exe"
if (-not (Test-Path -LiteralPath $ExePath)) {
    throw "Build finished but exe was not found: $ExePath"
}

Write-Host ""
Write-Host "Build complete:"
Write-Host $ExePath
Write-Host ""
Write-Host "Copy the whole folder dist\UpStudioFOGTool to another Windows PC, not only the exe."
