# Gera dist\apsen.exe a partir do backend Flask.
# Uso (a partir de backend_apsen\):  powershell -ExecutionPolicy Bypass -File build_exe.ps1
# Atualizar o app na maquina de producao = rodar isto de novo e copiar o .exe novo.

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Error "venv nao encontrada em .venv\ — crie com: python -m venv .venv"
    exit 1
}

& $py -m pip install -r (Join-Path $PSScriptRoot "requirements.txt") -r (Join-Path $PSScriptRoot "requirements-desktop.txt")
if ($LASTEXITCODE -ne 0) { exit 1 }

& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name apsen `
    --add-data "templates;templates" `
    --hidden-import webview.platforms.edgechromium `
    --hidden-import webview.platforms.winforms `
    (Join-Path $PSScriptRoot "desktop.py")
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host ""
Write-Host "OK: executavel gerado em dist\apsen.exe"
Write-Host "O banco apsen.db e criado/lido na MESMA pasta onde o .exe estiver."
