@echo off
setlocal
cd /d "%~dp0"

rem ===========================================================================
rem  cnc-adapter FORA do Docker — o processo que abre a COM da mesa CoreXY.
rem
rem  Ele roda no host porque o Docker Desktop nao repassa porta COM para
rem  container. As variaveis abaixo sao DESTE processo: o `.env` da raiz e do
rem  compose, e este arquivo nao o le.
rem
rem  AVISO — O FIRMWARE DA MESA AINDA NAO FALA JSON.
rem  `cnc/receitas_manuais` responde ao terminal humano (H, MP, EXEC) e ignora
rem  `{"cmd":...}`. Com CNC_TRANSPORTE=serial e a COM fixada, este adapter ABRE
rem  a porta e se da por CONECTADO — a URL fixa dispensa a sondagem por ping —,
rem  mas nenhum comando e executado e o ACK nunca vem: /comandos/mover falha no
rem  CNC_ACK_TIMEOUT_S e a OS aborta com a placa parecendo saudavel no /health.
rem  Ate a voz de maquina entrar (TASKS_CNC.md, tasks 3 a 5), use a mesa pelo
rem  Monitor Serial e deixe a planta no simulador: set CNC_TRANSPORTE=http.
rem
rem  Manual da placa: cnc\README.md
rem ===========================================================================

rem ---- A UNICA linha que voce precisa editar --------------------------------
rem  A COM fixada da placa da mesa (Gerenciador de Dispositivos -> Portas COM
rem  -> Propriedades -> Avancado -> Numero da Porta COM). Confira contra o
rem  ping: a placa certa emite {"cmd":"ping","sub":"cnc"}. Casar por VID/PID
rem  acharia a placa errada — o VID/PID do conversor USB-serial e o mesmo nas
rem  cinco placas da celula, e mandar `mover` para a balanca nao da erro: a OS
rem  morre por timeout de um slot que esta integro.
if "%CNC_SERIAL_URL%"=="" set CNC_SERIAL_URL=COM5

rem ---- O resto raramente muda ------------------------------------------------
rem  `serial` e o firmware; `http` volta para o cnc_simulator — que e onde a
rem  mesa esta hoje, enquanto o firmware nao fala JSON (ver o AVISO acima).
set CNC_TRANSPORTE=serial
set CNC_SERIAL_BAUD=115200
rem  Prazo do ACK, nao da conclusao. A conclusao continua nos TIMEOUT_* do
rem  orquestrador (ver docs\PROTOCOLO_SERIAL.md): um `mover` leva segundos e o
rem  ACK sai em milissegundos.
set CNC_ACK_TIMEOUT_S=2
rem  A porta PUBLICADA do central. `central-computer:8000` e nome DNS da rede
rem  Docker e nao resolve aqui no host — e a mesma armadilha do BACKEND_URL.
if "%CENTRAL_URL%"=="" set CENTRAL_URL=http://localhost:8000

rem  Porta HTTP deste adapter. E a mesma que o compose publicava, entao nada
rem  muda para o central, o dashboard ou o pre-voo. Se ela estiver ocupada, o
rem  cnc-adapter de CONTAINER esta de pe: `docker compose stop cnc-adapter`
rem  (ou tire COMPOSE_PROFILES=simulado do .env).
if "%PORTA%"=="" set PORTA=8101

echo.
echo   cnc-adapter ^(host^)
echo   porta serial : %CNC_SERIAL_URL% @ %CNC_SERIAL_BAUD%
echo   transporte   : %CNC_TRANSPORTE%
echo   central      : %CENTRAL_URL%
echo   HTTP         : http://localhost:%PORTA%
echo.
if /i "%CNC_TRANSPORTE%"=="serial" (
    echo   ATENCAO: o firmware da mesa ainda NAO fala JSON. Esperado neste modo:
    echo            /health diz "conectado", a placa imprime "Comando
    echo            desconhecido" a cada comando, e toda OS aborta no ACK.
    echo            Para rodar a planta agora: set CNC_TRANSPORTE=http
    echo.
)
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
