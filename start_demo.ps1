# Local demo of LabExtract AI with the labx multi-model layer (four patient fields).
# Run:  powershell -ExecutionPolicy Bypass -File start_demo.ps1      then open http://127.0.0.1:8000
# Sign in: demo@lab.local / Demo2026!
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = "C:\accthon_chani\cloud_spike\.venv\Scripts\python.exe"

$hash = & $py -c "from backend.auth import hash_password; print(hash_password('Demo2026!'))"
$env:LABEXTRACT_DEV_USERS = '{"demo@lab.local": {"user_id": "demo-user", "password_hash": "' + $hash.Trim() + '"}}'
$env:LABEXTRACT_STORAGE = "local"
$env:LABEXTRACT_AUTH = "local"
$env:LABEXTRACT_COOKIE_SECURE = "false"
$env:LABEXTRACT_ALLOWED_ORIGINS = "http://127.0.0.1:8000,http://localhost:8000"
$env:LABEXTRACT_PUBLIC_DIR = "$PSScriptRoot\frontend\dist"
$env:LABEXTRACT_ENGINE = "hybrid"          # text layer when present, local OCR for scans (sender, numbers)
$env:LABEXTRACT_LABX = "on"                # multi-model consensus for first/last name, ID, received date
$env:LABX_DIR = "C:\accthon_chani\AI"
# $env:LABX_AUTO_APPROVE = "1"             # uncomment: consensus values are approved automatically

& $py -m uvicorn backend.container:app --host 127.0.0.1 --port 8000
