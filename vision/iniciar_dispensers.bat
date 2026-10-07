@echo off
setlocal EnableDelayedExpansion

rem ===========================================================================
rem  Estacao de visao de UMA fileira de dispensers, no host (dona da webcam).
rem
rem     vision\iniciar_dispensers.bat esq     camera da esquerda, D1-D4, :8301
rem     vision\iniciar_dispensers.bat dir     camera da direita,  D5-D8, :8302
rem
rem  O codigo e um so (vision\visao\). Cada camera roda numa PASTA propria,
rem  vision\visao_<lado>\, porque tudo na estacao e relativo a pasta: config\,
rem  dados\eventos.db, config\pilhas.json. Duas estacoes na mesma pasta
rem  dividiriam a calibracao e o banco de eventos.
rem
rem  A estacao se REERGUE sozinha: se ela sair (falha de camera, ESC na janela,
rem  qualquer codigo), o laco registra em vision\visao_<lado>\logs\reinicios.log
rem  e sobe de novo em 5 s. Para encerrar de vez: Ctrl+C e responda S.
rem
rem  NAO use vision\iniciar_visao.bat: ele sobe a estacao na porta 8000, que e a
rem  do central.
rem ===========================================================================

rem ---- As linhas que voce edita ---------------------------------------------
rem  Camera de cada lado. VAZIO (o padrao) = a camera que `python src\camera.py`
rem  gravou no parametros.json da pasta do lado, procurada pelo NOME (o indice
rem  de webcam USB muda com a ordem em que o Windows as enumera). Preencha so
rem  como escape manual: um numero aqui vai direto para o indice e PULA o nome.
set CAMERA_ESQ=
set CAMERA_DIR=
rem  Portas HTTP das estacoes. O vision-adapter le o /api/estado delas por
rem  VISAO_DISP_ESQ_URL / VISAO_DISP_DIR_URL (http://host.docker.internal:<porta>).
set PORTA_ESQ=8301
set PORTA_DIR=8302

rem ---- Daqui para baixo nada muda --------------------------------------------
set LADO=%~1
if /i "%LADO%"=="esq" (
    set CAMERA=%CAMERA_ESQ%
    set PORTA=%PORTA_ESQ%
) else if /i "%LADO%"=="dir" (
    set CAMERA=%CAMERA_DIR%
    set PORTA=%PORTA_DIR%
) else (
    echo Uso: iniciar_dispensers.bat esq ^| dir
    pause
    exit /b 1
)

set ORIGEM=%~dp0visao
set PASTA=%~dp0visao_%LADO%
rem  O venv da estacao dos dispensers e o de vision\visao: opencv-python aqui,
rem  opencv-contrib na estacao da mesa. Os dois no mesmo venv quebram o cv2.
set PYTHON=%ORIGEM%\.venv\Scripts\python.exe

if not exist "%PYTHON%" (
    echo Ambiente virtual da visao nao encontrado. Rode uma vez:
    echo    cd vision\visao ^&^& python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
    pause
    exit /b 1
)

rem  Primeira execucao: a pasta do lado nasce como copia de vision\visao.
if not exist "%PASTA%\src\estacao.py" (
    echo Criando %PASTA% a partir de vision\visao ...
    robocopy "%ORIGEM%" "%PASTA%" /E /XD dados logs .venv __pycache__ /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 (
        echo Falha ao copiar vision\visao para %PASTA%.
        pause
        exit /b 1
    )
    echo.
    echo Pasta criada. Antes de operar, NESTA pasta:
    echo    python src\camera.py      escolhe a camera
    echo    python src\calibrar.py    zonas com o numero REAL do slot
    if /i "%LADO%"=="dir" echo                              ^(direita: 5 a 8, nao 1 a 4^)
    echo.
)

rem  Toda execucao: o codigo e espelhado de vision\visao\src. Nunca toca em
rem  config\ nem em dados\ — a calibracao e o banco de eventos sao da pasta.
robocopy "%ORIGEM%\src" "%PASTA%\src" /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo Falha ao atualizar o codigo em %PASTA%\src.
    pause
    exit /b 1
)

rem  A conferencia roda UMA vez, antes do laco: zonas calibradas e numeradas
rem  pelo lado, camera escolhida por alguem e diferente das outras duas
rem  estacoes, backend.url com o lado (/estacoes/<lado>) e intervalo do
rem  catalogo igual ao do vision-adapter. Ela avisa e para — nao edita nada.
"%PYTHON%" "%~dp0conferir_dispensers.py" %LADO%
if errorlevel 1 (
    powershell -NoProfile -Command "Write-Host 'A estacao %LADO% NAO subiu: corrija o que esta listado acima e rode de novo.' -ForegroundColor Red"
    pause
    exit /b 1
)

cd /d "%PASTA%"
if not exist "logs" mkdir logs

set ARG_CAMERA=
if not "%CAMERA%"=="" set ARG_CAMERA=--camera %CAMERA%

echo.
echo   estacao dos dispensers ^(%LADO%^) no host
echo   pasta   : %PASTA%
if "%CAMERA%"=="" (echo   camera  : a gravada no parametros.json ^(por nome^)) else (echo   camera  : %CAMERA% ^(escape manual^))
echo   painel  : http://localhost:%PORTA%
echo   catalogo: http://127.0.0.1:8102/estacoes/%LADO%/api/visao/catalogo
echo   teclas  : ESC encerra ^(o laco sobe de novo^) - p pausa ^(a conferencia de SKU para^)
echo.

:laco
"%PYTHON%" -u src\estacao.py %ARG_CAMERA% --estacao dispensers-%LADO% --porta %PORTA%
set CODIGO=!errorlevel!
echo %date% %time% estacao %LADO% saiu com codigo !CODIGO! >> "%PASTA%\logs\reinicios.log"
powershell -NoProfile -Command "Write-Host 'estacao %LADO% caiu (codigo !CODIGO!) - reiniciando em 5 s. Ctrl+C para encerrar.' -ForegroundColor Red"
timeout /t 5 /nobreak >nul
goto laco
