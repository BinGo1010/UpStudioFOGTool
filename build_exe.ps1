$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

Write-Host "Project: $ProjectRoot"
Write-Host "Cleaning previous build outputs..."
if (Test-Path -LiteralPath "build") {
    Remove-Item -LiteralPath "build" -Recurse -Force
}
if (Test-Path -LiteralPath "dist\UpStudioFOGTool") {
    Remove-Item -LiteralPath "dist\UpStudioFOGTool" -Recurse -Force
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
