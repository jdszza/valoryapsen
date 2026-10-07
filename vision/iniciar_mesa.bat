@echo off
setlocal EnableDelayedExpansion

rem ===========================================================================
rem  Estacao de visao da MESA (camera da balanca), no host (dona da webcam).
rem
rem     vision\iniciar_mesa.bat
rem
rem  Substitui o vision\visao_mesa\integracao_apsen\iniciar_estacao.bat, que
rem  continua la intocado (o codigo da estacao nao e editado por este
rem  repositorio). A diferenca:
rem
rem   1. ANTES de subir, roda vision\conferir_mesa.py com o Python do venv da
rem      mesa: modo do fundo, .env da estacao, pacotes do venv e colisao de
rem      camera com as estacoes dos dispensers. Falhou, para em vermelho.
rem   2. A estacao se REERGUE sozinha: se o processo sair, o laco registra em
rem      vision\visao_mesa\dados\reinicios.log e sobe de novo em 5 s. Reiniciar
rem      com OS rodando custa uma conferencia (o vision-adapter marca a foto
rem      seguinte como ressincronizar), nao uma trava.
rem      Para encerrar de vez: Ctrl+C e responda S.
rem ===========================================================================

set RAIZ=%~dp0visao_mesa
set PYTHON=%RAIZ%\.venv\Scripts\python.exe

if not exist "%PYTHON%" (
    echo Ambiente virtual da estacao da mesa nao encontrado. Rode uma vez:
    echo    cd vision\visao_mesa ^&^& python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt -r integracao_apsen\requirements.txt
    pause
    exit /b 1
)

"%PYTHON%" "%~dp0conferir_mesa.py"
if errorlevel 1 (
    powershell -NoProfile -Command "Write-Host 'A estacao da mesa NAO subiu: corrija o que esta listado acima e rode de novo.' -ForegroundColor Red"
    pause
    exit /b 1
)

cd /d "%RAIZ%"
if not exist "dados" mkdir dados

echo.
echo   estacao da mesa no host
echo   pasta   : %RAIZ%
echo   status  : http://localhost:8212/status
echo.

:laco
"%PYTHON%" -m integracao_apsen.servidor
set CODIGO=!errorlevel!
echo %date% %time% estacao da mesa saiu com codigo !CODIGO! >> "%RAIZ%\dados\reinicios.log"
powershell -NoProfile -Command "Write-Host 'estacao da mesa caiu (codigo !CODIGO!) - reiniciando em 5 s. Ctrl+C para encerrar.' -ForegroundColor Red"
timeout /t 5 /nobreak >nul
goto laco
