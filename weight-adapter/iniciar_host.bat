@echo off
setlocal
cd /d "%~dp0"

rem ===========================================================================
rem  weight-adapter FORA do Docker — o processo que abre a COM da balanca.
rem
rem  Ele roda no host porque o Docker Desktop nao repassa porta COM para
rem  container. As variaveis abaixo sao DESTE processo: o `.env` da raiz e do
rem  compose, e este arquivo nao o le.
rem
rem  Passo a passo do ensaio: weight\PRIMEIRO_ENSAIO.md
rem ===========================================================================

rem ---- A UNICA linha que voce precisa editar --------------------------------
rem  A COM fixada da placa da balanca (Gerenciador de Dispositivos ->
rem  Portas COM -> Propriedades -> Avancado -> Numero da Porta COM).
rem  Confira contra o ping: a placa certa emite {"cmd":"ping","sub":"weight"}.
if "%WEIGHT_SERIAL_URL%"=="" set WEIGHT_SERIAL_URL=COM8

rem ---- O resto raramente muda ------------------------------------------------
rem  `serial` e o firmware; `http` voltaria para o weight-simulator.
set WEIGHT_TRANSPORTE=serial
set WEIGHT_SERIAL_BAUD=115200
rem  Prazo do ACK, nao da conclusao. A conclusao continua nos TIMEOUT_* do
rem  orquestrador (ver docs\PROTOCOLO_SERIAL.md).
set WEIGHT_ACK_TIMEOUT_S=2
rem  A porta PUBLICADA do central. `central-computer:8000` e nome DNS da rede
rem  Docker e nao resolve aqui no host — e a mesma armadilha do BACKEND_URL.
if "%CENTRAL_URL%"=="" set CENTRAL_URL=http://localhost:8000

rem  Porta HTTP deste adapter. E a mesma que o compose publicava, entao nada
rem  muda para o central, o dashboard ou o pre-voo. Se ela estiver ocupada, o
rem  weight-adapter de CONTAINER esta de pe: `docker compose stop weight-adapter`
rem  (ou tire COMPOSE_PROFILES=simulado do .env).
if "%PORTA%"=="" set PORTA=8103

echo.
echo   weight-adapter ^(host^)
echo   porta serial : %WEIGHT_SERIAL_URL% @ %WEIGHT_SERIAL_BAUD%
echo   central      : %CENTRAL_URL%
echo   HTTP         : http://localhost:%PORTA%
echo.
echo   Se aparecer "conectado: false" em /health, o suspeito n1 e o Monitor
echo   Serial da Arduino IDE ainda aberto: no Windows a COM e exclusiva de um
echo   processo. Feche-o e este adapter reconecta sozinho.
echo.

if not exist ".venv\Scripts\python.exe" (
    echo Ambiente virtual nao encontrado. Rode uma vez:
    echo.
    echo     python -m venv .venv
    echo     .venv\Scripts\activate
    echo     pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port %PORTA%
