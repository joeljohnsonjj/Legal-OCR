# Install all Python dependencies for Legal OCR backend (Windows).
# Usage:  cd backend; .\scripts\install_server.ps1
#
# Uses backend\.venv ONLY (never global Python) to avoid conflicts with embedchain, etc.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "==> Legal OCR backend install (root: $Root)"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python not found. Install Python 3.10+ first."
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$VenvPip = Join-Path $Root ".venv\Scripts\pip.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "==> Creating virtualenv .venv"
    python -m venv .venv
}

function Assert-VenvPython {
    $exe = & $VenvPython -c "import sys; print(sys.executable)"
    if ($exe -notlike "*\Legal-OCR\backend\.venv\*") {
        throw "Python is not backend\.venv. Got: $exe"
    }
    Write-Host "==> Using venv Python: $exe"
}

Assert-VenvPython

Write-Host "==> Upgrading pip, wheel, setuptools"
& $VenvPython -m pip install --upgrade pip wheel "setuptools>=65.0.0,<82"

Write-Host "==> Installing requirements.txt (isolated venv)"
& $VenvPython -m pip install -r requirements.txt

Write-Host "==> Installing memory_management (editable)"
& $VenvPython -m pip install -e ./memory_management

Write-Host "==> spaCy model for Mem0 NLP"
& $VenvPython -m spacy download en_core_web_sm 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "spaCy model download failed; run: .\.venv\Scripts\python.exe -m spacy download en_core_web_sm"
}

Write-Host "==> Creating data folders"
@("docs", "output", "logs", "ocr_cache", "query_results") | ForEach-Object {
    New-Item -ItemType Directory -Force -Path $_ | Out-Null
}

Write-Host "==> Verifying imports"
& $VenvPython scripts/verify_install.py

Write-Host ""
Write-Host "SUCCESS. Activate and run:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  python run_api.py"
Write-Host ""
Write-Host "Tip: Always use backend\.venv. If you see global site-packages paths during pip,"
Write-Host "deactivate other venvs and run this script again."
