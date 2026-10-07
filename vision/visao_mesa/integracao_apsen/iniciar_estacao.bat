@echo off
REM Sobe a estacao de visao da mesa (APSEN).
REM
REM Coloque este .bat na raiz de visao_mesa\ ou rode de onde estiver: ele se
REM localiza sozinho pelo proprio caminho (%~dp0), porque atalho do Windows
REM costuma iniciar em C:\Windows\System32 e ai nenhum caminho relativo vale.

setlocal
cd /d "%~dp0\.."

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else (
    echo [aviso] .venv nao encontrado; usando o Python do sistema.
    echo         Para criar:  python -m venv .venv
)

if not exist "integracao_apsen\.env" (
    echo [aviso] integracao_apsen\.env nao existe — valendo os padroes.
    echo         Copie integracao_apsen\.env.example para .env e ajuste o ADAPTER_URL.
)

echo.
echo === Estacao de visao da mesa — APSEN ===
echo Encerre com Ctrl+C.
echo.
python -m integracao_apsen.servidor %*

REM Pausa so quando houver erro: assim o atalho nao fecha a janela e some com
REM a mensagem antes de alguem conseguir ler.
if errorlevel 1 pause
endlocal
