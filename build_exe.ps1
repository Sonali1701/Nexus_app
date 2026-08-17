$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found at $python"
}

Push-Location $PSScriptRoot
try {
    & $python -m PyInstaller --noconfirm --clean "NexusUploader.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE"
    }

    Copy-Item -LiteralPath ".env.example" -Destination "dist\.env.example" -Force
    Copy-Item -LiteralPath "PACKAGED_APP_README.txt" -Destination "dist\README.txt" -Force
    if (Test-Path -LiteralPath ".env") {
        Copy-Item -LiteralPath ".env" -Destination "dist\.env" -Force
    }

    Write-Host "Built: $PSScriptRoot\dist\NexusUploader.exe"
    Write-Host "Keep dist\.env beside the executable. It contains private credentials."
}
finally {
    Pop-Location
}
