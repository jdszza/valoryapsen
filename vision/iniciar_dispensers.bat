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
rem  NAO use vision\iniciar_visao.bat: ele sobe a estacao na porta 8000, que e a
rem  do central.
rem ===========================================================================

rem ---- As linhas que voce edita ---------------------------------------------
rem  Camera de cada lado: o indice (0, 1, 2...) ou parte do nome da webcam
rem  (o estacao.py aceita os dois em --camera). `python src\camera.py`, dentro
rem  da pasta do lado, lista as cameras.
set CAMERA_ESQ=0
set CAMERA_DIR=1
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

cd /d "%PASTA%"

rem  O catalogo vem do vision-adapter, e o adapter conta o tempo ate a estacao
rem  usar o catalogo novo com o MESMO intervalo. backend.url errado faz a estacao
rem  julgar pelo catalogo local; intervalo diferente faz a leitura sair antes do
rem  catalogo da OS valer. Este arquivo avisa e para — nao edita o json.
"%PYTHON%" -c "import json,sys; b=json.load(open('config/parametros.json',encoding='utf-8')).get('backend',{}); sys.exit(0 if b.get('ativo') is True and str(b.get('url','')).rstrip('/')=='http://127.0.0.1:8102' and float(b.get('intervalo_catalogo',0))==2 else 1)"
if errorlevel 1 (
    powershell -NoProfile -Command "Write-Host 'ATENCAO: %PASTA%\config\parametros.json precisa de backend.ativo=true, backend.url=http://127.0.0.1:8102 e backend.intervalo_catalogo=2. Corrija o arquivo e rode de novo.' -ForegroundColor Red"
    pause
    exit /b 1
)

echo.
echo   estacao dos dispensers ^(%LADO%^) no host
echo   pasta   : %PASTA%
echo   camera  : %CAMERA%
echo   painel  : http://localhost:%PORTA%
echo   catalogo: http://127.0.0.1:8102/api/visao/catalogo
echo.

"%PYTHON%" -u src\estacao.py --camera %CAMERA% --estacao dispensers-%LADO% --porta %PORTA%
if errorlevel 1 (
    echo.
    echo A estacao terminou com erro ^(codigo %errorlevel%^).
    pause
)
