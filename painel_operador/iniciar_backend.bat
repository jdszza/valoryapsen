@echo off
cd /d "%~dp0backend"

rem ---- A UNICA linha que voce precisa editar --------------------------------
rem  A COM fixada do display de 7" (Gerenciador de Dispositivos -> Portas COM
rem  -> Propriedades -> Avancado -> Numero da Porta COM). Confira contra o
rem  ping: o display e o unico que manda {"cmd":"ping"} SEM `sub` — as quatro
rem  placas da celula carregam o subsistema no ping.
rem
rem  Vazia, o painel VARRE todas as portas a cada 3 s, abrindo as dos outros
rem  processos para conferir — com cinco placas isso e boot nao-deterministico,
rem  em que uma placa as vezes nao e achada. Na celula montada, preencha.
rem  Fora da celula (feira, treinamento, uma bancada so), COMENTE a linha
rem  abaixo: sem a variavel o painel varre e acha o display sozinho.
if "%APSEN_DISPLAY_PORTA%"=="" set APSEN_DISPLAY_PORTA=COM9

rem O painel nao sobe sem APSEN_SECRET (a chave que assina o cookie de sessao).
rem Sem esta checagem o operador veria so um traceback numa janela que fecha.
if "%APSEN_SECRET%"=="" (
    echo.
    echo APSEN_SECRET nao definida - o painel NAO sobe sem ela.
    echo A chave assina o cookie de sessao: sem ela, qualquer um assina um
    echo cookie de Admin. Gere uma e guarde no ambiente do Windows:
    echo.
    echo     python -c "import secrets; print(secrets.token_hex(32))"
    echo     setx APSEN_SECRET ^<a chave gerada^>
    echo.
    echo Depois FECHE e reabra esta janela: setx so vale para processos novos.
    echo.
    pause
    exit /b 1
)

rem Token ausente NAO impede a subida — so o bloco /api/* fica fora do ar.
if "%APSEN_API_TOKEN%"=="" (
    echo AVISO: APSEN_API_TOKEN nao definida. As rotas /api/* vao responder 503,
    echo        entao a estacao de visao nao consegue gravar estoque. O painel,
    echo        o display e o espelho do central sobem normalmente.
    echo.
)

echo.
echo   painel de bancada ^(host^)
echo   display : %APSEN_DISPLAY_PORTA%
echo   web     : http://localhost:5000
echo.
echo   Se o display ficar OFFLINE, o suspeito n1 e o Monitor Serial ainda
echo   aberto nessa COM: no Windows a porta e exclusiva de um processo.
echo.

rem `app.py` e o unico entrypoint, e ele escolhe o servidor: waitress quando
rem instalado, servidor de desenvolvimento do Flask como fallback. NAO troque
rem por `waitress-serve app:app` — aquilo importa o modulo sem passar por
rem iniciar_workers(), e o painel sobe sem a ponte serial (display OFFLINE).
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -u app.py
) else (
    echo Ambiente virtual nao encontrado. Rode primeiro:
    echo    cd backend ^&^& python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
    pause
)
