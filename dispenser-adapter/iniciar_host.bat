@echo off
setlocal
cd /d "%~dp0"

rem ===========================================================================
rem  dispenser-adapter FORA do Docker — o processo que abre as DUAS COM do
rem  dispenser: a dos mecanismos e a das 8 telas TFT.
rem
rem  Ele roda no host porque o Docker Desktop nao repassa porta COM para
rem  container. As variaveis abaixo sao DESTE processo: o `.env` da raiz e do
rem  compose, e este arquivo nao o le.
rem
rem  DUAS placas, DUAS portas, UM processo. Acionar 8 mecanismos, desenhar 8
rem  telas e manter a serial nao cabe num ESP so; quem ja ve todo comando e
rem  todo evento do slot e este adapter, e por isso as duas portas sao dele.
rem
rem  Passo a passo do ensaio: dispenser\PRIMEIRO_ENSAIO.md
rem ===========================================================================

rem ---- As DUAS linhas que voce precisa editar -------------------------------
rem  As COM fixadas (Gerenciador de Dispositivos -> Portas COM -> Propriedades
rem  -> Avancado -> Numero da Porta COM). Confira cada uma contra o ping: a
rem  placa dos mecanismos emite {"cmd":"ping","sub":"dispenser"} e a das telas
rem  emite {"cmd":"ping","sub":"dispenser_tft"}. Casar por VID/PID acharia a
rem  placa errada — o VID/PID do conversor USB-serial e o mesmo nas duas.
if "%DISPENSER_SERIAL_URL%"=="" set DISPENSER_SERIAL_URL=COM6
if "%DISPENSER_TFT_SERIAL_URL%"=="" set DISPENSER_TFT_SERIAL_URL=COM10

rem ---- O resto raramente muda ------------------------------------------------
rem  `serial` e o firmware; `http` voltaria para o dispenser_simulator. Para as
rem  telas, `http` significa "sem telas" — e nao um erro.
set DISPENSER_TRANSPORTE=serial
set DISPENSER_SERIAL_BAUD=115200
set DISPENSER_TFT_TRANSPORTE=serial
set DISPENSER_TFT_SERIAL_BAUD=115200
rem  Prazo do ACK, nao da conclusao. A conclusao continua nos TIMEOUT_* do
rem  orquestrador (ver docs\PROTOCOLO_SERIAL.md): um `dispensar` de 10 unidades
rem  leva dezenas de segundos, e o ACK sai em milissegundos.
set DISPENSER_ACK_TIMEOUT_S=2
set DISPENSER_TFT_ACK_TIMEOUT_S=2
rem  A porta PUBLICADA do central. `central-computer:8000` e nome DNS da rede
rem  Docker e nao resolve aqui no host — e a mesma armadilha do BACKEND_URL.
if "%CENTRAL_URL%"=="" set CENTRAL_URL=http://localhost:8000

rem  Porta HTTP deste adapter. E a mesma que o compose publicava, entao nada
rem  muda para o central, o dashboard ou o pre-voo. Se ela estiver ocupada, o
rem  dispenser-adapter de CONTAINER esta de pe:
rem  `docker compose stop dispenser-adapter` (ou tire COMPOSE_PROFILES=simulado
rem  do .env).
if "%PORTA%"=="" set PORTA=8100

echo.
echo   dispenser-adapter ^(host^)
echo   mecanismos : %DISPENSER_SERIAL_URL% @ %DISPENSER_SERIAL_BAUD%
echo   telas TFT  : %DISPENSER_TFT_SERIAL_URL% @ %DISPENSER_TFT_SERIAL_BAUD%
echo   central    : %CENTRAL_URL%
echo   HTTP       : http://localhost:%PORTA%
echo.
echo   Se aparecer "conectado: false" em /health, o suspeito n1 e o Monitor
echo   Serial da Arduino IDE ainda aberto: no Windows a COM e exclusiva de um
echo   processo. Feche-o e este adapter reconecta sozinho.
echo.
echo   A placa das telas fora do ar NAO impede a dispensa: o adapter loga e
echo   segue. Tela errada e cosmetica; dispensa atrasada nao e.
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
