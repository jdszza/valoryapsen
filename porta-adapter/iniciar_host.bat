@echo off
setlocal
cd /d "%~dp0"

rem ===========================================================================
rem  porta-adapter FORA do Docker — o processo que LE a COM do receptor da
rem  porta NR-12.
rem
rem  Ele roda no host porque o Docker Desktop nao repassa porta COM para
rem  container. E ele NAO tem servico no compose: nao existe porta simulada.
rem  As variaveis abaixo sao DESTE processo: o `.env` da raiz e do compose, e
rem  este arquivo nao o le.
rem
rem  Em campo a porta sao DUAS ESPs: a da porta (nr12_oficial), que decide a
rem  tag sozinha, e o RECEPTOR (receptor_esp32), que repassa as linhas dela na
rem  USB. Este processo abre a COM do RECEPTOR e so LE: nada desce para as
rem  ESPs (docs\PROTOCOLO_SERIAL.md, secao 10).
rem ===========================================================================

rem ---- A UNICA linha que voce precisa editar --------------------------------
rem  A COM fixada do ESP RECEPTOR (Gerenciador de Dispositivos ->
rem  Portas COM -> Propriedades -> Avancado -> Numero da Porta COM).
rem  Para conferir: com este adapter PARADO, o Monitor Serial nessa COM mostra
rem  o banner "=== RECEPTOR Serial2" ao resetar o receptor, e as linhas
rem  "EVT:..." da porta ao passar uma tag.
rem  SEM default de proposito: uma COM chutada aqui poderia ser a de outra
rem  placa, e a varredura automatica e proibida na celula montada.
rem  Exemplo:  set PORTA_SERIAL_URL=COM7
if "%PORTA_SERIAL_URL%"=="" set PORTA_SERIAL_URL=

if "%PORTA_SERIAL_URL%"=="" (
    echo.
    echo   PORTA_SERIAL_URL nao esta definida.
    echo.
    echo   Anote a COM do ESP RECEPTOR no Gerenciador de Dispositivos e
    echo   edite a linha "set PORTA_SERIAL_URL=" deste arquivo ^(ex.: COM7^),
    echo   ou defina a variavel antes de chamar este .bat. Ela nao pode ser a
    echo   mesma COM dos mecanismos, das telas, da CNC nem da balanca.
    echo.
    pause
    exit /b 1
)

rem ---- O resto raramente muda ------------------------------------------------
set PORTA_SERIAL_BAUD=115200
rem  A porta nao tem heartbeat: o `estado` que o central recebe e sintetizado
rem  por este adapter e reenviado a cada tanto, enquanto o link esta ok.
if "%PORTA_ESTADO_REENVIO_S%"=="" set PORTA_ESTADO_REENVIO_S=5
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
echo   receptor     : %PORTA_SERIAL_URL% @ %PORTA_SERIAL_BAUD% ^(so leitura^)
echo   central      : %CENTRAL_URL%
echo   HTTP         : http://localhost:%PORTA_HTTP%
echo.
echo   O estado da porta comeca DESCONHECIDO ^(nao ha a quem perguntar^), e a
echo   celula fica parada ate o ritual de subida:
echo     1. passe a tag  ^(a porta libera; o LED "pode abrir" acende^)
echo     2. espere os 10 s, ou abra e feche a porta
echo     3. passe a tag de novo ^(rearme^) - a celula faz homing e retoma
echo.
echo   Se aparecer "desconectada" em /health, o suspeito n1 e o Monitor Serial
echo   da Arduino IDE ainda aberto na COM do receptor: no Windows a COM e
echo   exclusiva de um processo. Cadastro de tag ^(R/S/C/L^) e na USB DA PORTA,
echo   com notebook - este adapter nao participa.
echo.
echo   PONTO CEGO: se a ESP da porta travar parada, nada avisa. Antes de
echo   comecar, um teste de tag confirma que o caminho esta vivo.
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
