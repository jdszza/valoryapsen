@echo off
setlocal
cd /d "%~dp0"

rem ===========================================================================
rem  porta-adapter FORA do Docker — o processo que abre a COM da porta NR-12.
rem
rem  Ele roda no host porque o Docker Desktop nao repassa porta COM para
rem  container. E ele NAO tem servico no compose: nao existe porta simulada.
rem  As variaveis abaixo sao DESTE processo: o `.env` da raiz e do compose, e
rem  este arquivo nao o le.
rem
rem  A porta tem USB PROPRIA e este processo e o dono dela. Ela nao passa mais
rem  pela placa das telas TFT (docs\PROTOCOLO_SERIAL.md, secao 9).
rem ===========================================================================

rem ---- A UNICA linha que voce precisa editar --------------------------------
rem  A COM fixada da placa da porta (Gerenciador de Dispositivos ->
rem  Portas COM -> Propriedades -> Avancado -> Numero da Porta COM).
rem  Confira contra o ping: a placa certa emite {"cmd":"ping","sub":"porta"}.
rem  SEM default de proposito: uma COM chutada aqui poderia ser a de outra
rem  placa, e a varredura automatica e proibida na celula montada.
rem  Exemplo:  set PORTA_SERIAL_URL=COM7
if "%PORTA_SERIAL_URL%"=="" set PORTA_SERIAL_URL=

if "%PORTA_SERIAL_URL%"=="" (
    echo.
    echo   PORTA_SERIAL_URL nao esta definida.
    echo.
    echo   Anote a COM da placa da porta no Gerenciador de Dispositivos e
    echo   edite a linha "set PORTA_SERIAL_URL=" deste arquivo ^(ex.: COM7^),
    echo   ou defina a variavel antes de chamar este .bat. Ela nao pode ser a
    echo   mesma COM dos mecanismos, das telas, da CNC nem da balanca.
    echo.
    pause
    exit /b 1
)

rem ---- O resto raramente muda ------------------------------------------------
set PORTA_SERIAL_BAUD=115200
rem  Prazo do ACK, nao da conclusao (ver docs\PROTOCOLO_SERIAL.md).
set PORTA_ACK_TIMEOUT_S=2
rem  Heartbeat da porta mais velho que isto = link perdido = celula parada.
if "%PORTA_HEARTBEAT_MAX_S%"=="" set PORTA_HEARTBEAT_MAX_S=2.5
rem  A porta PUBLICADA do central. `central-computer:8000` e nome DNS da rede
rem  Docker e nao resolve aqui no host — e a mesma armadilha do BACKEND_URL.
if "%CENTRAL_URL%"=="" set CENTRAL_URL=http://localhost:8000

rem  Porta HTTP deste adapter. O central a alcanca por
rem  PORTA_ADAPTER_URL=http://host.docker.internal:8104 (no .env da raiz).
rem  `PORTA_HTTP`, e nao `PORTA` como nos outros .bat: aqui "porta" e a porta
rem  NR-12, e as variaveis dela comecam com PORTA_.
if "%PORTA_HTTP%"=="" set PORTA_HTTP=8104

echo.
echo   porta-adapter ^(host^)
echo   porta serial : %PORTA_SERIAL_URL% @ %PORTA_SERIAL_BAUD%
echo   central      : %CENTRAL_URL%
echo   HTTP         : http://localhost:%PORTA_HTTP%
echo.
echo   Se aparecer "conectado: false" em /health, o suspeito n1 e o Monitor
echo   Serial da Arduino IDE ainda aberto: no Windows a COM e exclusiva de um
echo   processo. Para cadastrar tag ^(R/S/L^) pelo Monitor Serial, PARE este
echo   adapter antes - e, com ele parado, o central para a celula: link perdido
echo   e porta aberta.
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

".venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port %PORTA_HTTP%
