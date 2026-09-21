from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, session, flash
from functools import wraps
import hmac
import sqlite3
import os
import sys
import io
import json
import threading
import time
import qrcode
import serial
import serial.tools.list_ports
from datetime import datetime, timedelta, timezone

from werkzeug.security import check_password_hash, generate_password_hash

import central_client
import central_comandos
from central_client import CENTRAL_SYNC_S, INTEGRACAO_ATIVA, traduzir_status

app = Flask(__name__)

# ============================================================
# Segredo de sessao — a MESMA regra do central (config.validar_secret_key)
# ============================================================
# O default ficava embutido aqui, e ele esta versionado neste repositorio:
# quem le o repo assina um cookie com `perfil=Admin` e entra sem PIN nenhum.
# Warning de boot nao resolve isso — ninguem le log de bancada, e o painel
# segue funcionando como se estivesse protegido.
#
# A regra e copiada do `central-computer/config.py` de proposito: duas
# disciplinas diferentes para o mesmo problema e o que faz alguem "consertar" o
# painel copiando o habito errado. A duplicacao e toleravel porque o painel roda
# FORA do Docker e nao compartilha pacote com o central — e porque divergir aqui
# falha alto (o processo nao sobe), nunca em silencio.
SECRET_DEFAULT_PUBLICO = "apsen-dev-2024-change-in-prod"
SECRET_TAMANHO_MINIMO = 32
RECEITA_SECRET = 'python -c "import secrets; print(secrets.token_hex(32))"'


def resolver_secret_key(chave=None, ambiente=None):
    """Devolve a chave de sessao, ou levanta se ela nao puder ir para a bancada.

    `APSEN_ENV=dev` e a unica saida, e ela e explicita e visivel — nunca o
    silencio de um default embutido. Funcao pura (os dois argumentos existem
    para o teste chamar sem mexer no ambiente do processo).
    """
    if chave is None:
        chave = os.environ.get("APSEN_SECRET", "")
    if ambiente is None:
        ambiente = os.environ.get("APSEN_ENV", "")
    ambiente = (ambiente or "").strip().lower()

    if not chave or chave == SECRET_DEFAULT_PUBLICO:
        problema = "APSEN_SECRET esta com o valor default, que e publico no repositorio"
    elif len(chave) < SECRET_TAMANHO_MINIMO:
        problema = (f"APSEN_SECRET tem {len(chave)} caracteres — "
                    f"minimo {SECRET_TAMANHO_MINIMO}")
    else:
        return chave

    if ambiente == "dev":
        print(f"[seguranca] AVISO: {problema}. Tolerado porque APSEN_ENV=dev — "
              f"NAO suba assim na bancada. Gere uma chave com: {RECEITA_SECRET}")
        return chave or SECRET_DEFAULT_PUBLICO

    raise RuntimeError(
        f"{problema}. Defina APSEN_SECRET no ambiente (gere com: {RECEITA_SECRET}) "
        f"ou rode com APSEN_ENV=dev se esta for uma maquina de desenvolvimento."
    )


app.secret_key = resolver_secret_key()

# ── O cookie de sessao ───────────────────────────────────────────────────────
#
# `/trava/liberar`, `/ordens/<id>/excluir`, `/admin/historico/limpar` e
# `/medicamentos/<id>/excluir` dependiam SO do cookie: qualquer pagina aberta no
# mesmo browser podia POSTar neles em nome de quem estivesse logado. E a
# primeira dessas rotas escreve no computador central.
#
# `SameSite=Lax` e a metade que nao custa nada: o browser deixa de mandar o
# cookie em POST vindo de outra origem, e a navegacao normal (GET, link, form do
# proprio painel) continua igual. `Strict` quebraria voltar ao painel por um
# link externo, que e como o atalho da bancada abre.
#
# `Secure` so quando ha https: numa bancada em http o flag faria o browser
# DESCARTAR o cookie e ninguem conseguiria logar — o sintoma seria "o login nao
# funciona", sem nada no log. Quem publica atras de https liga
# `APSEN_COOKIE_SECURE=1`.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("APSEN_COOKIE_SECURE", "0") == "1",
)


# ============================================================
# Freio de forca bruta — a MESMA regra do central (console.py)
# ============================================================
# O `/login` do painel nao tinha nenhum: PIN de 4 digitos, 10 mil candidatos, e
# nada limitando as tentativas. O central ja tinha o freio no console e no
# `POST /auth/login`; a bancada, que e onde o PIN realmente esta, nao.
#
# Copia e nao import, pelo mesmo motivo do segredo de sessao logo acima: o
# painel roda FORA do Docker e nao compartilha pacote com o central. Os numeros
# sao os de la de proposito — dois freios com janelas diferentes para o mesmo
# problema e o que faz alguem "consertar" um copiando o habito do outro.
#
# **A chave e IP _e_ nome, e isso e escolha.** So o IP puniria o turno inteiro
# por causa de um operador que erra o PIN — a bancada fala por um NAT so, e
# atras de proxy todos caem no mesmo `remote_addr`. So o nome deixaria qualquer
# um trancar a conta alheia de fora, que e negacao de servico disfarcada de
# protecao. O par NAO cobre varredura de muitos nomes a partir de um IP; o que
# ele resolve e adivinhar o PIN de uma conta conhecida, que e o caso real aqui
# (os nomes do seed estao no README).
LOGIN_MAX_TENTATIVAS = 5
LOGIN_JANELA_S = 60.0

_login_tentativas: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def _login_origem(nome: str) -> str:
    return f"{request.remote_addr or '?'}|{(nome or '').strip().lower()}"


def login_registrar_falha(origem: str, agora: float = None) -> None:
    momento = agora if agora is not None else time.monotonic()
    with _login_lock:
        # Varre TODOS os baldes: a chave e escolhida por quem tenta, entao uma
        # varredura com um nome novo por tentativa deixaria uma entrada
        # permanente por tentativa. O custo e O(nº de baldes) por tentativa
        # FALHA, e e ele mesmo que mantem o numero pequeno.
        for chave in [k for k, ts in _login_tentativas.items()
                      if all(momento - t >= LOGIN_JANELA_S for t in ts)]:
            del _login_tentativas[chave]
        recentes = [t for t in _login_tentativas.get(origem, [])
                    if momento - t < LOGIN_JANELA_S]
        recentes.append(momento)
        _login_tentativas[origem] = recentes


def login_limpar_falhas(origem: str) -> None:
    """PIN certo zera o contador — quem sabe o PIN nao e varredura."""
    with _login_lock:
        _login_tentativas.pop(origem, None)


def login_bloqueado(origem: str, agora: float = None) -> float:
    """Segundos que faltam para a origem poder tentar de novo. 0 = liberada."""
    momento = agora if agora is not None else time.monotonic()
    with _login_lock:
        recentes = [t for t in _login_tentativas.get(origem, [])
                    if momento - t < LOGIN_JANELA_S]
        if recentes:
            _login_tentativas[origem] = recentes
        else:
            _login_tentativas.pop(origem, None)
        if len(recentes) < LOGIN_MAX_TENTATIVAS:
            return 0.0
        return max(LOGIN_JANELA_S - (momento - min(recentes)), 1.0)


# ============================================================
# Token CSRF — para os POST que destroem
# ============================================================
# `SameSite=Lax` (acima) ja recusa o cookie num POST de outra origem, e nos
# browsers atuais ele sozinho resolveria. O token existe porque a suposicao
# "todo browser da bancada e atual" e justamente a que ninguem confere: o painel
# roda no Windows do mini PC, e um atalho num navegador antigo nao da erro
# nenhum — so deixa de proteger.
#
# **Nao e um CSRF para o app inteiro, e isso e escolha.** Cobrir os ~30
# formularios exigiria tocar todos os templates, e um esquecido vira um 400 na
# cara do operador no meio do turno. O que entra aqui sao os POST que DESTROEM
# ou que saem da maquina, um por um e com o `{{ campo_csrf() }}` no form.
#
# O token e derivado da SESSAO, nao guardado nela: assim ele sobrevive a um
# restart do processo (a sessao e um cookie assinado, nao ha estado de servidor)
# e nao precisa de tabela nenhuma.
CSRF_CAMPO = "_csrf"


def _token_csrf() -> str:
    """HMAC do id da sessao com a chave do app — vazio se ninguem esta logado."""
    op_id = session.get("op_id")
    if op_id is None:
        return ""
    return hmac.new(
        str(app.secret_key).encode("utf-8"),
        f"csrf:{op_id}:{session.get('op_nome', '')}".encode("utf-8"),
        "sha256",
    ).hexdigest()


@app.context_processor
def _injetar_csrf():
    """`{{ campo_csrf() }}` monta o input escondido dentro do form."""
    from markupsafe import Markup

    def campo_csrf() -> str:
        return Markup(
            f'<input type="hidden" name="{CSRF_CAMPO}" value="{_token_csrf()}">'
        )

    return {"campo_csrf": campo_csrf, "token_csrf": _token_csrf}


def csrf_protegido(f):
    """Recusa o POST cujo token nao bate com o da sessao.

    Escrito ABAIXO de `login_required`, para RODAR depois dele — decorator
    aplica de baixo para cima e executa de cima para baixo. Sem sessao nao ha
    token a comparar, e o redirect para o login e a resposta certa: 400 ali
    mandaria quem so perdeu a sessao caçar um problema que nao existe.

    O marcador `_csrf_protegido` sobe pela pilha porque `functools.wraps` copia
    o `__dict__` do embrulhado — a mesma mecanica com que
    `tests/test_painel_seguranca.py` ja varre o `api_token_required`.

    `hmac.compare_digest` e nao `==`: a comparacao byte a byte de `==` vaza o
    prefixo correto pelo tempo, e o token e a unica coisa que separa este POST
    de um vindo de fora.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        esperado = _token_csrf()
        enviado = request.form.get(CSRF_CAMPO, "")
        if not esperado or not hmac.compare_digest(esperado, enviado):
            return render_template(
                "403.html", perfil=session.get("perfil"),
                motivo="Formulário expirado ou vindo de outra origem. "
                       "Recarregue a página e tente de novo.",
            ), 400
        return f(*args, **kwargs)
    decorated._csrf_protegido = True
    return decorated


# ============================================================
# `next=` do login — so caminho do proprio painel
# ============================================================


def destino_seguro(bruto: str, padrao: str) -> str:
    """O `next=` do login, reduzido a um caminho relativo deste app.

    `redirect(request.args.get("next"))` mandava o browser para qualquer lugar:
    bastava um link para `/login?next=https://...` para a pagina de login
    legitima do painel virar o trampolim de uma copia dela. O operador digita o
    PIN na tela certa e sai do outro lado sem ver nada estranho.

    A regra e por FORMA, nao por lista de bloqueio: aceita-se um caminho que
    comece com uma barra e nao com duas — `//outro.host/x` e um URL de esquema
    relativo, que o browser segue para fora. Funcao pura, para o teste chamar
    sem requisicao.
    """
    bruto = (bruto or "").strip()
    if not bruto.startswith("/") or bruto.startswith("//"):
        return padrao
    # Barra invertida em QUALQUER posicao: alguns browsers a normalizam para
    # barra, entao `/\outro.host/x` chega la fora sem nunca ter tido duas
    # barras — e um caminho legitimo deste painel nunca tem uma.
    if "\\" in bruto:
        return padrao
    return bruto


# ============================================================
# Autenticacao das rotas /api/* — token de maquina
# ============================================================
API_TOKEN_HEADER = "X-API-Token"


def _api_token_configurado() -> str:
    """Lido a CADA requisicao, e nao no import: o `.bat` da bancada e reiniciado
    com frequencia, e um token lido uma vez so deixaria a diferenca entre
    "configurei agora" e "reiniciei" invisivel para quem opera."""
    return (os.environ.get("APSEN_API_TOKEN") or "").strip()


def api_token_required(f):
    """Exige `X-API-Token` em toda rota /api/*.

    Quem chama essas rotas e MAQUINA — a estacao de visao e o que restou do
    contrato HTTP do display —, entao o segredo e um token de ambiente e nao a
    sessao do navegador: nao ha quem digite PIN do outro lado.

    **Sem `APSEN_API_TOKEN` a API responde 503 e o painel sobe assim mesmo.** E
    a diferenca para o `APSEN_SECRET` logo acima, e ela nao e de gosto: sem
    segredo de sessao TUDO que o painel serve e forjavel, e ai recusar o boot e
    proporcional; sem token, so o bloco /api/* fica sem dono — derrubar o
    processo levaria junto a ponte serial, ou seja, a tela que o operador esta
    olhando, por causa de uma variavel que a bancada talvez nem use. E a mesma
    escolha do `CONSOLE_SENHA` no central: segredo ausente desliga o RECURSO,
    nao o processo.

    503 e nao 404 pelo mesmo motivo de la: as duas escondem a API de quem nao
    tem o token, entao quem decide e o outro leitor — o operador que configurou
    errado. 404 manda essa pessoa cacar o problema na URL; 503 dizendo "defina
    APSEN_API_TOKEN" encerra o assunto numa linha.

    Nunca um token default embutido: ele ficaria versionado, que e exatamente o
    bug que esta mudanca existe para fechar.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        esperado = _api_token_configurado()
        if not esperado:
            return jsonify({
                "ok": False,
                "erro": "APSEN_API_TOKEN nao definido — API desabilitada",
            }), 503
        recebido = request.headers.get(API_TOKEN_HEADER, "")
        # Bytes, e nao str: `compare_digest` levanta TypeError com caractere
        # fora do ASCII, e um header malformado nao pode virar 500.
        if not hmac.compare_digest(recebido.encode("utf-8", "replace"),
                                   esperado.encode("utf-8", "replace")):
            return jsonify({"ok": False, "erro": "token invalido"}), 401
        return f(*args, **kwargs)

    # Marcador que `tests/test_painel_seguranca.py` varre no `url_map`: rota
    # /api/* sem ele reprova, sem lista manual para alguem esquecer.
    decorated.api_token_required = True
    return decorated


# Empacotado como .exe (PyInstaller), __file__ aponta para a pasta temporaria
# de extracao, apagada quando o programa fecha — o banco tem que viver ao lado
# do executavel para sobreviver entre execucoes e atualizacoes.
if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(__file__)
# APSEN_DB existe para a suite de testes apontar o banco para um arquivo
# temporario sem tocar no da bancada — e serve tambem a quem queira rodar duas
# instancias na mesma pasta.
DB_PATH = os.environ.get("APSEN_DB") or os.path.join(_BASE_DIR, "apsen.db")

# ============================================================
# Comunicacao com o display ESP32 via Serial USB (substitui MQTT/HTTP).
# A porta e auto-detectada: nenhuma porta e fixada no codigo.
# ============================================================
SERIAL_BAUD = 115200

_serial_lock = threading.Lock()
_serial_conn = None  # serial.Serial aberta e validada, ou None se desconectado


def extrair_json(raw):
    """Extrai o objeto JSON de uma linha do display, mesmo grudado em log.

    O firmware imprime texto e JSON no mesmo Serial, e nada garante que caiam em
    linhas separadas — no boot sai literalmente:

        Conectando ao backend via Serial...{"cmd":"ping"}

    Exigir que a linha COMECE com '{' descartava justamente os pings do boot,
    que sao os primeiros a chegar. A deteccao de porta passava a depender dos
    pings avulsos posteriores, dentro de uma janela apertada — e falhava de
    forma intermitente, sem nada no log explicando por que o display ficou
    OFFLINE. Devolve None quando nao ha JSON valido na linha.
    """
    inicio = raw.find("{")
    if inicio < 0:
        return None
    try:
        return json.loads(raw[inicio:])
    except json.JSONDecodeError:
        return None


def _probe_port(port_name):
    """Abre uma porta candidata e escuta o ping que o proprio display manda
    periodicamente (no boot e depois a cada poucos segundos). O display e
    quem sempre inicia o ping — o backend so responde com pong; por isso a
    deteccao de porta escuta em vez de perguntar. Evita depender de VID/PID
    especifico: so uma porta com o firmware real vai mandar esse ping."""
    # DTR/RTS desligados ANTES de abrir. Nesta placa esses dois sinais sao o
    # circuito de reset/boot do ESP32-S3: abrir a porta do jeito padrao do
    # pyserial reinicia o display. Isso criava um ciclo em que a sondagem nunca
    # terminava — abre (reset) -> ESP leva ~5 s bootando (painel, SD, logos) ->
    # o probe desiste em 8 s e fecha (outro reset) -> repete. O display nunca
    # alcancava o loop de ping, ficava com a tela apagada, e o log so dizia
    # "display desconectado". Sondar um dispositivo nao pode reinicia-lo.
    conn = serial.Serial()
    conn.port = port_name
    conn.baudrate = SERIAL_BAUD
    conn.timeout = 1
    conn.dtr = False
    conn.rts = False
    try:
        conn.open()
    except (serial.SerialException, OSError):
        return None
    time.sleep(1.5)  # deixa a porta assentar
    try:
        conn.reset_input_buffer()
        deadline = time.time() + 8
        while time.time() < deadline:
            raw = conn.readline().decode(errors="ignore").strip()
            msg = extrair_json(raw)
            if msg is None:
                continue
            if msg.get("cmd") == "ping":
                conn.write(f'{{"resp":"pong","epoch":{int(time.time())}}}\n'.encode())
                conn.flush()
                return conn
    except (serial.SerialException, OSError):
        pass
    conn.close()
    return None


def porta_fixa_do_display() -> str:
    """`APSEN_DISPLAY_PORTA`, lida a CADA tentativa (o `.bat` da bancada e
    reiniciado com frequencia — ver `_api_token_configurado`)."""
    return (os.environ.get("APSEN_DISPLAY_PORTA") or "").strip()


def varredura_permitida() -> bool:
    """A varredura de portas e OPT-IN desde que a celula ganhou cinco placas.

    Ela continua sendo o caminho de quem desenvolve SEM hardware ou com uma
    placa so na mesa — `APSEN_DISPLAY_VARRER=1` a devolve. O que ela deixou de
    ser e o DEFAULT, porque na bancada montada ela e o problema, nao a
    conveniencia.

    Lida a cada tentativa, como `porta_fixa_do_display` e pelo mesmo motivo: o
    `.bat` da bancada e reiniciado com frequencia.
    """
    return (os.environ.get("APSEN_DISPLAY_VARRER") or "").strip() == "1"


def find_display_port():
    """Abre a porta do display: a FIXA, se `APSEN_DISPLAY_PORTA` estiver
    definida; senao, varre todas as portas procurando o ping.

    A varredura e para desenvolvimento SEM hardware. Na celula montada ela e o
    problema: cada processo que varre abre, por ate 9,5 s, as portas DOS OUTROS
    (a do dispenser, a da CNC, a da balanca, a das telas) so para descobrir se
    o display esta ali — e, enquanto segura uma delas, o dono de verdade toma
    ACCESS_DENIED na propria porta. Com cinco processos e cinco portas isso nao
    da erro: da boot nao-deterministico, em que uma placa as vezes simplesmente
    nao e achada. Porta fixa elimina a varredura, e sem varredura nao ha
    competicao (README, "Portas seriais na célula montada").

    A porta fixa passa pelo MESMO `_probe_port`: DTR/RTS desligados (o circuito
    de reset do ESP32-S3) e a confirmacao pelo ping — fixar o numero nao
    dispensa conferir que ha um display do outro lado.
    """
    fixa = porta_fixa_do_display()
    if fixa:
        conn = _probe_port(fixa)
        if conn:
            print(f"Display encontrado na porta {fixa} (APSEN_DISPLAY_PORTA)")
        return conn

    if not varredura_permitida():
        # **Aviso, e nao recusa de boot** — a diferenca para os adapters e
        # deliberada, e e a mesma do `APSEN_API_TOKEN`. Os adapters existem para
        # falar com UMA porta, e sem ela nao fazem nada: recusar subir e honesto
        # la. Este processo tem DOIS trabalhos, e o outro e servir a tela que o
        # gestor esta olhando — derruba-lo por causa de uma variavel de serial
        # trocaria um display offline por um painel inteiro fora do ar.
        #
        # O que se ganha e o mesmo: sem varredura nao ha competicao pelas portas
        # dos outros quatro processos. O display fica OFFLINE, e a linha abaixo
        # diz exatamente o que definir.
        print("[serial] APSEN_DISPLAY_PORTA nao definida e a varredura esta "
              "desligada (APSEN_DISPLAY_VARRER=1 a religa). Com cinco placas na "
              "celula, varrer faz este processo abrir as portas dos outros e "
              "vice-versa. Fixe a COM do display e defina APSEN_DISPLAY_PORTA.")
        return None

    for p in serial.tools.list_ports.comports():
        conn = _probe_port(p.device)
        if conn:
            print(f"Display encontrado na porta {p.device}")
            return conn
    return None


def push_to_display(payload: dict):
    """Envia uma mensagem nao solicitada ao display (ex: status de ordem
    mudou no dashboard web). Silencioso se o display estiver desconectado."""
    with _serial_lock:
        if _serial_conn is None:
            print(f"[push] '{payload.get('push')}' descartado: display desconectado")
            return
        try:
            bruto = (json.dumps(payload) + "\n").encode()
            _serial_conn.write(bruto)
            _serial_conn.flush()
            print(f"[push] '{payload.get('push')}' enviado ({len(bruto)} bytes)")
        except (serial.SerialException, OSError) as exc:
            print(f"[push] '{payload.get('push')}' falhou: {exc}")


def publish_order_action(numero_os, status, source="app"):
    if source == "display":
        return  # evita eco: a propria mudanca ja veio do display
    push_to_display({"push": "ordem_status", "numero_os": numero_os, "status": status})


# cmd de leitura -> chave "resp" que o firmware espera de volta.
# `validar_operador` responde com a tag "operador" e NAO entra aqui: ele nao
# devolve `data`, e sim o veredito de um PIN (ver `_handle_serial_message`).
_GET_RESP_TAG = {
    "get_ordens": "ordens",
    "get_catalogo": "catalogo",
    "get_operadores": "operadores",
    "get_dispensers": "dispensers",
}


def _handle_serial_message(conn, msg):
    """Processa uma linha JSON recebida do display e escreve a resposta
    esperada (quando houver). Roda na thread do serial_worker.

    Toda operacao de banco fica em try/finally para garantir o close() mesmo
    em erro (uma conexao vazada aberta, sem commit/rollback, foi a causa de
    um "database is locked" observado quando um update de dispenser falhava
    no meio). Erros tambem viram uma resposta de erro para o display, em vez
    de deixar o firmware esperando ate estourar o timeout do serial_request.
    """
    cmd = msg.get("cmd")
    event = msg.get("event")
    reply = None

    try:
        if cmd == "ping":
            reply = {"resp": "pong", "epoch": int(time.time())}
        elif cmd in _GET_RESP_TAG:
            db = get_db()
            try:
                if cmd == "get_ordens":
                    data = _ordens_pendentes_data(db)
                elif cmd == "get_catalogo":
                    data = _catalogo_data(db)
                elif cmd == "get_operadores":
                    data = _operadores_data(db)
                else:
                    data = _dispensers_data(db)
            finally:
                db.close()
            reply = {"resp": _GET_RESP_TAG[cmd], "data": data}
        elif cmd == "validar_operador":
            # O display manda o PIN; QUEM CONFERE E O BACKEND.
            #
            # A alternativa era mandar o hash junto com a lista de operadores e
            # deixar o display comparar, como ele fazia com o PIN em claro. Ela
            # nao resolve nada aqui: o PIN tem 4 digitos, ou seja, 10 mil
            # candidatos — quem puser a mao no cartao SD (o firmware gravava a
            # lista em /operadores.json) ou escutar o cabo USB tem todos os PINs
            # em milissegundos, com qualquer algoritmo. Hash so protege onde o
            # atacante NAO consegue enumerar a entrada.
            #
            # Validar aqui traz junto o que faltava: operador desativado perde
            # acesso na hora, em vez de continuar entrando ate o display
            # atualizar a lista dele.
            #
            # O preco e que nao da para logar no display com o backend fora do
            # ar. Ele ja nao dava: o backend E o unico canal do display — sem
            # ele nao ha ordem, catalogo nem estoque na tela. Isso NAO contraria
            # "a bancada precisa funcionar com o central desligado": o central e
            # outra maquina, este processo e o dono da porta serial.
            db = get_db()
            try:
                resultado = _validar_pin_data(db, msg.get("pin", ""))
            finally:
                db.close()
            reply = {"resp": "operador", **resultado}
        elif cmd == "set_status":
            db = get_db()
            try:
                ok, erro = _set_status_by_numero_os(db, msg.get("numero_os", ""), msg.get("status", ""))
            finally:
                db.close()
            reply = {"resp": "ok", "ok": ok}
            if not ok:
                reply["msg"] = erro
        elif cmd == "sync_dispensers":
            db = get_db()
            try:
                ok, erro = _sync_dispensers_data(db, msg.get("itens", []))
            finally:
                db.close()
            reply = {"resp": "ok", "ok": ok}
            if not ok:
                reply["msg"] = erro
        elif cmd == "set_dispenser_med":
            db = get_db()
            try:
                ok, erro = _set_dispenser_med_data(db, msg.get("slot"), msg.get("nome", ""))
            finally:
                db.close()
            reply = {"resp": "ok", "ok": ok}
            if not ok:
                reply["msg"] = erro
        elif cmd == "get_trava":
            # Tag propria ("trava"), como `validar_operador`: nao devolve `data`,
            # devolve o estado. Cache de TRAVA_CACHE_S — ver `_trava_para_display`.
            reply = {"resp": "trava", **_trava_para_display(), "ts": int(time.time())}
        elif cmd == "liberar_trava":
            # Nome + PIN, e nao o operador logado no display: o supervisor e
            # OUTRA pessoa, que chega a bancada, libera e vai embora. Quem
            # confere o PIN e o backend, pelo mesmo motivo do `validar_operador`.
            # A resposta NUNCA carrega o PIN.
            db = get_db()
            try:
                ok, texto = _liberar_trava_display(db, msg.get("nome", ""), msg.get("pin", ""))
            finally:
                db.close()
            reply = {"resp": "ok", "ok": ok, "msg": texto}
        elif event == "historico":
            db = get_db()
            try:
                op_row = db.execute(
                    "SELECT perfil FROM operadores WHERE nome=? AND ativo=1", (msg.get("operador", ""),)
                ).fetchone()
                registrar_historico(
                    db, msg.get("operador", "---"), op_row["perfil"] if op_row else "",
                    msg.get("acao", ""), msg.get("detalhes", ""),
                )
                db.commit()
            finally:
                db.close()
        elif event == "desvio":
            db = get_db()
            try:
                op_row = db.execute(
                    "SELECT perfil FROM operadores WHERE nome=? AND ativo=1", (msg.get("operador", ""),)
                ).fetchone()
                registrar_desvio(
                    db, msg.get("numero_os", ""), msg.get("tipo", "Outro"), msg.get("descricao", ""),
                    msg.get("operador", ""), op_row["perfil"] if op_row else "",
                )
                db.commit()
            finally:
                db.close()
        elif event == "ordem_concluida":
            print(f"[display] ordem concluida: {msg.get('id')} por {msg.get('operador')}")
    except Exception as e:
        print(f"Erro processando {cmd or event}: {e}")
        if cmd in _GET_RESP_TAG:
            reply = {"resp": _GET_RESP_TAG[cmd], "data": [], "erro": str(e)}
        elif cmd == "get_trava":
            # Tag propria, pelo mesmo motivo de `validar_operador` logo abaixo.
            reply = {"resp": "trava", **TRAVA_DESCONHECIDA, "ts": int(time.time()),
                     "erro": str(e)}
        elif cmd == "validar_operador":
            # Tag propria: o display espera "operador" e descarta o resto.
            # Responder {"resp":"ok"} aqui o deixaria travado ate o timeout,
            # que e o que este `except` existe para evitar.
            reply = {"resp": "operador", "ok": False, "msg": str(e)}
        elif cmd:
            reply = {"resp": "ok", "ok": False, "msg": str(e)}

    if reply is not None:
        with _serial_lock:
            try:
                conn.write((json.dumps(reply) + "\n").encode())
                conn.flush()
            except (serial.SerialException, OSError):
                pass


def serial_worker():
    global _serial_conn
    while True:
        if _serial_conn is None:
            _serial_conn = find_display_port()
            if _serial_conn is None:
                time.sleep(3)
                continue

        try:
            raw = _serial_conn.readline().decode(errors="ignore").strip()
        except (serial.SerialException, OSError):
            with _serial_lock:
                try:
                    _serial_conn.close()
                except Exception:
                    pass
                _serial_conn = None
            continue

        if not raw:
            continue

        msg = extrair_json(raw)
        if msg is None:
            print(f"[display] {raw}")
            continue
        # parte de log que veio grudada antes do JSON — nao pode sumir do console
        prefixo = raw[:raw.find("{")].strip()
        if prefixo:
            print(f"[display] {prefixo}")

        try:
            _handle_serial_message(_serial_conn, msg)
        except Exception as e:
            print(f"Erro processando mensagem serial: {e}")


MEDICAMENTOS_DEFAULT = [
    ("Amoxicilina 500mg", 60, 60),
    ("Paracetamol 750mg", 60, 60),
    ("Ibuprofeno 600mg", 60, 60),
    ("Dipirona 500mg", 60, 60),
    ("Vitamina C 1g", 60, 60),
    ("Omeprazol 20mg", 60, 60),
    ("Losartana 50mg", 60, 60),
    ("Metformina 850mg", 60, 60),
]

CATALOGO_DEFAULT = [
    "Amoxicilina 500mg",
    "Paracetamol 750mg",
    "Ibuprofeno 600mg",
    "Dipirona 500mg",
    "Vitamina C 1g",
    "Omeprazol 20mg",
    "Losartana 50mg",
    "Metformina 850mg",
    "Azitromicina 500mg",
    "Dexametasona 4mg",
    "Prednisona 20mg",
    "Cefalexina 500mg",
    "Fluconazol 150mg",
    "Ranitidina 150mg",
    "Captopril 25mg",
]


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # As FKs sao declaradas por conexao, nao por banco: no SQLite o
    # `foreign_keys` nasce DESLIGADO em toda conexao nova, e sem ele o
    # `REFERENCES medicamentos(id)` de `lotes` e decoracao — o banco aceita a
    # linha filha sem pai e nao reclama nunca. Ligar aqui e o unico lugar que
    # cobre todas as conexoes, porque todas saem desta funcao.
    #
    # O PRAGMA e ignorado dentro de transacao, entao ele vem logo apos o
    # connect, antes de qualquer BEGIN.
    #
    # Ele nao e retroativo: orfao gravado antes de hoje continua no banco.
    # Apaga-lo seria destruir rastreabilidade para arrumar uma estatistica —
    # e na pratica esses orfaos ja estao fora de alcance, porque toda consulta
    # de lote passa por JOIN com `medicamentos`.
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ============================================================
# Perfis e permissões
# ============================================================
PERFIS = ["Admin", "Supervisor", "PCP", "PCM", "Operador"]

# Cada chave é uma permissão; True = permitido para o perfil.
#
# `trava_liberar` é a única permissão que ESCREVE no computador central: libera
# a trava do Triple Check (ver `central_comandos.py`). Só Admin e Supervisor a
# têm. O Supervisor é quem chega à bancada, libera e vai embora — vê o
# dashboard (onde a faixa da trava aparece) e as ordens, e nada mais.
PERMISSOES = {
    "Admin": {
        "dash": True,
        "trava_liberar": True,
        "ordens_ver": True,  "ordens_criar": True,  "ordens_editar": True,
        "ordens_excluir": True, "ordens_status": True,
        "dispensers_ver": True, "dispensers_editar": True,
        "catalogo_ver": True,   "catalogo_editar": True,
        "operadores_ver": True, "operadores_editar": True,
        "historico_ver": True,  "historico_limpar": True,
        "relatorio_ver": True,
        "lotes_ver": True, "lotes_editar": True,
        "desvios_ver": True, "desvios_editar": True,
        "kpis_ver": True,
        "clientes_ver": True, "clientes_editar": True,
        "visao_ver": True, "visao_editar": True,
    },
    "Supervisor": {
        "dash": True,
        "trava_liberar": True,
        "ordens_ver": True, "ordens_criar": False, "ordens_editar": False,
        "ordens_excluir": False, "ordens_status": False,
        "dispensers_ver": False, "dispensers_editar": False,
        "catalogo_ver": False,   "catalogo_editar": False,
        "operadores_ver": False, "operadores_editar": False,
        "historico_ver": False,  "historico_limpar": False,
        "relatorio_ver": False,
        "lotes_ver": False, "lotes_editar": False,
        "desvios_ver": False, "desvios_editar": False,
        "kpis_ver": False,
        "clientes_ver": False, "clientes_editar": False,
        "visao_ver": False, "visao_editar": False,
    },
    "PCP": {
        "dash": True,
        "trava_liberar": False,
        "ordens_ver": True, "ordens_criar": True, "ordens_editar": True,
        "ordens_excluir": False, "ordens_status": True,
        "dispensers_ver": True, "dispensers_editar": False,
        "catalogo_ver": True,   "catalogo_editar": False,
        "operadores_ver": False, "operadores_editar": False,
        "historico_ver": True,  "historico_limpar": False,
        "relatorio_ver": True,
        "lotes_ver": True, "lotes_editar": False,
        "desvios_ver": True, "desvios_editar": True,
        "kpis_ver": True,
        "clientes_ver": True, "clientes_editar": True,
        "visao_ver": True, "visao_editar": False,
    },
    "PCM": {
        "dash": True,
        "trava_liberar": False,
        "ordens_ver": True, "ordens_criar": False, "ordens_editar": False,
        "ordens_excluir": False, "ordens_status": False,
        "dispensers_ver": True, "dispensers_editar": True,
        "catalogo_ver": True,   "catalogo_editar": True,
        "operadores_ver": False, "operadores_editar": False,
        "historico_ver": True,  "historico_limpar": False,
        "relatorio_ver": True,
        "lotes_ver": True, "lotes_editar": True,
        "desvios_ver": True, "desvios_editar": True,
        "kpis_ver": True,
        "clientes_ver": True, "clientes_editar": False,
        "visao_ver": True, "visao_editar": True,
    },
    "Operador": {  # somente display, sem acesso web
        "dash": False,
        "trava_liberar": False,
        "ordens_ver": False, "ordens_criar": False, "ordens_editar": False,
        "ordens_excluir": False, "ordens_status": False,
        "dispensers_ver": False, "dispensers_editar": False,
        "catalogo_ver": False,   "catalogo_editar": False,
        "operadores_ver": False, "operadores_editar": False,
        "historico_ver": False,  "historico_limpar": False,
        "relatorio_ver": False,
        "lotes_ver": False, "lotes_editar": False,
        "desvios_ver": False, "desvios_editar": False,
        "kpis_ver": False,
        "clientes_ver": False, "clientes_editar": False,
        "visao_ver": False, "visao_editar": False,
    },
}


def _pode(perm: str) -> bool:
    return PERMISSOES.get(session.get("perfil", ""), {}).get(perm, False)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "op_id" not in session:
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated


def requer(*perms):
    """Bloqueia rota se NENHUMA das permissões for True (OR lógico)."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not any(_pode(p) for p in perms):
                return render_template("403.html", perfil=session.get("perfil")), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


@app.context_processor
def inject_user():
    return {
        "sess_nome":   session.get("op_nome", ""),
        "sess_perfil": session.get("perfil", ""),
        "pode": _pode,
    }

# ============================================================
# PIN de operador — hash, nunca texto
# ============================================================
# `werkzeug.security` e nao `bcrypt`: o Werkzeug ja vem com o Flask, e o painel
# tem a mesma disciplina do central de nao ganhar dependencia sem necessidade
# real. O algoritmo (pbkdf2:sha256 com sal por operador) fica gravado no proprio
# hash, entao trocar de esquema depois nao invalida o que ja esta no banco.


def gerar_pin_hash(pin: str) -> str:
    """Hash do PIN, com o custo padrao do Werkzeug (hoje scrypt).

    O custo alto e o ponto, e nao um efeito colateral: o PIN tem 4 digitos, o
    arquivo .db anda de pendrive, e ~300 ms por tentativa e o que separa um
    banco copiado de todos os PINs da bancada. Quem paga a conta e a validacao
    do display, que percorre os operadores ativos — por isso o firmware espera
    5 s nesse pedido, e nao os 800 ms dos demais.
    """
    return generate_password_hash((pin or "").strip())


def conferir_pin(pin_hash, pin: str) -> bool:
    """True se o PIN digitado corresponde ao hash gravado.

    Hash vazio (operador migrado de um banco em que o PIN estava em branco)
    recusa sempre, em vez de deixar `check_password_hash` decidir por um valor
    que nunca foi uma senha. Hash malformado tambem recusa — e o mesmo motivo
    do `verificar_senha` do central: formato estranho no banco nao pode virar
    500 na tela de login.
    """
    if not pin_hash or not pin:
        return False
    try:
        return check_password_hash(pin_hash, pin)
    except (ValueError, TypeError):
        return False


# PIN do seed: e o primeiro login documentado no README, e o proprio README
# manda troca-lo em Admin -> Operadores antes de qualquer uso real. O que mudou
# e que nem ele existe em claro no banco — o seed grava o HASH, como qualquer
# operador cadastrado depois.
OPERADORES_DEFAULT = [
    ("Administrador", "1234", "Admin"),
    ("Operador 1",    "0001", "Operador"),
]


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ordens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_os TEXT UNIQUE NOT NULL,
            itens TEXT NOT NULL,
            destino TEXT NOT NULL,
            prioridade TEXT NOT NULL DEFAULT 'Normal',
            status TEXT NOT NULL DEFAULT 'Pendente',
            data_criacao TEXT NOT NULL,
            data_atualizacao TEXT NOT NULL,
            origem TEXT NOT NULL DEFAULT 'local',
            os_id_central TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS medicamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT UNIQUE NOT NULL,
            quantidade INTEGER NOT NULL DEFAULT 60,
            capacidade INTEGER NOT NULL DEFAULT 60,
            ativo INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS catalogo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS operadores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            pin_hash TEXT NOT NULL DEFAULT '',
            perfil TEXT NOT NULL DEFAULT 'Operador',
            ativo INTEGER NOT NULL DEFAULT 1,
            data_criacao TEXT NOT NULL,
            pin_provisorio INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS historico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operador TEXT NOT NULL,
            perfil TEXT DEFAULT '',
            acao TEXT NOT NULL,
            detalhes TEXT DEFAULT '',
            timestamp TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            medicamento_id INTEGER NOT NULL REFERENCES medicamentos(id),
            lote TEXT NOT NULL,
            validade TEXT,
            quantidade INTEGER NOT NULL DEFAULT 0,
            quantidade_inicial INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Ativo',
            data_entrada TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT UNIQUE NOT NULL,
            cnpj TEXT DEFAULT '',
            endereco TEXT DEFAULT '',
            contato TEXT DEFAULT '',
            telefone TEXT DEFAULT '',
            ativo INTEGER NOT NULL DEFAULT 1,
            data_cadastro TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lotes_baixas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lote_id INTEGER,
            medicamento_id INTEGER,
            medicamento_nome TEXT NOT NULL,
            lote TEXT NOT NULL,
            quantidade INTEGER NOT NULL,
            motivo TEXT NOT NULL,
            operador TEXT DEFAULT '',
            data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ordem_lotes_consumidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ordem_id INTEGER,
            numero_os TEXT NOT NULL,
            medicamento_id INTEGER,
            medicamento_nome TEXT NOT NULL,
            lote_id INTEGER,
            lote TEXT NOT NULL,
            quantidade INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS desvios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ordem_id INTEGER,
            numero_os TEXT DEFAULT '',
            tipo TEXT NOT NULL,
            descricao TEXT DEFAULT '',
            operador TEXT DEFAULT '',
            perfil TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Aberto',
            causa_raiz TEXT DEFAULT '',
            acao_corretiva TEXT DEFAULT '',
            data_abertura TEXT NOT NULL,
            data_resolucao TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS estoque_visao (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            momento TEXT NOT NULL,
            estacao TEXT DEFAULT '',
            dispenser INTEGER,
            medicamento_id INTEGER,
            medicamento_nome TEXT DEFAULT '',
            sku TEXT DEFAULT '',
            caixas INTEGER,
            unidades INTEGER,
            unidades_anterior INTEGER,
            delta INTEGER,
            confianca REAL,
            veredito TEXT DEFAULT '',
            numero_os TEXT DEFAULT '',
            acao TEXT NOT NULL DEFAULT '',
            detalhe TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_estoque_visao_momento ON estoque_visao(momento);
        CREATE INDEX IF NOT EXISTS idx_estoque_visao_acao    ON estoque_visao(acao);
    """)

    # Migration: espelho do computador central.
    #   origem        -> 'local' (nasceu aqui) | 'central' (espelhada, so-leitura)
    #   os_id_central -> o os_id da ordem no central, o mesmo valor de numero_os.
    # Coluna nova exige as DUAS entradas: a definitiva no CREATE TABLE acima
    # (banco novo) e o ALTER aqui (banco de versao anterior) — CREATE TABLE IF
    # NOT EXISTS nao repara tabela que ja existe.
    ordem_cols = [r[1] for r in conn.execute("PRAGMA table_info(ordens)").fetchall()]
    if "origem" not in ordem_cols:
        conn.execute("ALTER TABLE ordens ADD COLUMN origem TEXT NOT NULL DEFAULT 'local'")
    if "os_id_central" not in ordem_cols:
        conn.execute("ALTER TABLE ordens ADD COLUMN os_id_central TEXT DEFAULT ''")

    # Migration: adiciona coluna minimo se ainda nao existir
    cols = [r[1] for r in conn.execute("PRAGMA table_info(medicamentos)").fetchall()]
    if "minimo" not in cols:
        conn.execute("ALTER TABLE medicamentos ADD COLUMN minimo INTEGER DEFAULT 10")

    # Migration: `ativo` — o medicamento sai do catalogo sem sair da tabela.
    # Coluna nova exige as DUAS entradas, a definitiva no CREATE TABLE acima e
    # o ALTER aqui: `CREATE TABLE IF NOT EXISTS` nao repara tabela existente.
    if "ativo" not in cols:
        conn.execute("ALTER TABLE medicamentos ADD COLUMN ativo INTEGER NOT NULL DEFAULT 1")
        cols.append("ativo")

    # Migration: adiciona coluna rfid_uid em operadores (login por crachá)
    op_cols = [r[1] for r in conn.execute("PRAGMA table_info(operadores)").fetchall()]
    if "rfid_uid" not in op_cols:
        conn.execute("ALTER TABLE operadores ADD COLUMN rfid_uid TEXT DEFAULT ''")
        op_cols.append("rfid_uid")

    # Migration: o PIN vira HASH, e a coluna em claro SAI do banco.
    #
    # Roda depois da de rfid_uid de proposito: a reconstrucao abaixo copia a
    # tabela coluna a coluna, e a nova precisa ja existir do lado antigo.
    #
    # Apagar o texto do PIN nao basta ser feito na escrita nova: o banco da
    # bancada ja tem os PINs de todo mundo em claro, e um arquivo .db anda de
    # pendrive. Por isso a migracao CONVERTE o que existe e depois se livra da
    # coluna — nao ha caminho em que o painel volte a ler `pin`.
    #
    # A coluna some por reconstrucao, e nao por `ALTER TABLE ... DROP COLUMN`,
    # por duas razoes: DROP COLUMN so existe no SQLite >= 3.35 (o painel roda no
    # Python que a bancada tiver), e a coluna antiga e `NOT NULL` sem default —
    # enquanto ela existir, todo INSERT que nao a mencione falha. Mante-la
    # "vazia" trocaria um vazamento por um bug de cadastro.
    if "pin" in op_cols:
        if "pin_hash" not in op_cols:
            conn.execute("ALTER TABLE operadores ADD COLUMN pin_hash TEXT NOT NULL DEFAULT ''")
        for row in conn.execute(
            "SELECT id, pin FROM operadores WHERE pin_hash IS NULL OR pin_hash=''"
        ).fetchall():
            pin_claro = (row["pin"] or "").strip()
            if pin_claro:
                conn.execute("UPDATE operadores SET pin_hash=? WHERE id=?",
                             (gerar_pin_hash(pin_claro), row["id"]))
        conn.executescript("""
            CREATE TABLE operadores_sem_pin (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                pin_hash TEXT NOT NULL DEFAULT '',
                perfil TEXT NOT NULL DEFAULT 'Operador',
                ativo INTEGER NOT NULL DEFAULT 1,
                data_criacao TEXT NOT NULL,
                rfid_uid TEXT DEFAULT ''
            );
            INSERT INTO operadores_sem_pin
                (id, nome, pin_hash, perfil, ativo, data_criacao, rfid_uid)
                SELECT id, nome, pin_hash, perfil, ativo, data_criacao,
                       COALESCE(rfid_uid, '') FROM operadores;
            DROP TABLE operadores;
            ALTER TABLE operadores_sem_pin RENAME TO operadores;
        """)
    elif "pin_hash" not in op_cols:
        # Banco que nasceu sem `pin` e sem `pin_hash` nao existe hoje, mas a
        # entrada de reparo e obrigatoria: `CREATE TABLE IF NOT EXISTS` nao
        # repara tabela que ja existe.
        conn.execute("ALTER TABLE operadores ADD COLUMN pin_hash TEXT NOT NULL DEFAULT ''")

    op_cols = [r[1] for r in
               conn.execute("PRAGMA table_info(operadores)").fetchall()]
    # Migration: `pin_provisorio` — o PIN que veio do seed precisa ser trocado.
    #
    # O hash sempre esteve certo; o problema era o SEGREDO. Os PINs do seed
    # estão neste repositório, e na bancada os TRÊS operadores ainda estavam com
    # eles — incluindo o **Supervisor**, que é o perfil que libera a trava do
    # Triple Check. Somado a um `/login` que não tinha freio, eram 4 dígitos com
    # o valor publicado e tentativas ilimitadas.
    #
    # `DEFAULT 0` no ALTER, e não 1: um banco que já está na bancada pode ter
    # PINs trocados há meses, e marcar todo mundo como provisório trancaria o
    # turno inteiro numa tela de troca. Quem nasce provisório é quem o SEED
    # cria — a linha abaixo trata do banco que já existe com os PINs de fábrica.
    if "pin_provisorio" not in op_cols:
        conn.execute(
            "ALTER TABLE operadores ADD COLUMN pin_provisorio INTEGER NOT NULL DEFAULT 0")
        op_cols.append("pin_provisorio")
        # Um banco de versão anterior não tem como saber quem trocou o PIN. O
        # que dá para saber é quem ainda está com o PIN DE FÁBRICA, e isso se
        # confere com o hash que já está gravado.
        # Contra TODOS os PINs do seed, e não contra o do operador de mesmo
        # nome: na bancada havia um "Supervisor" — perfil que libera a trava —
        # com o PIN do Administrador, e ele não está em `OPERADORES_DEFAULT`.
        # Casar por nome deixaria de fora exatamente a conta mais perigosa.
        pins_de_fabrica = {pin for _, pin, _ in OPERADORES_DEFAULT}
        for linha in conn.execute("SELECT id, pin_hash FROM operadores").fetchall():
            if any(conferir_pin(linha["pin_hash"], pin) for pin in pins_de_fabrica):
                conn.execute("UPDATE operadores SET pin_provisorio=1 WHERE id=?",
                             (linha["id"],))

    # Migration: integração com a estação de visão.
    #   dispenser_visao   -> numero da zona em visao_dispensers/config/zonas.json
    #   sku_visao         -> conteudo do QR/ArUco (ex: MED-001), chave alternativa
    #   unidades_por_caixa-> converte a contagem da visao (CAIXAS) para o estoque
    #                        do backend (UNIDADES). 1 = o dispenser guarda unidades
    #                        avulsas e cada "caixa" vista vale uma unidade.
    if "dispenser_visao" not in cols:
        conn.execute("ALTER TABLE medicamentos ADD COLUMN dispenser_visao INTEGER")
    if "sku_visao" not in cols:
        conn.execute("ALTER TABLE medicamentos ADD COLUMN sku_visao TEXT DEFAULT ''")
    if "unidades_por_caixa" not in cols:
        conn.execute("ALTER TABLE medicamentos ADD COLUMN unidades_por_caixa INTEGER DEFAULT 1")
    # aruco_visao: id do marcador ArUco impresso na mesma etiqueta do QR. Fica
    # aqui, e nao no arquivo local da estacao, para que o backend seja o unico
    # dono do registro "que medicamento vive em que dispenser" — a estacao le
    # isso dele em vez de manter uma copia que diverge na primeira troca.
    if "aruco_visao" not in cols:
        conn.execute("ALTER TABLE medicamentos ADD COLUMN aruco_visao INTEGER")

    # Migration: rastreabilidade completa do lote — fornecedor, nota fiscal, fabricação
    lote_cols = [r[1] for r in conn.execute("PRAGMA table_info(lotes)").fetchall()]
    if "fornecedor" not in lote_cols:
        conn.execute("ALTER TABLE lotes ADD COLUMN fornecedor TEXT DEFAULT ''")
    if "nota_fiscal" not in lote_cols:
        conn.execute("ALTER TABLE lotes ADD COLUMN nota_fiscal TEXT DEFAULT ''")
    if "data_fabricacao" not in lote_cols:
        conn.execute("ALTER TABLE lotes ADD COLUMN data_fabricacao TEXT DEFAULT ''")

    existing = conn.execute("SELECT COUNT(*) as c FROM medicamentos").fetchone()["c"]
    if existing == 0:
        for nome, qtd, cap in MEDICAMENTOS_DEFAULT:
            conn.execute(
                "INSERT OR IGNORE INTO medicamentos (nome, quantidade, capacidade, minimo) VALUES (?, ?, ?, 10)",
                (nome, qtd, cap),
            )

    cat_existing = conn.execute("SELECT COUNT(*) as c FROM catalogo").fetchone()["c"]
    if cat_existing == 0:
        for nome in CATALOGO_DEFAULT:
            conn.execute("INSERT OR IGNORE INTO catalogo (nome) VALUES (?)", (nome,))

    op_existing = conn.execute("SELECT COUNT(*) as c FROM operadores").fetchone()["c"]
    if op_existing == 0:
        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for nome, pin, perfil in OPERADORES_DEFAULT:
            # `pin_provisorio=1`: o PIN está escrito neste repositório. Nascer
            # marcado é o que faz a primeira entrada de cada operador exigir a
            # troca, em vez de depender de alguém lembrar.
            conn.execute(
                "INSERT INTO operadores (nome, pin_hash, perfil, ativo, data_criacao, "
                "pin_provisorio) VALUES (?,?,?,1,?,1)",
                (nome, gerar_pin_hash(pin), perfil, agora),
            )

    conn.commit()
    conn.close()


def get_medicamentos_list():
    conn = get_db()
    rows = conn.execute("SELECT nome FROM medicamentos ORDER BY id").fetchall()
    conn.close()
    return [r["nome"] for r in rows]


def get_clientes_list():
    conn = get_db()
    rows = conn.execute("SELECT nome FROM clientes WHERE ativo=1 ORDER BY nome").fetchall()
    conn.close()
    return [r["nome"] for r in rows]


# `init_db()` NAO roda aqui. Ver `preparar_banco`, junto de `iniciar_workers`.
# Com debug=True o Werkzeug sobe 2 processos (monitor do reloader + worker
# real) que executam este modulo inteiro desde o topo. app.debug so vira True
# depois que app.run() e chamado la embaixo, entao nao da pra checar isso
# aqui — o jeito certo e checar a env var que o Werkzeug seta so no processo
# filho (o worker de verdade). So esse processo deve abrir a porta serial,
# senao as duas threads brigam pelo mesmo dispositivo USB.
# No app desktop nao existe reloader nem essa env var — la quem inicia a
# serial e o desktop.py, uma unica vez.
# O reloader e desligado por padrao porque este processo POSSUI um dispositivo
# fisico. Com ele ligado, qualquer save em app.py mata e recria o processo que
# detem a COM do display: a porta as vezes nao e liberada a tempo, o worker novo
# nao consegue abrir, e o display fica OFFLINE sem nada no log explicando.
# Ligue com APSEN_RELOADER=1 durante desenvolvimento sem hardware conectado.
USAR_RELOADER = os.environ.get("APSEN_RELOADER", "0") == "1"

def preparar_banco(semear: bool = True) -> None:
    """Cria o schema e (se pedido) semeia a demonstracao. NAO roda no import.

    As duas chamadas moravam no topo do modulo, e isso significa que IMPORTAR
    `app.py` criava um banco no disco e o populava com dado fabricado —
    `waitress-serve app:app`, um linter, um `python -c "import app"`, qualquer
    coisa servia. E a regra que o central ja tem escrita para o `seed_demo.py`
    dele: um seed que roda sozinho transforma a primeira subida de uma
    instalacao DE VERDADE em dado inventado no banco de producao.

    O que NAO muda e o comportamento de quem opera: os tres entrypoints
    (`python app.py`, o waitress e o `desktop.py`) passam por `iniciar_workers`,
    e e de la que esta funcao e chamada. O `seed_demo_data` continua com a
    guarda dele — so popula com `lotes` vazia.
    """
    init_db()
    if semear:
        seed_demo_data()


def _expirar_lotes_no_boot() -> None:
    """A varredura de lotes vencidos, com transação e log próprios.

    Nunca levanta: este processo é o dono da porta serial do display, e uma
    falha de SQLite aqui derrubaria a tela que o operador está olhando por
    causa de uma manutenção de estoque. O `consumir_fefo` e o
    `verificar_estoque_ordem` varrem de novo, cada um na própria transação.
    """
    try:
        conn = get_db()
        try:
            vencidos = expirar_lotes_vencidos(conn)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:                       # noqa: BLE001
        print(f"[estoque] varredura de lotes vencidos falhou: {exc}")
        return

    if vencidos:
        print(f"[estoque] {len(vencidos)} lote(s) vencido(s) saíram do saldo "
              f"dispensável: " +
              ", ".join(f"{l['lote']} (venceu {l['validade']}, {l['quantidade']} un.)"
                        for l in vencidos))


def iniciar_workers():
    """Sobe as threads de fundo: a ponte serial com o display e o espelho do
    central. Chamada pelo bloco de execucao (`python app.py`) e pelo
    `desktop.py` — nunca no import.

    Importar este modulo nao pode abrir uma porta USB nem uma conexao de rede:
    a suite de testes o importa por caminho, e o `desktop.py` o importa para
    pegar `app`. Enquanto o start morava no topo do modulo, o desktop subia
    DUAS threads de serial disputando o mesmo dispositivo.

    Com reloader, so o processo filho (o worker de verdade) abre a serial —
    senao as duas instancias brigam pelo mesmo USB. Sem reloader existe um
    processo so, e a checagem da env var nunca seria verdadeira: dai a segunda
    condicao.
    """
    # Lote vence pela passagem do TEMPO, e o painel pode ter ficado desligado —
    # na bancada foram dois meses. Sem esta passada, a primeira ordem depois de
    # um fim de semana longo seria liberada contra um agregado que ainda conta
    # estoque que não pode sair.
    #
    # Aqui e não no `init_db`: aquele roda no IMPORT do módulo, antes de
    # `expirar_lotes_vencidos` existir. Este é o ponto em que o painel de fato
    # começa a servir, e é por onde passam os três entrypoints (`python
    # app.py`, o waitress e o `desktop.py`).
    preparar_banco()
    _expirar_lotes_no_boot()

    if (not USAR_RELOADER) or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        threading.Thread(target=serial_worker, daemon=True).start()
        if INTEGRACAO_ATIVA:
            threading.Thread(target=central_sync_worker, daemon=True).start()
        else:
            print("[central] PAINEL_CENTRAL=0 — espelho desligado, painel 100% local")


def gerar_numero_os():
    conn = get_db()
    row = conn.execute("SELECT MAX(id) as m FROM ordens").fetchone()
    conn.close()
    num = (row["m"] or 0) + 1
    return f"OS-{num:04d}"


def parse_itens(row):
    try:
        return json.loads(row["itens"])
    except (json.JSONDecodeError, TypeError):
        return []


def itens_resumo(row):
    itens = parse_itens(row)
    parts = [f"{it['med']} x{it['qtd']}" for it in itens]
    return ", ".join(parts) if parts else "--"


def itens_total_qtd(row):
    return sum(it.get("qtd", 0) for it in parse_itens(row))


# ============================================================
# Rastreabilidade de lote — consumo FEFO (First-Expire-First-Out)
# ============================================================
# Este bloco mora ACIMA de `consumir_fefo` de propósito: `seed_demo_data()`
# roda no IMPORT do módulo e chama `consumir_fefo`, que varre os lotes
# vencidos antes de escolher. Definido lá embaixo, o import morria com
# `NameError` — e o lugar certo dele é junto de quem depende da invariante.

# ============================================================
# Lotes — rastreabilidade de estoque (lote + validade, FEFO)
# ============================================================
#
# ── A invariante: `medicamentos.quantidade` conta saldo DISPENSÁVEL ──────────
#
# Lote bloqueado NÃO entra nesse número. Sem a regra escrita, bloquear um lote
# mexia só em `lotes.status` e o agregado seguia contando o saldo bloqueado —
# `verificar_estoque_ordem` liberava a ordem, `consumir_fefo` não achava lote
# 'Ativo' para abater, caía no ramo do resíduo e gravava "SEM LOTE REGISTRADO"
# debitando o agregado assim mesmo. Ou seja: o medicamento saía, sem
# genealogia, exatamente do lote que a tela dizia ter bloqueado.
#
# ── Por que não derivar `quantidade` de SUM(lotes WHERE status='Ativo') ──────
#
# Seria a forma de ter UM número só, e é o que este repositório faz em toda
# parte (ver CLAUDE.md, "A cópia do mapa no cnc_simulator não existe mais").
# Aqui ela não cabe, por duas razões concretas:
#
# 1. `medicamentos.quantidade` não é uma soma de lotes — é um número MEDIDO.
#    A estação de visão o reescreve (`_aplicar_visao_*`), o espelho do central
#    o reescreve (`_sync_dispensers_data`), a troca de medicamento do slot o
#    reescreve, e o cadastro permite corrigi-lo à mão. Nenhum desses caminhos
#    tem lote para escrever: derivar obrigaria cada um a inventar uma linha em
#    `lotes` — dado de rastreabilidade fabricado — ou a ter sua medição
#    silenciosamente descartada na leitura seguinte.
# 2. Estoque sem lote cadastrado existe e é legítimo (é o que o ramo
#    "SEM LOTE REGISTRADO" de `consumir_fefo` cobre). Derivado, todo
#    medicamento sem lote leria zero e `verificar_estoque_ordem` recusaria
#    toda ordem numa bancada fisicamente cheia.
#
# Então os dois números coexistem — e o que criou o bug não foi a coexistência,
# foi ela não ter invariante declarada nem um dono. O dono é
# `_mover_saldo_lote`: TODA transição que muda a dispensabilidade de um lote
# passa por ela, e ela move os dois números na mesma transação. Quem
# acrescentar um status novo de lote acrescenta aqui, não em mais uma rota.

# Status de lote cujo saldo CONTA no agregado do medicamento. 'Esgotado' fica
# de fora por ter quantidade 0 — incluí-lo não mudaria soma nenhuma, mas
# esconderia que a lista é sobre dispensabilidade, não sobre histórico.
LOTE_STATUS_DISPENSAVEL = "Ativo"


def _mover_saldo_lote(conn, lote_row, dispensavel: bool) -> None:
    """Põe (ou tira) o saldo de um lote no agregado do medicamento.

    `dispensavel=True` devolve o saldo ao agregado (desbloqueio),
    `False` o remove (bloqueio). Chamar com o lote JÁ no estado pedido é
    proibido pelos chamadores, e não por acaso: esta função soma e subtrai, não
    reconcilia — invocá-la duas vezes no mesmo sentido duplicaria o saldo.
    """
    saldo = lote_row["quantidade"] or 0
    if saldo <= 0:
        return
    if dispensavel:
        conn.execute(
            "UPDATE medicamentos SET quantidade = quantidade + ? WHERE id=?",
            (saldo, lote_row["medicamento_id"]),
        )
    else:
        conn.execute(
            "UPDATE medicamentos SET quantidade = MAX(0, quantidade - ?) WHERE id=?",
            (saldo, lote_row["medicamento_id"]),
        )


# Status de quem venceu. Ele existe para o saldo SAIR do agregado — e é por
# isso que é um status, e não um filtro nas consultas: `medicamentos.quantidade`
# é o número que `verificar_estoque_ordem` consulta para liberar uma ordem, e um
# filtro não o corrige.
LOTE_STATUS_VENCIDO = "Vencido"


def _hoje() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def expirar_lotes_vencidos(conn) -> list:
    """Tira do agregado o saldo de todo lote 'Ativo' cuja validade já passou.

    Devolve as linhas afetadas (antes da mudança), para quem chamar abrir
    desvio. **Idempotente**: a segunda passada não acha mais nenhum, porque o
    status deixou de ser 'Ativo' — e isso importa, porque `_mover_saldo_lote`
    soma e subtrai, não reconcilia.

    Um lote não vence por um EVENTO: vence pela passagem do tempo, e é a única
    transição de dispensabilidade que ninguém dispara. Sem esta varredura o
    agregado seguia contando o saldo vencido, e a cadeia inteira funcionava sem
    erro em lugar nenhum: `verificar_estoque_ordem` liberava a ordem →
    `consumir_fefo` achava o lote 'Ativo' → e o consumia PREFERENCIALMENTE, por
    construção, porque FEFO é "vence primeiro, sai primeiro" e um lote já
    vencido é o que vence primeiro de todos.

    Na bancada isso durou dois meses: seis lotes vencidos em 26/07, 286
    unidades ainda dispensáveis, e o único freio era manual — o alerta de
    `lotes_proximos_vencimento` no dashboard, esperando alguém lembrar de dar
    baixa.
    """
    vencidos = conn.execute(
        """SELECT * FROM lotes
           WHERE status=? AND quantidade>0
             AND validade IS NOT NULL AND validade != '' AND validade < ?""",
        (LOTE_STATUS_DISPENSAVEL, _hoje()),
    ).fetchall()

    for lote in vencidos:
        # A ordem é a de sempre: o dono do agregado primeiro, o status depois.
        # Invertida, uma exceção no meio deixaria o lote fora de 'Ativo' com o
        # saldo ainda contado — e a varredura seguinte não o acharia mais.
        _mover_saldo_lote(conn, lote, dispensavel=False)
        conn.execute("UPDATE lotes SET status=? WHERE id=?",
                     (LOTE_STATUS_VENCIDO, lote["id"]))
    return vencidos


def consumir_fefo(conn, medicamento_nome, qtd, ordem_id, numero_os):
    """Abate `qtd` unidades dos lotes ativos do medicamento (mais próximo
    de vencer primeiro) e registra a genealogia ordem -> lote em
    ordem_lotes_consumidos. Também mantém medicamentos.quantidade coerente
    com o que foi de fato consumido nesta ordem."""
    med = conn.execute(
        "SELECT id FROM medicamentos WHERE nome=?", (medicamento_nome,)
    ).fetchone()
    if not med:
        return
    medicamento_id = med["id"]
    restante = qtd
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # O que venceu sai do agregado ANTES da escolha, e a varredura é a metade
    # que faltava: sem ela o lote vencido não só era aceito — era PREFERIDO.
    # FEFO é "vence primeiro, sai primeiro", e um lote já vencido é, por
    # construção, o que vence primeiro de todos.
    expirar_lotes_vencidos(conn)

    # E o `WHERE` repete a condição em vez de confiar só na varredura acima.
    # Não é redundância: a varredura roda no começo desta transação, e uma
    # ordem que atravesse a virada do dia teria o lote vencendo entre as duas
    # linhas. Custa uma comparação de texto e fecha a janela.
    lotes = conn.execute(
        """SELECT * FROM lotes WHERE medicamento_id=? AND status=? AND quantidade>0
             AND (validade IS NULL OR validade='' OR validade >= ?)
           ORDER BY (validade IS NULL OR validade=''), validade ASC, id ASC""",
        (medicamento_id, LOTE_STATUS_DISPENSAVEL, _hoje()),
    ).fetchall()

    for lote in lotes:
        if restante <= 0:
            break
        usa = min(lote["quantidade"], restante)
        novo_saldo = lote["quantidade"] - usa
        conn.execute(
            "UPDATE lotes SET quantidade=?, status=? WHERE id=?",
            (novo_saldo, "Esgotado" if novo_saldo <= 0 else "Ativo", lote["id"]),
        )
        conn.execute(
            """INSERT INTO ordem_lotes_consumidos
               (ordem_id, numero_os, medicamento_id, medicamento_nome, lote_id, lote, quantidade, data)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ordem_id, numero_os, medicamento_id, medicamento_nome, lote["id"], lote["lote"], usa, agora),
        )
        restante -= usa

    if restante > 0:
        # Consumo sem lote cadastrado (estoque legado) — registra mesmo assim
        # para a genealogia não ficar incompleta.
        conn.execute(
            """INSERT INTO ordem_lotes_consumidos
               (ordem_id, numero_os, medicamento_id, medicamento_nome, lote_id, lote, quantidade, data)
               VALUES (?,?,?,?,NULL,?,?,?)""",
            (ordem_id, numero_os, medicamento_id, medicamento_nome, "SEM LOTE REGISTRADO", restante, agora),
        )
        # E agora vira PENDÊNCIA. Consumir sem lote válido sempre foi legítimo
        # (estoque anterior ao cadastro de lotes existe), mas num painel cuja
        # razão de existir é rastreabilidade ele não pode passar calado — e,
        # desde que o vencido deixou de ser escolhido, ele é também o sintoma
        # de "só restava lote vencido na prateleira".
        registrar_desvio(
            conn, numero_os or "", "Consumo sem lote válido",
            f"{medicamento_nome}: {restante} de {qtd} unidade(s) saíram sem lote "
            f"rastreável. Verifique se há lote vencido ou bloqueado no estoque.",
            "sistema",
        )

    conn.execute(
        "UPDATE medicamentos SET quantidade = MAX(0, quantidade - ?) WHERE id=?",
        (qtd, medicamento_id),
    )


def verificar_estoque_ordem(conn, ordem_row):
    """Mesma regra que o display aplica antes de liberar 'Iniciar': nenhum item
    pode ter quantidade pedida maior que o estoque atual do medicamento.
    Sem essa checagem no backend, o app web deixava iniciar uma ordem que o
    display corretamente bloqueava por falta de estoque."""
    # Uma função que se chama "verificar" e ESCREVE. É deliberado, e a
    # alternativa é pior: o agregado que ela consulta conta o saldo de lote
    # vencido até alguém varrer, e os dois chamadores (a rota web e o
    # `_set_status_by_numero_os`, que cobre a API e o display) teriam de
    # lembrar de varrer antes — é a forma exata do bug que o CLAUDE.md
    # registra em "todo caminho que contorna a função canônica herda os
    # efeitos dela". A varredura é idempotente e não faz nada no dia a dia.
    expirar_lotes_vencidos(conn)

    itens = parse_itens(ordem_row)
    faltas = []
    for it in itens:
        nome = it.get("med", "")
        qtd = it.get("qtd", 0)
        med = conn.execute("SELECT quantidade FROM medicamentos WHERE nome=?", (nome,)).fetchone()
        disponivel = med["quantidade"] if med else 0
        if disponivel < qtd:
            faltas.append(f"{nome}: precisa {qtd}, tem {disponivel}")
    if faltas:
        return False, "; ".join(faltas)
    return True, ""


def registrar_historico(conn, operador, perfil, acao, detalhes=""):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO historico (operador, perfil, acao, detalhes, timestamp) VALUES (?,?,?,?,?)",
        (operador or "---", perfil, acao, detalhes, agora),
    )


def registrar_desvio(conn, numero_os, tipo, descricao, operador, perfil=""):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO desvios (numero_os, tipo, descricao, operador, perfil, status, data_abertura)
           VALUES (?,?,?,?,?, 'Aberto', ?)""",
        (numero_os, tipo, descricao, operador, perfil, agora),
    )


# ============================================================
# Espelho do computador central
# ============================================================
# O central manda, o painel espelha. As ordens que a célula executa e o estoque
# que ela mede chegam aqui por GET e nunca voltam: nenhuma função deste bloco
# escreve no central (ver o docstring de `central_client`).
#
# O espelho mora na PRÓPRIA tabela `ordens`, marcado por `origem`, e não numa
# tabela paralela. A web, a API e o display já leem `ordens`; um espelho em
# tabela separada obrigaria cada uma dessas consultas a virar duas e a ser
# unida à mão — e a primeira que alguém esquecesse de duplicar mostraria meia
# planta sem erro nenhum no log.
#
# O preço de morar junto é que `origem` passa a ser regra, não enfeite: linha
# com origem='central' é SÓ-LEITURA, e o bloqueio vale nos três caminhos que
# escrevem status (web, API e serial).

ORIGEM_LOCAL = "local"
ORIGEM_CENTRAL = "central"

# Mensagem única da recusa — ela sai no flash da web, no JSON da API e na
# resposta serial ao display.
MSG_ORDEM_CENTRAL = "ordem do central"
# Recusa de SLOT tem mensagem própria: quem lesse "ordem do central" depois de
# tentar sincronizar dispensers iria procurar o problema numa ordem de
# expedição, que é o único lugar onde ele não está.
MSG_SLOT_CENTRAL = "slot medido pelo computador central"

# Toda ordem espelhada nasce com esta prioridade: o central não tem o conceito.
PRIORIDADE_ESPELHO = "Normal"

# A ordem REAL de prioridade, da mais urgente para a menos. É a fonte do
# ORDER BY de `/api/resumo` — a query que alimenta a lista de ordens ativas.
#
# Ela ordenava por `prioridade DESC`, e `prioridade` é TEXTO: com estes quatro
# valores o DESC alfabético produz Urgente → Normal → Baixa → Alta, ou seja,
# "Alta" caía em ÚLTIMO, atrás de "Baixa". A ordem errada chegava ao operador
# sem erro em lugar nenhum — a lista continuava com as mesmas ordens, só que
# na sequência errada, e nada no log distingue "ordenado errado" de "ordenado".
PRIORIDADES = ("Urgente", "Alta", "Normal", "Baixa")


def _sql_ordem_prioridade(coluna: str = "prioridade") -> str:
    """Fragmento `CASE` para um ORDER BY na ordem de `PRIORIDADES`.

    Prioridade que o banco não conheça vai para o FIM, não para o começo: um
    valor novo, ou digitado errado, não pode furar a fila de quem é urgente.
    Os valores são os da tupla acima, nunca entrada do usuário — o f-string
    monta SQL a partir de uma constante deste módulo.
    """
    whens = " ".join(f"WHEN '{p}' THEN {i}" for i, p in enumerate(PRIORIDADES))
    return f"CASE {coluna} {whens} ELSE {len(PRIORIDADES)} END"


# ── O vocabulário de status é FECHADO ─────────────────────────────────────────
#
# Toda tela do painel soma por igualdade de string — dashboard, /relatorio,
# /kpis, /api/resumo e a fila que o display recebe. Um status fora desta lista
# não dá erro em lugar nenhum: a ordem continua no banco, aparece na listagem
# geral e desaparece de todos os contadores. O painel passa a dizer que a
# planta está em dia.
#
# O central faz a mesma checagem no vocabulário DELE
# (`alterar_status_os`, em central-computer/main.py); esta é a metade de cá.
#
# A lista é DERIVADA de `STATUS_CENTRAL_PARA_PAINEL` em vez de escrita à mão:
# o sync do espelho grava exatamente os valores daquele mapa, então uma lista
# manual que ficasse para trás recusaria justamente o status que o espelho
# acabou de produzir. "Pausado" e o fallback de status desconhecido entram por
# fora porque não vêm do central — o primeiro é do fluxo local, o segundo é
# onde `traduzir_status` deposita um estado que o central passe a emitir.
STATUS_SO_LOCAIS = {"Pausado"}
STATUS_VALIDOS = (
    set(central_client.STATUS_CENTRAL_PARA_PAINEL.values())
    | {central_client.STATUS_DESCONHECIDO}
    | STATUS_SO_LOCAIS
)

# Mensagem única da recusa, como MSG_ORDEM_CENTRAL: ela sai no flash da web, no
# JSON da API e na resposta serial ao display.
MSG_STATUS_INVALIDO = "status invalido"


def status_valido(status) -> bool:
    return status in STATUS_VALIDOS


def ordem_e_do_central(row) -> bool:
    """`row` é uma linha de `ordens` (sqlite3.Row ou dict).

    Banco anterior à migração não tem a coluna; ausente = local, que é o
    comportamento de antes desta integração.
    """
    if row is None:
        return False
    try:
        origem = row["origem"]
    except (IndexError, KeyError):
        return False
    return (origem or ORIGEM_LOCAL) == ORIGEM_CENTRAL


# ── A escala de tempo do painel é LOCAL E INGÊNUA, e isso é convenção ────────
#
# Todo carimbo gravado aqui (`historico.timestamp`, `ordens.data_criacao`,
# `lotes.data_entrada`, …) é `"%Y-%m-%d %H:%M:%S"` em hora local, sem fuso. As
# telas o exibem cru, as consultas o ORDENAM como texto e o KPI de SLA o compara
# com outro `strftime` local — as três coisas só funcionam porque a escala é uma
# só. Trocar para UTC agora não seria uma correção: deslocaria toda data exibida
# em três horas e deixaria as linhas ANTIGAS incomparáveis com as novas, na mesma
# coluna e sem nada marcando qual é qual.
#
# O que importa, então, não é o fuso escolhido: é NÃO MISTURAR. E havia um lugar
# onde se misturava — `_normalizar_data`, logo abaixo.
def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalizar_data(bruto, padrao=""):
    """Carimbo do central (UTC) -> a escala do painel (local, ingênua).

    O 'T' já era tratado: o central serializa DATETIME como
    `2026-09-09T14:30:12`, e guardar o 'T' faria a ordem espelhada ordenar
    depois de qualquer ordem local do mesmo dia — texto, e 'T' > ' '.

    **O que faltava era o FUSO.** O central carimba em UTC (`database._ts` e
    `orchestrator._ts`), e o valor entrava direto numa coluna cujas outras
    linhas são hora local. Numa bancada no Brasil, toda OS espelhada nascia três
    horas no passado: ela ordenava antes de ordens locais mais velhas, e o KPI
    de SLA lhe creditava três horas a mais de duração. Nada disso dá erro — dá
    um relatório errado.

    Sem offset explícito, o valor é tratado como UTC, porque é o que o central
    manda. Com offset, ele manda. Valor irreconhecível cai no padrão, em vez de
    levantar dentro do laço do espelho.
    """
    texto = str(bruto or "").strip()
    if not texto:
        return padrao or _agora()

    try:
        momento = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        # Formato que não é ISO: mantém o comportamento anterior (troca o 'T'
        # e corta), porque um carimbo torto não pode parar a sincronização.
        return texto.replace("T", " ")[:19] or padrao or _agora()

    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _itens_do_central(detalhe: dict) -> list:
    """Itens do central no formato que o painel já usa: [{"med", "qtd"}]."""
    itens = []
    for it in (detalhe.get("itens") or []):
        nome = (it.get("medicamento") or "").strip()
        if not nome:
            continue
        try:
            qtd = int(it.get("quantidade_alvo") or 0)
        except (TypeError, ValueError):
            qtd = 0
        itens.append({"med": nome, "qtd": qtd})
    return itens


def _precisa_detalhe(row, status_painel: str, total_itens) -> bool:
    """Só ordem nova, com status mudado ou com contagem de itens mudada.

    Sem isso a thread faria uma requisição por ordem a cada CENTRAL_SYNC_S (5s)
    para reescrever exatamente o mesmo JSON.
    """
    if row is None:
        return True
    if row["status"] != status_painel:
        return True
    try:
        esperado = int(total_itens or 0)
    except (TypeError, ValueError):
        return True
    return len(parse_itens(row)) != esperado


def sincronizar_ordens_central(conn) -> dict:
    """Uma passada do espelho. Devolve um resumo contável, para o teste e o log.

    Três invariantes, e as três já custariam dado se caíssem:

    1. **UPSERT por `numero_os`**, que é o `os_id` do central. Rodar duas vezes
       com o mesmo dado não cria linha nova nem move estoque.
    2. **Linha com origem='local' nunca é tocada.** A tela de criação de ordem
       do painel continua criando ordens locais, que o central ignora.
    3. **Nada de `consumir_fefo`, `verificar_estoque_ordem` ou
       `processar_conclusao_ordem`.** Quem deu baixa no estoque foi a célula;
       repetir a baixa aqui dobraria o consumo no banco do painel. Por isso o
       status é gravado por UPDATE direto, e não por `_set_status_by_numero_os`.

    O UPDATE direto tem um preço, e ele é o motivo do `publish_order_action`
    lá embaixo: quem normalmente avisa o display de uma mudança de status é
    `_set_status_by_numero_os`, que este caminho evita de propósito. Sem o
    aviso explícito, a mudança morre no SQLite — `get_ordens` só monta ordem
    que o display ainda não conhece.
    """
    resumo = {"vistas": 0, "novas": 0, "atualizadas": 0,
              "ignoradas_locais": 0, "avisadas": 0}
    ordens = central_client.listar_ordens()
    if not ordens:
        return resumo

    for remota in ordens:
        os_id = (remota.get("os_id") or "").strip()
        if not os_id:
            continue
        resumo["vistas"] += 1

        row = conn.execute(
            "SELECT * FROM ordens WHERE numero_os=?", (os_id,)
        ).fetchone()
        if row is not None and not ordem_e_do_central(row):
            # Invariante 2: colisão de número com uma ordem criada aqui.
            resumo["ignoradas_locais"] += 1
            continue

        status = traduzir_status(remota.get("status"))
        destino = (remota.get("descricao") or "").strip() or (remota.get("categoria") or "").strip()
        criado = _normalizar_data(remota.get("criado_em"))
        atualizado = _normalizar_data(remota.get("concluida_em"), criado)

        if _precisa_detalhe(row, status, remota.get("total_itens")):
            detalhe = central_client.ordem_detalhe(os_id)
            if detalhe is None:
                # Detalhe indisponível não impede a ordem de aparecer: ela entra
                # com os itens que já se conhece (nenhum, se for nova) e o ciclo
                # seguinte os preenche. Sumir da lista seria pior — o operador
                # não veria a ordem que a célula está executando.
                itens = parse_itens(row) if row is not None else []
                print(f"[central] {os_id}: detalhe indisponível, itens mantidos")
            else:
                itens = _itens_do_central(detalhe)
        else:
            itens = parse_itens(row)

        if row is None:
            conn.execute(
                """INSERT INTO ordens
                   (numero_os, itens, destino, prioridade, status,
                    data_criacao, data_atualizacao, origem, os_id_central)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (os_id, json.dumps(itens), destino, PRIORIDADE_ESPELHO, status,
                 criado, atualizado, ORIGEM_CENTRAL, os_id),
            )
            resumo["novas"] += 1
        else:
            mudou_status = row["status"] != status
            # `AND origem=?` é redundante com a checagem acima e fica de
            # propósito: é a última linha de defesa da invariante 2.
            conn.execute(
                """UPDATE ordens SET itens=?, destino=?, status=?,
                       data_criacao=?, data_atualizacao=?, os_id_central=?
                   WHERE id=? AND origem=?""",
                (json.dumps(itens), destino, status, criado, atualizado, os_id,
                 row["id"], ORIGEM_CENTRAL),
            )
            resumo["atualizadas"] += 1
            if mudou_status:
                # Sem isto o display NUNCA fica sabendo. `get_ordens` só monta
                # ordem que ele ainda não conhece, e a fila que ele recebe traz
                # apenas 'Pendente' e 'Em Processo' — uma OS concluída ou
                # abortada some da lista em vez de mudar de estado. A ordem
                # ficaria congelada na tela do operador no status em que entrou.
                #
                # `source` não pode ser "display": esse valor existe para não
                # ecoar de volta a mudança que veio de lá, e a daqui não veio.
                publish_order_action(os_id, status, "central")
                resumo["avisadas"] += 1

    conn.commit()
    return resumo


def central_sync_worker():
    """Thread do espelho: laço com sleep, daemon, e que nunca derruba o processo.

    Mesmo formato do `serial_worker` e pelo mesmo motivo — este processo possui
    a porta serial do display e serve a tela do operador. Ordem que desapareceu
    do central NÃO é apagada daqui: o histórico local fica.
    """
    while True:
        conn = None
        try:
            conn = get_db()
            resumo = sincronizar_ordens_central(conn)
            if resumo["novas"] or resumo["atualizadas"]:
                print(f"[central] espelho: {resumo}")
            # A trava vai no MESMO ciclo: é uma leitura a mais a cada
            # CENTRAL_SYNC_S, e não uma thread a mais disputando o central.
            # `avisar_display=True`: é DAQUI que sai o push `trava` — só na
            # transição, comparando com o estado anterior.
            sincronizar_trava_central(avisar_display=True)
        except Exception as exc:
            print(f"[central] sincronizacao falhou: {exc}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        time.sleep(CENTRAL_SYNC_S)


# -- Trava do Triple Check ----------------------------------------------------
#
# Lida no mesmo ciclo do espelho (`central_sync_worker`) e guardada JUNTO com o
# estado anterior: é a comparação entre os dois que decide quando o display
# precisa ser avisado — o push `trava` da ponte serial —, na mesma forma do
# `ordem_status` em `sincronizar_ordens_central`. Estado igual não gera aviso.
#
# O dashboard web lê daqui, e não do central: a rota da tela não pode pagar a
# rede a cada carregamento, e o espelho já roda a cada CENTRAL_SYNC_S.
TRAVA_CHAVES = ("ativa", "os_id", "slot_id", "motivo")
_trava_espelho = {"atual": None, "anterior": None, "fonte_ok": False, "lida_em": 0.0}
_trava_lock = threading.Lock()

# Janela do cache de `get_trava` para o display — o mesmo número e o mesmo
# motivo de `DISPENSERS_CACHE_S`: o pedido roda DENTRO da ponte serial, o
# `serial_request` do firmware desiste em 800 ms e `CENTRAL_TIMEOUT_S` é 3 s.
# Central lento (de pé, mas sem responder) faria o display desistir antes de o
# backend ter a resposta. A leitura do espelho (a cada CENTRAL_SYNC_S) também
# renova o cache, então no caso comum o display é servido sem tocar na rede.
TRAVA_CACHE_S = 2.0

# O que o display recebe quando o backend nunca conseguiu ler a trava. "Sem
# trava" é o estado de boot da célula; o display não tem o que fazer com
# "desconhecido", e o push da primeira leitura de verdade corrige em segundos.
TRAVA_DESCONHECIDA = {"ativa": False, "os_id": "", "slot_id": None, "motivo": ""}


def _trava_normalizada(bruto) -> dict | None:
    """Só as quatro chaves que importam, com tipos previsíveis — ou None."""
    if not isinstance(bruto, dict):
        return None
    slot = bruto.get("slot_id")
    return {
        "ativa":   bool(bruto.get("ativa")),
        "os_id":   str(bruto.get("os_id") or ""),
        "slot_id": slot if isinstance(slot, int) and not isinstance(slot, bool) else None,
        "motivo":  str(bruto.get("motivo") or ""),
    }


def sincronizar_trava_central(avisar_display: bool = False) -> tuple:
    """Uma leitura da trava. Devolve `(estado, mudou)`.

    Central fora do ar devolve `(None, False)` e NÃO apaga o último estado
    conhecido — ele continua na tela, marcado como tal (`fonte_ok=False`).
    Apagar faria a faixa vermelha sumir justamente quando o central caiu, que é
    quando ninguém está olhando por ele; e "mudou" só vale entre duas leituras
    de verdade, senão uma queda de rede viraria push de "trava liberada".

    `avisar_display=True` (a thread do espelho) empurra `{"push": "trava"}`
    ao display quando — e só quando — o estado mudou. Push é uma linha serial,
    e linha serial se perde num reset do ESP32: por isso o display também
    PERGUNTA (`get_trava`), como já faz com as ordens espelhadas.
    """
    estado = _trava_normalizada(central_client.trava_estado())
    with _trava_lock:
        _trava_espelho["lida_em"] = time.time()
        if estado is None:
            _trava_espelho["fonte_ok"] = False
            return None, False
        anterior = _trava_espelho["atual"]
        mudou = anterior is None or any(anterior[c] != estado[c] for c in TRAVA_CHAVES)
        if mudou:
            _trava_espelho["anterior"] = anterior
            _trava_espelho["atual"] = estado
        _trava_espelho["fonte_ok"] = True
    if mudou and avisar_display:
        push_to_display({"push": "trava", **estado})
    return dict(estado), mudou


def _trava_para_display() -> dict:
    """Resposta de `get_trava`: o espelho, renovado se tiver mais de
    `TRAVA_CACHE_S`. Nunca `None` — ver `TRAVA_DESCONHECIDA`."""
    with _trava_lock:
        fresco = (time.time() - _trava_espelho["lida_em"]) < TRAVA_CACHE_S
    if not fresco:
        sincronizar_trava_central()
    with _trava_lock:
        atual = _trava_espelho["atual"]
    return dict(atual) if atual is not None else dict(TRAVA_DESCONHECIDA)


def trava_atual() -> dict | None:
    """O último estado conhecido, com `fonte_ok` dizendo se ele é fresco."""
    with _trava_lock:
        if _trava_espelho["atual"] is None:
            return None
        return {**_trava_espelho["atual"], "fonte_ok": _trava_espelho["fonte_ok"]}


# -- Slots espelhados ---------------------------------------------------------

# `minimo` de um slot que o central conhece e o cadastro local nao: o central
# nao tem o conceito de estoque minimo, e um numero escrito na chamada some do
# alcance de quem procura por que o alerta disparou.
DISPENSER_MINIMO_PADRAO = 10


# Janela do cache de `/dispensers/estado`. O número não é arbitrário: ele
# resolve um descasamento de relógios entre as duas pontas.
#
# `get_dispensers` do display cai em `_dispensers_data`, que faz HTTP no
# central. O `serial_request` do firmware desiste em **800 ms**;
# `CENTRAL_TIMEOUT_S` é **3 s**. Central lento — de pé, mas sem responder — faz
# o display desistir muito antes de o backend ter a resposta, e o painel de
# estoque congela sem que o log do firmware aponte para o central.
#
# O cache tira a rede do caminho serial no caso comum: o display pergunta a
# cada 5 s, e uma janela curta basta para que a resposta já esteja pronta.
# Curta de propósito — estoque de dispenser é o número que o operador confere
# contra a bancada, e atrasá-lo não é de graça.
DISPENSERS_CACHE_S = 2.0

_dispensers_cache = {"quando": 0.0, "dados": []}
_dispensers_cache_lock = threading.Lock()


def _dispensers_central(forcar: bool = False) -> list:
    """Slots como a célula os mede. Lista vazia = central fora, use o local.

    Cache de `DISPENSERS_CACHE_S`, e por dois motivos. O primeiro é o
    descasamento de timeouts descrito acima. O segundo é que um único pedido do
    display chega a passar duas vezes por aqui — `_dispensers_data` para
    montar a lista e `_slots_espelhados` para decidir o que é só-leitura —, e
    as duas respostas TÊM de concordar: metade da decisão tomada sobre um
    central que respondeu e a outra metade sobre um que caiu deixaria o painel
    sem poder ler e sem poder escrever o mesmo slot.
    """
    agora = time.time()
    with _dispensers_cache_lock:
        if not forcar and (agora - _dispensers_cache["quando"]) < DISPENSERS_CACHE_S:
            return _dispensers_cache["dados"]

    dados = central_client.dispensers_estado()

    with _dispensers_cache_lock:
        # Central fora do ar não apaga o que já se sabia por engano: a lista
        # vazia é gravada como qualquer outra resposta, e é ela que dispara o
        # fallback local. O que o cache evita é perguntar de novo em 2 s.
        _dispensers_cache["quando"] = time.time()
        _dispensers_cache["dados"] = dados
    return dados


def _slots_espelhados() -> set:
    """Ids dos slots cujo número vem do central — e que o display não escreve.

    Mesmo motivo pelo qual `_sync_dispensers_data` já ignora dispenser sob a
    câmera: quem manda no número é quem o mede. Central fora do ar devolve
    conjunto vazio, e a escrita local volta a valer junto com o fallback de
    leitura — as duas metades precisam concordar.
    """
    return {
        d.get("dispenser_id") for d in _dispensers_central()
        if d.get("dispenser_id") is not None
    }


# ============================================================
# Estação de visão — contagem física e validação de saída
# ============================================================
# A estação (projeto visao_dispensers) enxerga a prateleira por câmera e mede
# QUANTAS CAIXAS existem em cada dispenser. Aqui essa medida vira o estoque
# oficial: a visão é a fonte da verdade, não o cálculo teórico da ordem.
#
# Três conversões/decisões acontecem neste bloco:
#   1. CAIXA -> UNIDADE, via medicamentos.unidades_por_caixa;
#   2. queda de nível -> baixa FEFO, para não perder a genealogia do lote;
#   3. saída -> confronto com a OS em processo, que é a VALIDAÇÃO: o que saiu
#      da prateleira bate com o que a ordem mandou tirar?
#
# O que a visão NÃO faz é decidir sozinha em cima de evidência ruim. Leitura
# sem calibração, com confiança baixa ou com veredito crítico não mexe no
# estoque — vira registro e, quando for o caso, desvio. Estoque errado com
# aparência de certo é pior que estoque desatualizado.

VISAO_CONFIANCA_MINIMA = 0.50

# Vereditos da fusão (código + embalagem) que impedem a contagem de valer:
# se não se sabe QUAL produto está ali, contar quantos há não significa nada.
VISAO_VEREDITOS_BLOQUEIAM = {"ERRO_POSICAO", "DIVERGENCIA", "NAO_CADASTRADO"}

VISAO_OPERADOR = "Estacao de visao"


def _visao_resolver_medicamento(conn, leitura):
    """Descobre a qual linha de `medicamentos` a zona da câmera corresponde.

    Três chaves, da mais forte para a mais fraca. O mapeamento explícito por
    dispenser_visao é o único que sobrevive a renomear o medicamento, por isso
    vem primeiro; casar por nome é o último recurso e existe só para quem ainda
    não configurou o mapeamento na tela /visao.
    """
    dispenser = leitura.get("dispenser")
    if dispenser is not None:
        row = conn.execute(
            "SELECT * FROM medicamentos WHERE dispenser_visao=?", (dispenser,)
        ).fetchone()
        if row:
            return row

    sku = (leitura.get("sku") or "").strip()
    if sku:
        row = conn.execute(
            "SELECT * FROM medicamentos WHERE sku_visao=? AND sku_visao != ''", (sku,)
        ).fetchone()
        if row:
            return row

    nome = (leitura.get("medicamento") or "").strip()
    if nome:
        row = conn.execute(
            "SELECT * FROM medicamentos WHERE lower(nome)=lower(?)", (nome,)
        ).fetchone()
        if row:
            return row

    return None


def _visao_registrar_leitura(conn, leitura, med_row, estacao, acao, detalhe,
                             unidades=None, anterior=None, delta=None, numero_os=""):
    conn.execute(
        """INSERT INTO estoque_visao
           (momento, estacao, dispenser, medicamento_id, medicamento_nome, sku,
            caixas, unidades, unidades_anterior, delta, confianca, veredito,
            numero_os, acao, detalhe)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            leitura.get("momento") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            estacao,
            leitura.get("dispenser"),
            med_row["id"] if med_row else None,
            med_row["nome"] if med_row else (leitura.get("medicamento") or ""),
            leitura.get("sku") or "",
            leitura.get("caixas"),
            unidades,
            anterior,
            delta,
            leitura.get("confianca"),
            leitura.get("veredito") or "",
            numero_os,
            acao,
            detalhe,
        ),
    )


def _visao_ordens_em_processo(conn):
    return conn.execute(
        "SELECT * FROM ordens WHERE status='Em Processo' ORDER BY data_atualizacao DESC"
    ).fetchall()


def _visao_ja_saiu_na_os(conn, numero_os, medicamento_id):
    """Quantas unidades deste medicamento a visão já debitou nesta OS.

    Sem isso a validação de excesso só olharia a última retirada e deixaria
    passar o caso real: o operador tira de pouco em pouco e o total estoura a
    quantidade pedida.
    """
    row = conn.execute(
        """SELECT COALESCE(SUM(-delta), 0) AS s FROM estoque_visao
           WHERE numero_os=? AND medicamento_id=? AND acao='saida'""",
        (numero_os, medicamento_id),
    ).fetchone()
    return int(row["s"] or 0)


def _visao_validar_saida(conn, med_row, unidades_saida):
    """Confronta a saída medida com as ordens em processo.

    Devolve (numero_os, ordem_row, lista_de_desvios). A ordem só é atribuída
    quando ela realmente pede este medicamento — atribuir por proximidade de
    horário criaria genealogia falsa, que em auditoria é pior que genealogia
    ausente.
    """
    desvios = []
    ordens = _visao_ordens_em_processo(conn)

    if not ordens:
        desvios.append((
            "Saída sem ordem",
            f"Saíram {unidades_saida} un de '{med_row['nome']}' do dispenser sem "
            f"nenhuma ordem em processo. Retirada não autorizada ou ordem não iniciada.",
        ))
        return "", None, desvios

    for ordem in ordens:
        for it in parse_itens(ordem):
            if (it.get("med") or "").strip().lower() != med_row["nome"].strip().lower():
                continue

            numero_os = ordem["numero_os"]
            pedido = int(it.get("qtd", 0))
            ja_saiu = _visao_ja_saiu_na_os(conn, numero_os, med_row["id"])
            total = ja_saiu + unidades_saida

            if total > pedido:
                desvios.append((
                    "Saída acima do previsto",
                    f"{med_row['nome']}: a ordem {numero_os} pede {pedido} un e já "
                    f"saíram {total} un da prateleira ({ja_saiu} antes + "
                    f"{unidades_saida} agora). Excesso de {total - pedido} un.",
                ))
            return numero_os, ordem, desvios

    nomes = ", ".join(o["numero_os"] for o in ordens)
    desvios.append((
        "Saída não prevista",
        f"Saíram {unidades_saida} un de '{med_row['nome']}', que não consta em "
        f"nenhuma ordem em processo ({nomes}).",
    ))
    return "", None, desvios


def processar_leitura_visao(conn, leitura, estacao=""):
    """Aplica UMA leitura da estação de visão. Devolve o dicionário de resposta.

    Não faz commit: quem chama decide a transação, para um lote de leituras
    entrar inteiro ou não entrar.
    """
    med_row = _visao_resolver_medicamento(conn, leitura)
    if med_row is None:
        detalhe = (f"dispenser {leitura.get('dispenser')} / sku "
                   f"'{leitura.get('sku') or '-'}' não está mapeado para nenhum "
                   f"medicamento (configure em /visao)")
        _visao_registrar_leitura(conn, leitura, None, estacao, "erro", detalhe)
        return {"dispenser": leitura.get("dispenser"), "acao": "erro", "detalhe": detalhe}

    def responder(acao, detalhe, **extra):
        return {"dispenser": leitura.get("dispenser"), "medicamento": med_row["nome"],
                "acao": acao, "detalhe": detalhe, **extra}

    # ---------- identidade errada: vale por si só ---------- #
    # Vem ANTES das checagens de contagem de propósito. "O medicamento aqui não é
    # o que deveria estar" é um achado completo e acionável mesmo sem calibração
    # de pilha — quem confere o produto não precisa saber contar caixas para
    # saber que a caixa é a errada. Com esta checagem depois do teste de
    # `caixas`, uma troca de medicamento passava em SILÊNCIO em toda estação
    # ainda não calibrada, que é justamente quando ela é mais provável.
    veredito = (leitura.get("veredito") or "").upper()
    if veredito in VISAO_VEREDITOS_BLOQUEIAM:
        detalhe = (f"veredito {veredito} no dispenser — o produto na prateleira "
                   f"não confere com o esperado; contagem não aplicada ao estoque")
        _visao_registrar_leitura(conn, leitura, med_row, estacao, "bloqueado", detalhe)
        registrar_desvio(conn, "", f"Visão: {veredito}",
                         f"Dispenser {leitura.get('dispenser')} ({med_row['nome']}): "
                         f"{leitura.get('detalhe') or detalhe}", VISAO_OPERADOR)
        return responder("bloqueado", detalhe)

    # ---------- de onde sai a contagem ---------- #
    # Duas vias, e a ordem importa. A ALTURA DA PILHA é a boa: enxerga caixa sem
    # etiqueta visível e não depende de o código estar virado para a câmera. Mas
    # exige calibração com foto da prateleira vazia.
    #
    # Quando ela não existe, cai para contar as ETIQUETAS identificadas na zona.
    # Não precisa de calibração nenhuma, e responde exatamente a pergunta
    # "tem medicamento aqui ou está vazio?" — 3 etiquetas, 3 caixas; nenhuma,
    # dispenser vazio.
    #
    # O risco desta segunda via está registrado junto do movimento: etiqueta
    # virada, reflexo ou caixa atrás da outra some da contagem, e o estoque cai
    # sem nada ter saído. Por isso ela é reserva, não preferência.
    caixas = leitura.get("caixas")
    confianca = float(leitura.get("confianca") or 0.0)
    fonte_contagem = "altura da pilha"

    if caixas is None:
        vistas = leitura.get("unidades_vistas")
        if vistas is None:
            detalhe = "sem contagem: nem calibração de pilha, nem etiqueta lida"
            _visao_registrar_leitura(conn, leitura, med_row, estacao, "ignorado", detalhe)
            return responder("ignorado", detalhe)
        caixas = int(vistas)
        fonte_contagem = "etiquetas lidas"
        # A confiança da pilha não se aplica aqui: quem responde por este número
        # é a leitura do código, que já passou pelas travas do detector (N
        # confirmações e memória de zona). Sem isto a leitura por etiqueta seria
        # sempre descartada pelo piso de confiança logo abaixo.
        confianca = max(confianca, 0.80)

    elif confianca < VISAO_CONFIANCA_MINIMA:
        detalhe = (f"confiança {confianca:.2f} abaixo do mínimo "
                   f"{VISAO_CONFIANCA_MINIMA:.2f}")
        _visao_registrar_leitura(conn, leitura, med_row, estacao, "ignorado", detalhe)
        return responder("ignorado", detalhe)

    # ---------- caixa -> unidade ---------- #
    por_caixa = med_row["unidades_por_caixa"] or 1
    unidades = max(0, int(caixas) * int(por_caixa))
    capacidade = med_row["capacidade"] or 0
    if capacidade and unidades > capacidade:
        unidades = capacidade

    anterior = int(med_row["quantidade"] or 0)
    delta = unidades - anterior

    if delta == 0:
        detalhe = f"nível medido igual ao estoque atual [{fonte_contagem}]"
        _visao_registrar_leitura(conn, leitura, med_row, estacao, "sem_mudanca",
                                 detalhe, unidades=unidades, anterior=anterior, delta=0)
        return responder("sem_mudanca", detalhe, unidades=unidades)

    # ---------- reposição ---------- #
    if delta > 0:
        detalhe = (f"reposição detectada: {anterior} -> {unidades} un "
                   f"(+{delta}), {caixas} caixa(s) x {por_caixa} un "
                   f"[{fonte_contagem}]")
        conn.execute("UPDATE medicamentos SET quantidade=? WHERE id=?",
                     (unidades, med_row["id"]))
        _visao_registrar_leitura(conn, leitura, med_row, estacao, "reposicao", detalhe,
                                 unidades=unidades, anterior=anterior, delta=delta)
        registrar_historico(conn, VISAO_OPERADOR, "Sistema", "Reposição (visão)",
                            f"{med_row['nome']}: {anterior} -> {unidades} un")
        return responder("reposicao", detalhe, unidades=unidades, delta=delta)

    # ---------- saída ---------- #
    saida = -delta
    numero_os, ordem, desvios = _visao_validar_saida(conn, med_row, saida)

    for tipo, descricao in desvios:
        registrar_desvio(conn, numero_os, tipo, descricao, VISAO_OPERADOR)

    # FEFO mantém a genealogia ordem -> lote; sem ordem atribuída a baixa ainda
    # precisa sair do lote certo, então passa ordem_id nulo e OS vazia.
    consumir_fefo(conn, med_row["nome"], saida,
                  ordem["id"] if ordem else None, numero_os)

    # A visão é a fonte da verdade: o valor final é o MEDIDO, não o resultado
    # aritmético do FEFO. Coincidem quando há lote suficiente; quando não há,
    # esta linha garante que o estoque siga espelhando a prateleira.
    conn.execute("UPDATE medicamentos SET quantidade=? WHERE id=?",
                 (unidades, med_row["id"]))

    detalhe = (f"saída de {saida} un ({anterior} -> {unidades}), "
               f"{caixas} caixa(s) x {por_caixa} un [{fonte_contagem}]"
               + (f", ordem {numero_os}" if numero_os else ", sem ordem atribuída"))
    _visao_registrar_leitura(conn, leitura, med_row, estacao, "saida", detalhe,
                             unidades=unidades, anterior=anterior, delta=delta,
                             numero_os=numero_os)
    registrar_historico(conn, VISAO_OPERADOR, "Sistema", "Saída conferida (visão)",
                        f"{med_row['nome']}: -{saida} un"
                        + (f" [{numero_os}]" if numero_os else " [sem ordem]"))

    return responder("saida", detalhe, unidades=unidades, delta=delta,
                     numero_os=numero_os,
                     desvios=[t for t, _ in desvios])


def processar_conclusao_ordem(conn, ordem_row):
    """Dispara a baixa FEFO para todos os itens de uma ordem que acabou de
    ser marcada como Concluído.

    Item cujo dispenser está sob a câmera NÃO é debitado aqui: a visão já deu
    baixa do que de fato saiu da prateleira, no momento em que saiu. Debitar de
    novo na conclusão descontaria duas vezes o mesmo medicamento. O que sobra
    para a conclusão fazer nesses itens é a outra metade da validação — apontar
    quando saiu MENOS do que a ordem mandava.
    """
    itens = parse_itens(ordem_row)
    for it in itens:
        nome = it.get("med", "")
        qtd = int(it.get("qtd", 0))

        med = conn.execute(
            "SELECT id, dispenser_visao FROM medicamentos WHERE nome=?", (nome,)
        ).fetchone()

        if med and med["dispenser_visao"] is not None:
            ja_saiu = _visao_ja_saiu_na_os(conn, ordem_row["numero_os"], med["id"])
            if ja_saiu < qtd:
                registrar_desvio(
                    conn, ordem_row["numero_os"], "Saída abaixo do previsto",
                    f"{nome}: a ordem pede {qtd} un mas a câmera viu sair apenas "
                    f"{ja_saiu} un da prateleira (faltam {qtd - ja_saiu} un). "
                    f"Ordem concluída sem a retirada completa.",
                    VISAO_OPERADOR,
                )
            continue

        consumir_fefo(conn, nome, qtd, ordem_row["id"], ordem_row["numero_os"])


# ============================================================
# Helpers de dados compartilhados entre as rotas /api/* (uso manual/debug)
# e o serial_worker (canal real de producao com o display ESP32).
# ============================================================
# Teto de ordens enviadas ao display. O firmware guarda MAX_ORDENS 5 e o
# central limita a fila a MAX_FILA_OS 5 — o teto casa por construcao, e mandar
# mais seria mandar o que o display descarta na chegada.
MAX_ORDENS_DISPLAY = 5


def _ordens_pendentes_data(conn):
    """Fila do display: o que ainda vai rodar E o que esta rodando agora.

    So 'Pendente' escondia justamente a ordem que a celula esta executando —
    ela vira 'Em Processo' no primeiro evento e sumia da tela do operador.
    """
    rows = conn.execute(
        "SELECT * FROM ordens WHERE status IN ('Pendente','Em Processo') "
        "ORDER BY data_criacao DESC LIMIT ?",
        (MAX_ORDENS_DISPLAY,),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["itens_lista"] = parse_itens(r)
        d["medicamento"] = itens_resumo(r)
        d["quantidade"] = itens_total_qtd(r)
        result.append(d)
    return result


def _catalogo_data(conn):
    rows = conn.execute("SELECT nome FROM catalogo ORDER BY nome").fetchall()
    return [r["nome"] for r in rows]


def _operadores_data(conn):
    """Lista de operadores ativos para o display — SEM credencial nenhuma.

    Ela servia a lista com o PIN de todo mundo, e o firmware ainda gravava essa
    lista no cartao SD. O display precisa dos nomes (cabecalho, historico,
    linha da ordem); quem confere o PIN e o backend, por `validar_operador`.
    """
    ops = conn.execute(
        "SELECT nome, perfil FROM operadores WHERE ativo=1 ORDER BY id"
    ).fetchall()
    return [dict(o) for o in ops]


def _validar_pin_data(conn, pin: str) -> dict:
    """Confere um PIN contra os operadores ativos e diz de quem ele e.

    O display pergunta com o PIN, e nao com nome + PIN, porque a tela dele e um
    teclado numerico sem campo de nome — o PIN ja identifica o operador. Manter
    esse contrato e o que deixa a mudanca invisivel para quem opera.

    Consequencia que vale registrar: dois operadores com o MESMO PIN passam a
    ser indistinguiveis aqui, e o primeiro por id ganha. Era assim antes
    tambem — o firmware varria a lista e parava no primeiro `strcmp` igual —,
    so que agora a comparacao e por hash e nao da mais para casar por igualdade
    de PIN no SQL.
    """
    pin = (pin or "").strip()
    if not pin:
        return {"ok": False}
    for op in conn.execute(
        "SELECT nome, perfil, pin_hash, pin_provisorio FROM operadores "
        "WHERE ativo=1 ORDER BY id"
    ).fetchall():
        if conferir_pin(op["pin_hash"], pin):
            # `pin_provisorio` vai junto, e o display NÃO é bloqueado por ele:
            # a troca acontece na web, onde há teclado. Bloquear aqui deixaria
            # a bancada sem operador até alguém achar um computador — e o PIN
            # de fábrica continua sendo o de fábrica nos dois lugares, então o
            # que falta ao display é AVISAR, não impedir.
            return {"ok": True, "nome": op["nome"], "perfil": op["perfil"],
                    "pin_provisorio": bool(op["pin_provisorio"])}
    return {"ok": False}


def _liberar_trava_display(conn, nome: str, pin: str) -> tuple:
    """`cmd: liberar_trava` do display — nome + PIN, conferidos AQUI.

    Devolve `(ok, mensagem)`; a mensagem vai para a tela do display e nunca
    contem o PIN. Tres portas, nesta ordem: o operador existe e esta ativo, o
    PIN confere (hash, ~300 ms de proposito), e o PERFIL dele libera trava
    (`PERMISSOES[perfil]["trava_liberar"]`) — um Operador com o PIN certo
    continua nao liberando. So entao a escrita sai para o central, por
    `central_comandos`, em nome de quem digitou.

    Custo total: hash + POST ao central (CENTRAL_TIMEOUT_S). E por isso que o
    firmware espera 5 s neste pedido, e nao os 800 ms de sempre.
    """
    nome = (nome or "").strip()
    pin = (pin or "").strip()
    if not nome or not pin:
        return False, "informe nome e PIN"
    op = conn.execute(
        "SELECT nome, perfil, pin_hash FROM operadores WHERE nome=? AND ativo=1", (nome,)
    ).fetchone()
    if op is None or not conferir_pin(op["pin_hash"], pin):
        return False, "nome ou PIN incorreto"
    if not PERMISSOES.get(op["perfil"], {}).get("trava_liberar", False):
        return False, f"perfil {op['perfil']} nao libera trava (precisa Supervisor ou Admin)"

    ok, texto = central_comandos.liberar_trava(em_nome_de=op["nome"])
    registrar_historico(conn, op["nome"], op["perfil"],
                        "Liberar trava (display)" if ok else "Liberar trava recusada (display)",
                        texto)
    conn.commit()
    if ok:
        # O espelho (e a web) ficam sabendo agora, nao no proximo ciclo.
        sincronizar_trava_central()
    return ok, texto


def _lote_ativo(conn, medicamento_id):
    if medicamento_id is None:
        return None
    return conn.execute(
        """SELECT lote, validade FROM lotes
           WHERE medicamento_id=? AND status='Ativo' AND quantidade>0
           ORDER BY (validade IS NULL OR validade=''), validade ASC LIMIT 1""",
        (medicamento_id,),
    ).fetchone()


def _dispensers_data_local(conn):
    """O arranjo de antes da integracao: uma linha de `medicamentos` por slot,
    numerado por POSICAO na lista. Ele batia com os 8 slots da celula por
    coincidencia, nao por construcao — e por isso deixou de ser o caminho
    principal. Sobrevive como fallback.

    Inativo entra na lista, pelo motivo registrado em `_slot_para_id`: a
    posicao E o numero do slot, e tirar uma linha desloca todas as seguintes."""
    rows = conn.execute("SELECT * FROM medicamentos ORDER BY id").fetchall()
    dispensers = []
    for i, r in enumerate(rows):
        lote = _lote_ativo(conn, r["id"])
        dispensers.append({
            "slot": i + 1,
            "nome": r["nome"],
            "quantidade": r["quantidade"],
            "capacidade": r["capacidade"],
            "minimo": r["minimo"] if r["minimo"] is not None else DISPENSER_MINIMO_PADRAO,
            "lote": lote["lote"] if lote else "",
            "validade": lote["validade"] if lote else "",
        })
    return dispensers


def _dispensers_data(conn):
    """Estoque dos slots como o display o recebe.

    O numero vem do central, que e quem MEDE a celula: `dispenser_id` vira o
    slot, e nao a posicao da linha na tabela local. `minimo`, lote e validade
    continuam saindo do cadastro local, casados por nome do medicamento — o
    central nao tem nenhum dos tres.

    Central fora do ar cai no arranjo local. Tela em branco por rede caida e
    pior que estoque um pouco velho, e o operador precisa saber qual dos dois
    esta vendo: por isso o fallback aparece no log.
    """
    slots = _dispensers_central()
    if not slots:
        # "desligado" e "indisponivel" chegam aqui do mesmo jeito — lista vazia
        # — e sao coisas diferentes para quem le o log. Confundi-las mandaria
        # alguem investigar a rede por causa de uma variavel de ambiente.
        if INTEGRACAO_ATIVA:
            print("[central] dispensers indisponiveis — usando o cadastro local")
        return _dispensers_data_local(conn)

    locais = {
        r["nome"]: r
        for r in conn.execute("SELECT * FROM medicamentos").fetchall()
    }

    dispensers = []
    for d in sorted(slots, key=lambda x: x.get("dispenser_id") or 0):
        nome = (d.get("medicamento") or "").strip()
        local = locais.get(nome)
        lote = _lote_ativo(conn, local["id"]) if local else None
        minimo = local["minimo"] if local is not None and local["minimo"] is not None else None
        dispensers.append({
            "slot": d.get("dispenser_id"),
            "nome": nome,
            "quantidade": d.get("quantidade_atual") or 0,
            "capacidade": d.get("capacidade") or 0,
            "minimo": minimo if minimo is not None else DISPENSER_MINIMO_PADRAO,
            "lote": lote["lote"] if lote else "",
            "validade": lote["validade"] if lote else "",
        })
    return dispensers


def _set_status_by_numero_os(conn, numero_os, status):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute("SELECT * FROM ordens WHERE numero_os=?", (numero_os,)).fetchone()
    if not row:
        return False, "ordem nao encontrada"

    # O vocabulario e fechado aqui tambem, e pelo mesmo motivo da rota web: a
    # API e o `cmd: set_status` do display mandam o status como texto livre, e
    # um valor fora da lista some de todos os contadores sem erro nenhum.
    # Bloquear AQUI cobre os dois chamadores de uma vez.
    if not status_valido(status):
        return False, MSG_STATUS_INVALIDO

    # Espelho de mao unica: o painel nao decide o status de uma ordem que a
    # celula executa. Bloquear AQUI cobre de uma vez os dois chamadores desta
    # funcao — o `PUT /api/ordens/<id>/status` e o cmd `set_status` do display —
    # e os dois ja devolvem `erro`/`msg` com a mensagem que voltar daqui.
    if ordem_e_do_central(row):
        return False, MSG_ORDEM_CENTRAL

    if status == "Em Processo" and row["status"] in ("Pendente", "Pausado"):
        ok, erro = verificar_estoque_ordem(conn, row)
        if not ok:
            return False, erro

    conn.execute(
        "UPDATE ordens SET status=?, data_atualizacao=? WHERE id=?",
        (status, agora, row["id"]),
    )
    if status == "Concluido" and row["status"] != "Concluido":
        processar_conclusao_ordem(conn, row)
    conn.commit()
    return True, None


def _slot_para_id(conn):
    """slot (posição na lista que o display recebe) -> id real da linha.

    _dispensers_data numera os slots por POSIÇÃO (i+1), não pelo id. Enquanto
    ninguém apaga um medicamento os dois coincidem e o UPDATE ... WHERE id=slot
    funciona por acaso; depois da primeira exclusão ele escreve na linha errada.

    **A consulta NÃO filtra `ativo=1`, e isso é a regra, não esquecimento.** A
    posição é o que dá o número do slot: excluir uma linha renumera a bancada
    inteira daí para frente, e `excluir_medicamento` passou a desativar em vez
    de apagar justamente para que a linha fique onde está. Filtrar o inativo
    aqui desfaria a correção pelo outro lado — o mesmo deslocamento silencioso,
    só que produzido por um WHERE em vez de um DELETE. O mesmo vale para
    `_dispensers_data_local`.
    """
    rows = conn.execute("SELECT id FROM medicamentos ORDER BY id").fetchall()
    return {i + 1: r["id"] for i, r in enumerate(rows)}


def _sync_dispensers_data(conn, itens):
    """Recebe o estoque que o display calculou. Devolve (ok, erro).

    Dois donos recusam o valor teórico do display, pelo mesmo motivo: quem
    manda no número é quem o mede. A estação de visão, para o dispenser sob a
    câmera; o computador central, para todo slot que ele espelha.
    """
    # O corpo chega de fora (`PUT /api/dispensers/sync` e o `cmd` do display) e
    # nada o conferia: `None` derruba o `for` com TypeError, um dicionário solto
    # itera as CHAVES e `i.get` estoura, e um item sem `quantidade` chegava ao
    # `UPDATE` com KeyError. Todos viram 500 na rota que ESCREVE estoque — e é
    # a rota pela qual o display reporta o que contou.
    #
    # Item torto é IGNORADO, não recusa o lote inteiro: quem manda aqui é uma
    # ponte serial, e derrubar dez slots bons por causa de um campo faltando num
    # deles deixaria o estoque do display e o do painel divergindo, sem ninguém
    # saber qual está certo.
    if not isinstance(itens, list):
        return False, "corpo deve ser uma lista de {slot, quantidade}"

    def _inteiro(v):
        # `bool` é subclasse de `int` em Python: `True` passaria por slot 1.
        return isinstance(v, int) and not isinstance(v, bool)

    validos = [
        {"slot": i["slot"], "quantidade": i["quantidade"]}
        for i in itens
        if isinstance(i, dict) and _inteiro(i.get("slot"))
        and _inteiro(i.get("quantidade")) and i["quantidade"] >= 0
    ]

    espelhados = _slots_espelhados()
    recusados = [i["slot"] for i in validos if i["slot"] in espelhados]
    if recusados:
        return False, MSG_SLOT_CENTRAL

    mapa = _slot_para_id(conn)
    for item in validos:
        med_id = mapa.get(item["slot"])
        if med_id is None:
            continue
        row = conn.execute(
            "SELECT dispenser_visao FROM medicamentos WHERE id=?", (med_id,)
        ).fetchone()
        if row and row["dispenser_visao"] is not None:
            continue
        conn.execute(
            "UPDATE medicamentos SET quantidade=? WHERE id=?",
            (item["quantidade"], med_id),
        )
    conn.commit()
    return True, None


def _set_dispenser_med_data(conn, slot, nome):
    """Troca o medicamento de um slot. Devolve (ok, erro).

    Slot espelhado recusa: o medicamento que está ali é o que a célula
    carregou, e renomeá-lo aqui só faria o painel discordar da bancada.
    """
    if slot in _slots_espelhados():
        return False, MSG_SLOT_CENTRAL
    med_id = _slot_para_id(conn).get(slot)
    if med_id is None:
        return False, "slot inexistente"
    conn.execute("UPDATE medicamentos SET nome=? WHERE id=?", (nome, med_id))
    conn.commit()
    return True, None


def lotes_proximos_vencimento(conn, dias=30):
    limite = (datetime.now() + timedelta(days=dias)).strftime("%Y-%m-%d")
    return conn.execute(
        # 'Vencido' entra JUNTO com 'Ativo', e não é detalhe: desde que a
        # varredura passou a tirar o vencido de circulação, filtrar só por
        # 'Ativo' faria o lote SUMIR desta lista no dia seguinte ao vencimento
        # — exatamente quando ele mais precisa de alguém. O saldo já não conta
        # no agregado; o que falta é a baixa, e ela é manual.
        """SELECT lotes.*, medicamentos.nome as medicamento_nome
           FROM lotes JOIN medicamentos ON medicamentos.id = lotes.medicamento_id
           WHERE lotes.status IN (?, ?) AND lotes.quantidade>0
             AND lotes.validade IS NOT NULL AND lotes.validade != ''
             AND lotes.validade <= ?
           ORDER BY lotes.validade ASC""",
        (LOTE_STATUS_DISPENSAVEL, LOTE_STATUS_VENCIDO, limite),
    ).fetchall()


def seed_demo_data():
    """Popula lotes/ordens/histórico/desvios com dados fictícios (mas plausíveis)
    na primeira execução, para dispensadores, rastreabilidade, KPIs e desvios já
    nascerem com algo pra mostrar. Só roda se a tabela `lotes` estiver vazia —
    não mexe em nada depois que o sistema já está em uso real."""
    conn = get_db()

    # Clientes: seed independente (roda mesmo se o resto ja foi seedado antes).
    if conn.execute("SELECT COUNT(*) as c FROM clientes").fetchone()["c"] == 0:
        agora_cli = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for nome in ["Farmácia Central", "Filial Zona Sul", "Hospital Municipal", "Filial Zona Norte"]:
            conn.execute(
                "INSERT OR IGNORE INTO clientes (nome, ativo, data_cadastro) VALUES (?,1,?)",
                (nome, agora_cli),
            )
        conn.commit()

    if conn.execute("SELECT COUNT(*) as c FROM lotes").fetchone()["c"] > 0:
        conn.close()
        return

    meds = conn.execute("SELECT id, nome FROM medicamentos ORDER BY id").fetchall()
    if not meds:
        conn.close()
        return

    hoje = datetime.now()

    # --- Lotes: um vencendo em breve (dispara alerta) + um tranquilo, por medicamento ---
    for m in meds:
        conn.execute("UPDATE medicamentos SET capacidade=150, quantidade=0 WHERE id=?", (m["id"],))
        for i, (dias_validade, qtd) in enumerate([(25, 60), (180, 80)]):
            validade = (hoje + timedelta(days=dias_validade)).strftime("%Y-%m-%d")
            lote_num = f"L{hoje.year}{m['id']:02d}{i}"
            conn.execute(
                """INSERT INTO lotes (medicamento_id, lote, validade, quantidade, quantidade_inicial, status, data_entrada)
                   VALUES (?,?,?,?,?, 'Ativo', ?)""",
                (m["id"], lote_num, validade, qtd, qtd, (hoje - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.execute("UPDATE medicamentos SET quantidade = quantidade + ? WHERE id=?", (qtd, m["id"]))

    # --- Ordens concluídas nos últimos 14 dias, com início/fim reais no audit
    #     log (alimenta os KPIs) e baixa FEFO (alimenta a rastreabilidade) ---
    operadores_demo = [r["nome"] for r in conn.execute(
        "SELECT nome FROM operadores WHERE ativo=1 ORDER BY id LIMIT 3"
    ).fetchall()] or ["Administrador"]
    destinos = ["Farmácia Central", "Filial Zona Sul", "Hospital Municipal", "Filial Zona Norte"]

    for i in range(10):
        dias_atras = 13 - i
        numero_os = f"OS-DEMO{i + 1:02d}"
        med = meds[i % len(meds)]
        qtd_consumo = 5 + (i % 4)
        criado_em = hoje - timedelta(days=dias_atras, hours=3)
        criado_str = criado_em.strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """INSERT INTO ordens (numero_os, itens, destino, prioridade, status, data_criacao, data_atualizacao)
               VALUES (?,?,?,?, 'Concluido', ?, ?)""",
            (numero_os, json.dumps([{"med": med["nome"], "qtd": qtd_consumo}]),
             destinos[i % len(destinos)], "Normal", criado_str, criado_str),
        )
        ordem_row = conn.execute("SELECT * FROM ordens WHERE numero_os=?", (numero_os,)).fetchone()

        operador = operadores_demo[i % len(operadores_demo)]
        inicio = criado_em + timedelta(minutes=5)
        fim = inicio + timedelta(minutes=8 + (i % 10))
        conn.execute(
            "INSERT INTO historico (operador, perfil, acao, detalhes, timestamp) VALUES (?,?,?,?,?)",
            (operador, "Operador", "Iniciar Ordem", numero_os, inicio.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.execute(
            "INSERT INTO historico (operador, perfil, acao, detalhes, timestamp) VALUES (?,?,?,?,?)",
            (operador, "Operador", "Concluir Ordem", numero_os, fim.strftime("%Y-%m-%d %H:%M:%S")),
        )

        processar_conclusao_ordem(conn, ordem_row)

    # --- Desvios fictícios: um aberto, um em tratativa, um resolvido ---
    desvios_demo = [
        ("Estoque Insuficiente", "Falta de Ibuprofeno 600mg para atender uma ordem urgente",
         operadores_demo[0], "Aberto", "", ""),
        ("Estoque Insuficiente", "Divergência de saldo identificada no dispenser 4",
         operadores_demo[0], "Em Tratativa", "Contagem manual divergente do sistema", "Reconferência de estoque agendada"),
        ("Erro Operacional", "Troca de medicamento no dispenser errado durante o turno",
         operadores_demo[-1], "Resolvido", "Falha de treinamento do operador", "Reforço de treinamento realizado"),
    ]
    for i, (tipo, desc, operador, status, causa, acao) in enumerate(desvios_demo):
        abertura = hoje - timedelta(days=6 - i * 2)
        resolucao = (abertura + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S") if status == "Resolvido" else ""
        conn.execute(
            """INSERT INTO desvios (numero_os, tipo, descricao, operador, perfil, status, causa_raiz, acao_corretiva, data_abertura, data_resolucao)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"OS-DEMO0{i + 1}", tipo, desc, operador, "Operador", status, causa, acao,
             abertura.strftime("%Y-%m-%d %H:%M:%S"), resolucao),
        )

    conn.commit()
    conn.close()
    print("Seed: dados ficticios de demonstracao criados (lotes/ordens/historico/desvios)")


# `seed_demo_data()` NAO roda aqui. Ver `preparar_banco`.


app.jinja_env.globals.update(
    itens_resumo=itens_resumo,
    itens_total_qtd=itens_total_qtd,
    parse_itens=parse_itens,
)


# ============================================================
# Auth — Login / Logout
# ============================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if "op_id" in session:
        return redirect(url_for("dashboard"))
    erro = None
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        pin  = request.form.get("pin",  "").strip()

        # O freio ANTES de tocar no banco: conferir hash custa ~300 ms de
        # proposito (ver `gerar_pin_hash`), e esse custo e de quem defende, nao
        # de quem tenta. Sem o freio aqui, cada tentativa ainda ocupava uma
        # conexao e 300 ms de CPU do processo que e dono da porta serial.
        origem = _login_origem(nome)
        espera = login_bloqueado(origem)
        if espera:
            resposta = render_template(
                "login.html",
                erro=(f"Muitas tentativas. Aguarde {int(espera)} segundo(s) "
                      f"antes de tentar de novo."),
            )
            # 429 com `Retry-After`: a tela diz a mesma coisa que o header, e o
            # header e o que um cliente automatico entende.
            return resposta, 429, {"Retry-After": str(int(espera) or 1)}

        conn = get_db()
        # Duas etapas porque o PIN virou hash: o SQL acha o operador pelo nome,
        # e a conferencia e do `check_password_hash`. Nao ha mais consulta que
        # case por igualdade de PIN — e nao deve haver.
        op = conn.execute(
            "SELECT * FROM operadores WHERE nome=? AND ativo=1", (nome,)
        ).fetchone()
        if op and not conferir_pin(op["pin_hash"], pin):
            op = None
        if op:
            agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO historico (operador, perfil, acao, detalhes, timestamp) VALUES (?,?,?,?,?)",
                (op["nome"], op["perfil"], "Login Web", f"IP: {request.remote_addr}", agora),
            )
            conn.commit()
        conn.close()
        if not op:
            login_registrar_falha(origem)
            erro = "Nome ou PIN incorreto."
        elif op["perfil"] == "Operador":
            # PIN CERTO: o freio some. A recusa aqui e de autorizacao, não de
            # autenticação — punir por ela trancaria um operador legítimo que
            # tentasse a tela errada.
            login_limpar_falhas(origem)
            erro = "Perfil Operador não tem acesso ao sistema web. Use o display."
        else:
            login_limpar_falhas(origem)
            session["op_id"]   = op["id"]
            session["op_nome"] = op["nome"]
            session["perfil"]  = op["perfil"]
            if op["pin_provisorio"]:
                # O PIN com que esta pessoa acabou de entrar está escrito no
                # repositório. Mandar para a troca é o único ponto do fluxo em
                # que se tem certeza de que ela está na frente do teclado.
                return redirect(url_for("trocar_pin"))
            return redirect(destino_seguro(request.args.get("next"),
                                           url_for("dashboard")))
    return render_template("login.html", erro=erro)


@app.route("/trava/liberar", methods=["POST"])
@login_required
@csrf_protegido
@requer("trava_liberar")
def web_liberar_trava():
    """Libera a trava do Triple Check no central, em nome de quem está logado.

    A permissão é conferida AQUI, no servidor, e não só no template: botão
    escondido não é proteção. O que acontece depois é a única escrita que o
    painel faz no central, e ela mora em `central_comandos.py` — ver o
    docstring de lá para o porquê de existir separada do espelho.

    Quem liberou entra no histórico do painel nos dois desfechos: a recusa
    também é rastro (alguém tentou, e o central disse não, e por quê).
    """
    nome = session.get("op_nome", "")
    perfil = session.get("perfil", "")
    ok, msg = central_comandos.liberar_trava(em_nome_de=nome)
    conn = get_db()
    try:
        registrar_historico(conn, nome, perfil,
                            "Liberar trava" if ok else "Liberar trava (recusada)", msg)
        conn.commit()
    finally:
        conn.close()
    if ok:
        # O espelho atualiza na hora, sem esperar o próximo ciclo: a faixa
        # vermelha não pode continuar na tela com a OS já rodando.
        sincronizar_trava_central()
    flash(msg, "success" if ok else "danger")
    return redirect(url_for("dashboard"))


# ── Troca de PIN provisório ──────────────────────────────────────────────────
#
# Rotas que o operador pode acessar COM o PIN provisório de pé. Sem a lista, o
# guarda abaixo mandaria a própria tela de troca de volta para si mesma —
# redirect infinito na primeira entrada de todo mundo.
_LIVRES_COM_PIN_PROVISORIO = {"trocar_pin", "logout", "login", "static"}


def _pin_provisorio_na_sessao() -> bool:
    op_id = session.get("op_id")
    if op_id is None:
        return False
    conn = get_db()
    try:
        linha = conn.execute(
            "SELECT pin_provisorio FROM operadores WHERE id=?", (op_id,)
        ).fetchone()
    finally:
        conn.close()
    return bool(linha and linha["pin_provisorio"])


@app.before_request
def _obrigar_troca_de_pin():
    """Enquanto o PIN for o de fábrica, o painel só serve a tela de troca.

    O redirect no login sozinho não bastaria: quem já tem sessão aberta (de
    antes desta versão, ou porque digitou a URL direto) continuaria navegando
    com o PIN publicado. A porta é uma só, e ela fica aqui.

    A consulta é por requisição, e é de propósito: marcar a sessão no login
    deixaria quem trocou o PIN preso na tela até deslogar, e quem foi RESETADO
    por um admin continuaria solto até a sessão expirar. O custo é um SELECT
    por id num SQLite local.
    """
    if request.endpoint in _LIVRES_COM_PIN_PROVISORIO or request.endpoint is None:
        return None
    if request.path.startswith("/api/"):
        # O bloco `/api/*` autentica por token de máquina, não por sessão: o
        # PIN de um operador não diz nada sobre ele.
        return None
    if "op_id" not in session:
        return None
    if _pin_provisorio_na_sessao():
        return redirect(url_for("trocar_pin"))
    return None


@app.route("/trocar-pin", methods=["GET", "POST"])
@login_required
def trocar_pin():
    erro = None
    if request.method == "POST":
        atual = request.form.get("pin_atual", "").strip()
        novo = request.form.get("pin_novo", "").strip()
        confirma = request.form.get("pin_confirma", "").strip()

        conn = get_db()
        try:
            op = conn.execute(
                "SELECT * FROM operadores WHERE id=? AND ativo=1",
                (session["op_id"],),
            ).fetchone()
            if not op or not conferir_pin(op["pin_hash"], atual):
                erro = "PIN atual incorreto."
            elif not novo.isdigit() or len(novo) != 4:
                erro = "O PIN novo precisa ter 4 dígitos."
            elif novo != confirma:
                erro = "A confirmação não bate com o PIN novo."
            elif any(conferir_pin(op["pin_hash"], p) for p in [novo]):
                # Trocar pelo mesmo PIN é não trocar — e sairia daqui com
                # `pin_provisorio=0` sobre o valor publicado.
                erro = "O PIN novo é igual ao atual."
            elif novo in {p for _, p, _ in OPERADORES_DEFAULT}:
                # O outro jeito de não trocar nada: sair do PIN de fábrica de
                # um para o PIN de fábrica de outro.
                erro = "Esse PIN é um dos PINs de fábrica. Escolha outro."
            else:
                conn.execute(
                    "UPDATE operadores SET pin_hash=?, pin_provisorio=0 WHERE id=?",
                    (gerar_pin_hash(novo), op["id"]),
                )
                registrar_historico(conn, op["nome"], op["perfil"],
                                    "Troca de PIN", "PIN alterado pelo próprio operador")
                conn.commit()
                return redirect(url_for("dashboard"))
        finally:
            conn.close()

    return render_template("trocar_pin.html", erro=erro,
                           provisorio=_pin_provisorio_na_sessao())


@app.route("/logout")
def logout():
    if "op_id" in session:
        conn = get_db()
        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO historico (operador, perfil, acao, detalhes, timestamp) VALUES (?,?,?,?,?)",
            (session.get("op_nome",""), session.get("perfil",""), "Logout Web", "", agora),
        )
        conn.commit()
        conn.close()
    session.clear()
    return redirect(url_for("login"))


# ============================================================
# Dashboard Web
# ============================================================
@app.route("/")
@login_required
def dashboard():
    conn = get_db()
    total       = conn.execute("SELECT COUNT(*) as c FROM ordens").fetchone()["c"]
    pendentes   = conn.execute("SELECT COUNT(*) as c FROM ordens WHERE status='Pendente'").fetchone()["c"]
    em_processo = conn.execute("SELECT COUNT(*) as c FROM ordens WHERE status='Em Processo'").fetchone()["c"]
    pausados    = conn.execute("SELECT COUNT(*) as c FROM ordens WHERE status='Pausado'").fetchone()["c"]
    concluidas  = conn.execute("SELECT COUNT(*) as c FROM ordens WHERE status='Concluido'").fetchone()["c"]
    com_erro    = conn.execute("SELECT COUNT(*) as c FROM ordens WHERE status='Erro'").fetchone()["c"]
    canceladas  = conn.execute("SELECT COUNT(*) as c FROM ordens WHERE status='Cancelado'").fetchone()["c"]
    recentes    = conn.execute(
        "SELECT * FROM ordens ORDER BY data_criacao DESC LIMIT 12"
    ).fetchall()
    dispensers  = conn.execute("SELECT * FROM medicamentos ORDER BY id").fetchall()
    ultimas_acoes = conn.execute(
        "SELECT * FROM historico ORDER BY timestamp DESC LIMIT 8"
    ).fetchall()
    alertas_validade = lotes_proximos_vencimento(conn, dias=30)
    desvios_abertos = conn.execute(
        "SELECT COUNT(*) as c FROM desvios WHERE status='Aberto'"
    ).fetchone()["c"]
    conn.close()

    # Alertas: dispensers abaixo do mínimo
    alertas = [
        d for d in dispensers
        if d["minimo"] is not None and d["quantidade"] <= d["minimo"]
    ]

    # Dados para gráficos (Chart.js)
    grafico_status = json.dumps({
        "labels": ["Pendente", "Em Processo", "Pausado", "Concluido", "Erro", "Cancelado"],
        "data":   [pendentes, em_processo, pausados, concluidas, com_erro, canceladas],
        "cores":  ["#EAB308", "#2563EB", "#EA580C", "#22C55E", "#DC2626", "#94A3B8"],
    })
    grafico_disp = json.dumps({
        "labels": [d["nome"][:14] for d in dispensers],
        "atual":  [d["quantidade"] for d in dispensers],
        "minimo": [d["minimo"] if d["minimo"] is not None else 0 for d in dispensers],
        "cap":    [d["capacidade"] for d in dispensers],
    })

    return render_template(
        "dashboard.html",
        # A trava do Triple Check vem do espelho, não de uma consulta ao
        # central por carregamento de tela. Sem a conta de serviço, o botão dá
        # lugar à frase que diz qual variável definir.
        trava=trava_atual(),
        trava_msg_desligado=(None if central_comandos.habilitado()
                             else central_comandos.motivo_desligado()),
        total=total, pendentes=pendentes, em_processo=em_processo,
        pausados=pausados, concluidas=concluidas,
        com_erro=com_erro, canceladas=canceladas,
        recentes=recentes, alertas=alertas,
        ultimas_acoes=ultimas_acoes,
        grafico_status=grafico_status,
        grafico_disp=grafico_disp,
        alertas_validade=alertas_validade,
        desvios_abertos=desvios_abertos,
    )


@app.route("/ordens")
@login_required
@requer("ordens_ver")
def listar_ordens():
    filtro_status = request.args.get("status", "")
    conn = get_db()
    if filtro_status:
        ordens = conn.execute(
            "SELECT * FROM ordens WHERE status=? ORDER BY data_criacao DESC",
            (filtro_status,),
        ).fetchall()
    else:
        ordens = conn.execute(
            "SELECT * FROM ordens ORDER BY data_criacao DESC"
        ).fetchall()
    conn.close()
    return render_template("ordens.html", ordens=ordens, filtro_status=filtro_status)


@app.route("/ordens/<int:id>/detalhe")
@login_required
@requer("ordens_ver")
def ordem_detalhe(id):
    """Rastreamento completo de uma ordem: linha do tempo (quem fez o que e
    quando), lotes consumidos por item (genealogia) e desvios relacionados —
    tudo num só lugar, seja a ação originada do app web ou do display."""
    conn = get_db()
    ordem = conn.execute("SELECT * FROM ordens WHERE id=?", (id,)).fetchone()
    if not ordem:
        conn.close()
        return redirect(url_for("listar_ordens"))

    eventos = conn.execute(
        "SELECT * FROM historico WHERE detalhes=? ORDER BY timestamp ASC",
        (ordem["numero_os"],),
    ).fetchall()
    lotes_consumidos = conn.execute(
        "SELECT * FROM ordem_lotes_consumidos WHERE ordem_id=? ORDER BY data ASC",
        (id,),
    ).fetchall()
    desvios_relacionados = conn.execute(
        "SELECT * FROM desvios WHERE numero_os=? ORDER BY data_abertura ASC",
        (ordem["numero_os"],),
    ).fetchall()
    conn.close()

    return render_template(
        "ordem_detalhe.html",
        ordem=ordem, eventos=eventos,
        lotes_consumidos=lotes_consumidos,
        desvios_relacionados=desvios_relacionados,
    )


@app.route("/ordens/nova", methods=["GET", "POST"])
@login_required
@requer("ordens_criar")
def nova_ordem():
    if request.method == "POST":
        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        meds = request.form.getlist("med[]")
        qtds = request.form.getlist("qtd[]")
        itens = []
        # `strict=False` EXPLÍCITO, e o motivo é o oposto do dos outros `zip`
        # deste repositório: os dois lados vêm de campos de formulário, e um
        # POST montado à mão pode trazer contagens diferentes. `strict=True`
        # levantaria `ValueError` no meio de uma rota web — 500 na cara do
        # operador por um corpo torto. Ignorar o excedente é o que já
        # acontecia; o `strict=` só torna a decisão visível.
        for med_bruto, qtd in zip(meds, qtds, strict=False):
            med = med_bruto.strip()
            if med and qtd:
                itens.append({"med": med, "qtd": int(qtd)})
        if not itens:
            numero_os = gerar_numero_os()
            return render_template(
                "ordem_form.html",
                ordem=None,
                numero_os=numero_os,
                medicamentos=get_medicamentos_list(),
                clientes=get_clientes_list(),
                erro="Adicione pelo menos um medicamento",
            )
        conn = get_db()
        conn.execute(
            """INSERT INTO ordens
            (numero_os, itens, destino, prioridade, status, data_criacao, data_atualizacao)
            VALUES (?, ?, ?, ?, 'Pendente', ?, ?)""",
            (
                request.form["numero_os"],
                json.dumps(itens),
                request.form["destino"],
                request.form["prioridade"],
                agora,
                agora,
            ),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("listar_ordens"))

    numero_os = gerar_numero_os()
    return render_template(
        "ordem_form.html",
        ordem=None,
        numero_os=numero_os,
        medicamentos=get_medicamentos_list(),
        clientes=get_clientes_list(),
        erro=None,
    )


@app.route("/ordens/<int:id>/editar", methods=["GET", "POST"])
@login_required
@requer("ordens_editar")
def editar_ordem(id):
    conn = get_db()
    alvo = conn.execute("SELECT * FROM ordens WHERE id=?", (id,)).fetchone()
    if ordem_e_do_central(alvo):
        conn.close()
        flash(
            f"A ordem {alvo['numero_os']} e do computador central: itens e "
            f"destino vem de la e nao sao editaveis aqui.",
            "warning",
        )
        return redirect(url_for("listar_ordens"))

    if request.method == "POST":
        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        meds = request.form.getlist("med[]")
        qtds = request.form.getlist("qtd[]")
        itens = []
        # `strict=False` EXPLÍCITO, e o motivo é o oposto do dos outros `zip`
        # deste repositório: os dois lados vêm de campos de formulário, e um
        # POST montado à mão pode trazer contagens diferentes. `strict=True`
        # levantaria `ValueError` no meio de uma rota web — 500 na cara do
        # operador por um corpo torto. Ignorar o excedente é o que já
        # acontecia; o `strict=` só torna a decisão visível.
        for med_bruto, qtd in zip(meds, qtds, strict=False):
            med = med_bruto.strip()
            if med and qtd:
                itens.append({"med": med, "qtd": int(qtd)})
        conn.execute(
            """UPDATE ordens SET itens=?,
            destino=?, prioridade=?, status=?, data_atualizacao=?
            WHERE id=?""",
            (
                json.dumps(itens),
                request.form["destino"],
                request.form["prioridade"],
                request.form["status"],
                agora,
                id,
            ),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("listar_ordens"))

    ordem = conn.execute("SELECT * FROM ordens WHERE id=?", (id,)).fetchone()
    conn.close()
    return render_template(
        "ordem_form.html",
        ordem=ordem,
        numero_os=None,
        medicamentos=get_medicamentos_list(),
        clientes=get_clientes_list(),
        erro=None,
    )


@app.route("/ordens/<int:id>/excluir", methods=["POST"])
@login_required
@csrf_protegido
@requer("ordens_excluir")
def excluir_ordem(id):
    conn = get_db()
    alvo = conn.execute("SELECT * FROM ordens WHERE id=?", (id,)).fetchone()
    if ordem_e_do_central(alvo):
        conn.close()
        flash(
            f"A ordem {alvo['numero_os']} e do computador central: apaga-la "
            f"aqui so a traria de volta na proxima sincronizacao.",
            "warning",
        )
        return redirect(url_for("listar_ordens"))
    conn.execute("DELETE FROM ordens WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("listar_ordens"))


@app.route("/ordens/<int:id>/status/<status>", methods=["POST"])
@login_required
@requer("ordens_status")
def atualizar_status(id, status):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    row = conn.execute("SELECT * FROM ordens WHERE id=?", (id,)).fetchone()

    # O status chega pela URL — `/ordens/1/status/Qualquer` escrevia "Qualquer"
    # no banco sem reclamar, e a ordem sumia de toda tela que soma por
    # igualdade de string. Recusa ANTES de tocar no banco (ver STATUS_VALIDOS).
    if not status_valido(status):
        conn.close()
        flash(f"Status {status!r} nao existe no painel.", "danger")
        return redirect(request.referrer or url_for("listar_ordens"))

    # Esta rota nao passa por `_set_status_by_numero_os` — escreve o UPDATE ela
    # mesma —, entao o bloqueio do espelho precisa estar aqui tambem.
    if ordem_e_do_central(row):
        conn.close()
        flash(
            f"A ordem {row['numero_os']} e do computador central: o painel a "
            f"espelha e nao altera o status dela.",
            "warning",
        )
        return redirect(request.referrer or url_for("listar_ordens"))

    # Mesma regra do display: nao libera "Em Processo" com estoque insuficiente.
    if row and status == "Em Processo" and row["status"] in ("Pendente", "Pausado"):
        ok, erro = verificar_estoque_ordem(conn, row)
        if not ok:
            registrar_desvio(
                conn, row["numero_os"], "Estoque Insuficiente", erro,
                session.get("op_nome", ""), session.get("perfil", ""),
            )
            conn.commit()
            conn.close()
            flash(f"Não é possível iniciar a ordem {row['numero_os']}: {erro}", "danger")
            return redirect(request.referrer or url_for("listar_ordens"))

    conn.execute(
        "UPDATE ordens SET status=?, data_atualizacao=? WHERE id=?",
        (status, agora, id),
    )
    if row and status == "Concluido" and row["status"] != "Concluido":
        processar_conclusao_ordem(conn, row)
    if row:
        acao_por_status = {
            "Em Processo": "Iniciar Ordem", "Pausado": "Pausar Ordem", "Concluido": "Concluir Ordem",
        }
        acao = acao_por_status.get(status, f"Status -> {status}")
        registrar_historico(conn, session.get("op_nome", ""), session.get("perfil", ""), acao, row["numero_os"])
    conn.commit()
    conn.close()
    if row:
        publish_order_action(row["numero_os"], status, "app")
    return redirect(request.referrer or url_for("listar_ordens"))


@app.route("/relatorio")
@login_required
@requer("relatorio_ver")
def relatorio():
    conn = get_db()
    ordens = conn.execute(
        "SELECT * FROM ordens ORDER BY data_criacao DESC"
    ).fetchall()
    total = len(ordens)
    concluidas = sum(1 for o in ordens if o["status"] == "Concluido")
    pendentes = sum(1 for o in ordens if o["status"] == "Pendente")
    em_processo = sum(1 for o in ordens if o["status"] == "Em Processo")
    com_erro = sum(1 for o in ordens if o["status"] == "Erro")
    canceladas = sum(1 for o in ordens if o["status"] == "Cancelado")
    conn.close()

    host = request.host
    qr_url = f"http://{host}/relatorio"

    return render_template(
        "relatorio.html",
        ordens=ordens,
        total=total,
        concluidas=concluidas,
        pendentes=pendentes,
        em_processo=em_processo,
        com_erro=com_erro,
        canceladas=canceladas,
        qr_url=qr_url,
    )


@app.route("/relatorio/qrcode.png")
def relatorio_qrcode():
    host = request.host
    url = f"http://{host}/relatorio"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


# ============================================================
# Medicamentos (CRUD)
# ============================================================
@app.route("/medicamentos")
@login_required
@requer("dispensers_ver")
def listar_medicamentos():
    conn = get_db()
    meds = conn.execute("SELECT * FROM medicamentos ORDER BY id").fetchall()
    conn.close()
    return render_template("medicamentos.html", medicamentos=meds)


@app.route("/medicamentos/novo", methods=["POST"])
@login_required
@requer("dispensers_editar")
def novo_medicamento():
    nome   = request.form.get("nome", "").strip()
    qtd    = int(request.form.get("quantidade", 60))
    cap    = int(request.form.get("capacidade", 60))
    minimo_raw = request.form.get("minimo", "").strip()
    minimo = int(minimo_raw) if minimo_raw.isdigit() else None
    if nome:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO medicamentos (nome, quantidade, capacidade, minimo) VALUES (?, ?, ?, ?)",
            (nome, qtd, cap, minimo),
        )
        conn.commit()
        conn.close()
    return redirect(url_for("listar_medicamentos"))


@app.route("/medicamentos/<int:id>/editar", methods=["POST"])
@login_required
@requer("dispensers_editar")
def editar_medicamento(id):
    nome   = request.form.get("nome", "").strip()
    qtd    = int(request.form.get("quantidade", 60))
    cap    = int(request.form.get("capacidade", 60))
    minimo_raw = request.form.get("minimo", "").strip()
    minimo = int(minimo_raw) if minimo_raw.isdigit() else None
    if nome:
        conn = get_db()
        conn.execute(
            "UPDATE medicamentos SET nome=?, quantidade=?, capacidade=?, minimo=? WHERE id=?",
            (nome, qtd, cap, minimo, id),
        )
        conn.commit()
        conn.close()
    return redirect(url_for("listar_medicamentos"))


# Tabelas que guardam o rastro de um medicamento. A coluna e sempre
# `medicamento_id`, e so `lotes` declara a FK — as outras tres nasceram sem
# `REFERENCES` e continuam assim: acrescentar a clausula agora exigiria
# reconstruir as tres tabelas, e a FK so protegeria daqui para frente, que e
# exatamente o que esta lista ja faz sem tocar em dado nenhum.
_TABELAS_COM_HISTORICO_DE_MEDICAMENTO = (
    "lotes",
    "lotes_baixas",
    "ordem_lotes_consumidos",
    "estoque_visao",
)


def _tem_historico(conn, medicamento_id):
    """Existe rastro deste medicamento em alguma tabela de historico?"""
    for tabela in _TABELAS_COM_HISTORICO_DE_MEDICAMENTO:
        achou = conn.execute(
            f"SELECT 1 FROM {tabela} WHERE medicamento_id=? LIMIT 1",
            (medicamento_id,),
        ).fetchone()
        if achou:
            return True
    return False


@app.route("/medicamentos/<int:id>/excluir", methods=["POST"])
@login_required
@csrf_protegido
@requer("dispensers_editar")
def excluir_medicamento(id):
    """Retira o medicamento do catalogo. DELETE so quando nao ha o que perder.

    O `DELETE` incondicional que existia aqui fazia dois estragos, e nenhum dos
    dois aparecia na tela:

    1. **Orfanava o historico.** Lote, baixa de lote, genealogia de ordem e
       leitura da estacao de visao apontam para `medicamento_id`, e a linha
       apontada sumia. Num painel cuja razao de existir e rastreabilidade, o
       registro de QUAL lote saiu em QUAL ordem passava a apontar para o vazio.
       O `PRAGMA foreign_keys=ON` do `get_db` fecha essa porta para `lotes`; as
       outras tres nao tem FK declarada, e e por isso que a decisao nao pode
       ficar so com o banco.
    2. **Renumerava a bancada inteira.** `_slot_para_id` e
       `_dispensers_data_local` numeram o slot pela POSICAO da linha
       (`i + 1`), nao pelo id. Apagado o medicamento da posicao 3, o que era
       D4 vira D3, D5 vira D4, e dai em diante — todo o estoque do display
       muda de slot. Sem erro, sem log: so o numero errado embaixo do
       medicamento certo.

    Desativar resolve os dois de uma vez, porque a LINHA fica: o historico
    continua tendo para onde apontar e a posicao de cada slot nao se mexe. O
    `DELETE` sobrevive para o caso em que ele e inofensivo — medicamento
    cadastrado por engano, sem lote, sem baixa, sem ordem e sem leitura —
    porque ai desativar so deixaria lixo permanente na tela.
    """
    conn = get_db()
    try:
        if _tem_historico(conn, id):
            conn.execute("UPDATE medicamentos SET ativo=0 WHERE id=?", (id,))
        else:
            try:
                conn.execute("DELETE FROM medicamentos WHERE id=?", (id,))
            except sqlite3.IntegrityError:
                # Referencia que `_TABELAS_COM_HISTORICO_DE_MEDICAMENTO` nao
                # conhece — tabela nova, FK nova. Cair na desativacao e o
                # comportamento certo; virar 500 na tela do operador, nao.
                conn.execute("UPDATE medicamentos SET ativo=0 WHERE id=?", (id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("listar_medicamentos"))


@app.route("/medicamentos/<int:id>/reativar", methods=["POST"])
@login_required
@requer("dispensers_editar")
def reativar_medicamento(id):
    """Volta o medicamento ao catalogo.

    Existe porque desativar sem caminho de volta seria uma porta de uma folha:
    quem clicasse em excluir por engano ficaria com a linha na tela, marcada
    como inativa, e sem nada a fazer a respeito.
    """
    conn = get_db()
    try:
        conn.execute("UPDATE medicamentos SET ativo=1 WHERE id=?", (id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("listar_medicamentos"))


def _registrar_entrada_lote(conn, medicamento_id, lote, validade, quantidade, fornecedor, nota_fiscal,
                             data_fabricacao, operador, perfil):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO lotes (medicamento_id, lote, validade, quantidade, quantidade_inicial, status,
                              data_entrada, fornecedor, nota_fiscal, data_fabricacao)
           VALUES (?,?,?,?,?, 'Ativo', ?,?,?,?)""",
        (medicamento_id, lote, validade or None, quantidade, quantidade, agora, fornecedor, nota_fiscal, data_fabricacao or None),
    )
    conn.execute(
        "UPDATE medicamentos SET quantidade = quantidade + ? WHERE id=?",
        (quantidade, medicamento_id),
    )
    registrar_historico(conn, operador, perfil, "Entrada de Lote",
                         f"{lote} ({quantidade} un.) - {fornecedor or 'sem fornecedor'}")


@app.route("/medicamentos/<int:id>/lotes/novo", methods=["POST"])
@login_required
@requer("lotes_editar")
def novo_lote(id):
    lote = request.form.get("lote", "").strip()
    validade = request.form.get("validade", "").strip()
    quantidade = int(request.form.get("quantidade", 0) or 0)
    fornecedor = request.form.get("fornecedor", "").strip()
    nota_fiscal = request.form.get("nota_fiscal", "").strip()
    data_fabricacao = request.form.get("data_fabricacao", "").strip()
    if lote and quantidade > 0:
        conn = get_db()
        _registrar_entrada_lote(conn, id, lote, validade, quantidade, fornecedor, nota_fiscal, data_fabricacao,
                                 session.get("op_nome", ""), session.get("perfil", ""))
        conn.commit()
        conn.close()
    return redirect(url_for("rastreabilidade") + f"#med-{id}")


@app.route("/lotes/entrada", methods=["GET", "POST"])
@login_required
@requer("lotes_editar")
def entrada_lote():
    conn = get_db()
    if request.method == "POST":
        medicamento_id = request.form.get("medicamento_id", type=int)
        lote = request.form.get("lote", "").strip()
        validade = request.form.get("validade", "").strip()
        quantidade = int(request.form.get("quantidade", 0) or 0)
        fornecedor = request.form.get("fornecedor", "").strip()
        nota_fiscal = request.form.get("nota_fiscal", "").strip()
        data_fabricacao = request.form.get("data_fabricacao", "").strip()

        med = conn.execute("SELECT nome FROM medicamentos WHERE id=?", (medicamento_id,)).fetchone() if medicamento_id else None
        if not med:
            flash("Selecione um medicamento válido.", "danger")
        elif not lote or quantidade <= 0:
            flash("Preencha o número do lote e uma quantidade maior que zero.", "danger")
        else:
            _registrar_entrada_lote(conn, medicamento_id, lote, validade, quantidade, fornecedor, nota_fiscal,
                                     data_fabricacao, session.get("op_nome", ""), session.get("perfil", ""))
            conn.commit()
            flash(f"Entrada de {quantidade} un. do lote {lote} registrada para {med['nome']}.", "success")
        conn.close()
        return redirect(url_for("entrada_lote"))

    # So medicamento ATIVO entra no seletor: dar entrada de lote novo num item
    # retirado do catalogo e criar estoque para o que a operacao ja decidiu nao
    # usar. O saldo que ele ja tem continua onde esta — desativar e tirar do
    # catalogo, nao apagar o que esta na prateleira.
    medicamentos = conn.execute(
        "SELECT id, nome FROM medicamentos WHERE ativo=1 ORDER BY nome"
    ).fetchall()
    ultimas = conn.execute(
        """SELECT lotes.*, medicamentos.nome as medicamento_nome FROM lotes
           JOIN medicamentos ON medicamentos.id = lotes.medicamento_id
           ORDER BY lotes.id DESC LIMIT 8"""
    ).fetchall()
    conn.close()
    return render_template("entrada_lote.html", medicamentos=medicamentos, ultimas=ultimas)


@app.route("/lotes/<int:id>/bloquear", methods=["POST"])
@login_required
@requer("lotes_editar")
def bloquear_lote(id):
    conn = get_db()
    lote = conn.execute("SELECT * FROM lotes WHERE id=?", (id,)).fetchone()
    med_id = lote["medicamento_id"] if lote else None
    # O botão só alterna entre dispensável e bloqueado. Lote 'Esgotado' ou
    # 'Baixado' já teve o saldo retirado do agregado por outro caminho;
    # alterná-lo aqui rotularia de "Bloqueado" um lote que acabou, e o
    # desbloqueio seguinte o devolveria como 'Ativo' de saldo zero.
    if lote and lote["status"] in (LOTE_STATUS_DISPENSAVEL, "Bloqueado"):
        bloqueando = lote["status"] != "Bloqueado"
        novo_status = "Bloqueado" if bloqueando else LOTE_STATUS_DISPENSAVEL
        conn.execute("UPDATE lotes SET status=? WHERE id=?", (novo_status, id))
        # O saldo acompanha o status: lote bloqueado não é estoque dispensável.
        # Sem esta linha o agregado seguia contando o lote bloqueado e a ordem
        # era liberada para consumi-lo sem genealogia (ver o bloco no topo).
        _mover_saldo_lote(conn, lote, dispensavel=not bloqueando)
        registrar_historico(conn, session.get("op_nome", ""), session.get("perfil", ""),
                             "Bloquear Lote" if bloqueando else "Desbloquear Lote",
                             f"{lote['lote']} ({lote['medicamento_id']})")
        conn.commit()
    conn.close()
    return redirect(url_for("rastreabilidade") + (f"#med-{med_id}" if med_id else ""))


@app.route("/lotes/<int:id>/baixa", methods=["POST"])
@login_required
@requer("lotes_editar")
def baixa_lote(id):
    """Baixa manual de lote: vencido, avariado, perda etc. Remove o saldo do
    lote (e do agregado do medicamento) sem passar por consumo de ordem —
    completa a rastreabilidade cobrindo saidas que nao sao dispensacao."""
    motivo = request.form.get("motivo", "").strip() or "Não informado"
    conn = get_db()
    lote = conn.execute("SELECT * FROM lotes WHERE id=?", (id,)).fetchone()
    med_id = lote["medicamento_id"] if lote else None
    if lote and lote["quantidade"] > 0:
        med = conn.execute("SELECT nome FROM medicamentos WHERE id=?", (lote["medicamento_id"],)).fetchone()
        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE lotes SET quantidade=0, status='Baixado' WHERE id=?", (id,)
        )
        # Só desconta do agregado o lote que AINDA contava nele. Um lote
        # bloqueado já saiu do agregado no bloqueio; descontá-lo outra vez aqui
        # apagaria do estoque unidades que estão na prateleira — o mesmo erro
        # de "dois números que precisam concordar", agora pelo outro lado.
        if lote["status"] == LOTE_STATUS_DISPENSAVEL:
            _mover_saldo_lote(conn, lote, dispensavel=False)
        conn.execute(
            """INSERT INTO lotes_baixas (lote_id, medicamento_id, medicamento_nome, lote, quantidade, motivo, operador, data)
               VALUES (?,?,?,?,?,?,?,?)""",
            (id, lote["medicamento_id"], med["nome"] if med else "", lote["lote"],
             lote["quantidade"], motivo, session.get("op_nome", ""), agora),
        )
        registrar_historico(conn, session.get("op_nome", ""), session.get("perfil", ""), "Baixa de Lote",
                             f"{lote['lote']} ({lote['quantidade']} un.) - {motivo}")
        conn.commit()
    conn.close()
    return redirect(url_for("rastreabilidade") + (f"#med-{med_id}" if med_id else ""))


@app.route("/rastreabilidade")
@login_required
@requer("lotes_ver")
def rastreabilidade():
    termo = request.args.get("lote", "").strip()
    termo_cliente = request.args.get("cliente", "").strip()
    conn = get_db()
    consumos = []
    if termo:
        consumos = conn.execute(
            "SELECT * FROM ordem_lotes_consumidos WHERE lote LIKE ? ORDER BY data DESC",
            (f"%{termo}%",),
        ).fetchall()

    ordens_cliente = []
    if termo_cliente:
        ordens_cliente = conn.execute(
            "SELECT * FROM ordens WHERE destino LIKE ? ORDER BY data_criacao DESC",
            (f"%{termo_cliente}%",),
        ).fetchall()

    # Gestao de lotes (cadastro/entrada/bloqueio/baixa) mora aqui, junto com a
    # busca de consumo — tudo que e rastreabilidade de produto num so lugar.
    meds = conn.execute("SELECT * FROM medicamentos ORDER BY id").fetchall()
    lotes_por_med = {}
    for m in meds:
        lotes_por_med[m["id"]] = conn.execute(
            "SELECT * FROM lotes WHERE medicamento_id=? ORDER BY (validade IS NULL OR validade=''), validade ASC",
            (m["id"],),
        ).fetchall()
    proximos_vencer = lotes_proximos_vencimento(conn, dias=30)
    total_lotes = conn.execute("SELECT COUNT(*) as c FROM lotes WHERE status='Ativo'").fetchone()["c"]
    sem_lote = sum(1 for m in meds if not lotes_por_med.get(m["id"]))
    clientes = conn.execute("SELECT nome FROM clientes WHERE ativo=1 ORDER BY nome").fetchall()
    conn.close()

    return render_template(
        "rastreabilidade.html", termo=termo, consumos=consumos,
        termo_cliente=termo_cliente, ordens_cliente=ordens_cliente,
        clientes=[c["nome"] for c in clientes],
        medicamentos=meds, lotes_por_med=lotes_por_med,
        proximos_vencer=proximos_vencer, total_lotes=total_lotes, sem_lote=sem_lote,
    )


# ============================================================
# Desvios / Não-conformidade
# ============================================================
@app.route("/admin/desvios")
@login_required
@requer("desvios_ver")
def admin_desvios():
    filtro_status = request.args.get("status", "")
    conn = get_db()
    query = "SELECT * FROM desvios WHERE 1=1"
    params = []
    if filtro_status:
        query += " AND status=?"
        params.append(filtro_status)
    query += " ORDER BY data_abertura DESC LIMIT 200"
    desvios = conn.execute(query, params).fetchall()
    abertos = conn.execute("SELECT COUNT(*) as c FROM desvios WHERE status='Aberto'").fetchone()["c"]
    em_tratativa = conn.execute("SELECT COUNT(*) as c FROM desvios WHERE status='Em Tratativa'").fetchone()["c"]
    resolvidos = conn.execute("SELECT COUNT(*) as c FROM desvios WHERE status='Resolvido'").fetchone()["c"]
    conn.close()
    return render_template(
        "admin_desvios.html", desvios=desvios, filtro_status=filtro_status,
        abertos=abertos, em_tratativa=em_tratativa, resolvidos=resolvidos,
    )


@app.route("/admin/desvios/<int:id>/atualizar", methods=["POST"])
@login_required
@requer("desvios_editar")
def atualizar_desvio(id):
    status = request.form.get("status", "Aberto")
    causa_raiz = request.form.get("causa_raiz", "").strip()
    acao_corretiva = request.form.get("acao_corretiva", "").strip()
    conn = get_db()
    data_resolucao = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status == "Resolvido" else ""
    conn.execute(
        """UPDATE desvios SET status=?, causa_raiz=?, acao_corretiva=?, data_resolucao=?
           WHERE id=?""",
        (status, causa_raiz, acao_corretiva, data_resolucao, id),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_desvios"))


@app.route("/api/desvios", methods=["POST"])
@api_token_required
def api_criar_desvio():
    data = request.get_json(silent=True) or {}
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    perfil = ""
    if data.get("operador"):
        op = conn.execute(
            "SELECT perfil FROM operadores WHERE nome=? AND ativo=1", (data["operador"],)
        ).fetchone()
        perfil = op["perfil"] if op else ""
    conn.execute(
        """INSERT INTO desvios (numero_os, tipo, descricao, operador, perfil, status, data_abertura)
           VALUES (?,?,?,?,?, 'Aberto', ?)""",
        (
            data.get("numero_os", ""),
            data.get("tipo", "Outro"),
            data.get("descricao", ""),
            data.get("operador", ""),
            perfil,
            agora,
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True}), 201


# ============================================================
# Estação de visão — API e tela
# ============================================================
@app.route("/api/visao/estoque", methods=["POST"])
@api_token_required
def api_visao_estoque():
    """Recebe o nível medido pela câmera e o transforma em estoque + validação.

    Corpo:
        {"estacao": "bancada-1",
         "leituras": [{"dispenser": 1, "sku": "MED-001",
                       "medicamento": "Dipirona 500mg", "caixas": 4,
                       "confianca": 0.82, "veredito": "OK",
                       "momento": "2026-08-17 14:03:11", "detalhe": ""}]}

    A resposta diz, por leitura, o que foi feito — a estação usa isso para saber
    se pode marcar o envio como entregue ou se precisa reenfileirar.
    """
    data = request.get_json(silent=True) or {}
    leituras = data.get("leituras")
    if leituras is None:
        leituras = [data] if "dispenser" in data or "sku" in data else []
    if not isinstance(leituras, list) or not leituras:
        return jsonify({"ok": False, "erro": "envie 'leituras' com ao menos um item"}), 400

    estacao = (data.get("estacao") or "").strip()
    conn = get_db()
    try:
        resultados = [processar_leitura_visao(conn, l, estacao) for l in leituras]
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        return jsonify({"ok": False, "erro": str(exc)}), 500
    conn.close()

    # Empurra o estoque novo para o display na hora. Ele já busca sozinho a
    # cada 5 s, mas esperar o próximo ciclo faz a tela parecer atrasada em
    # relação à prateleira — o operador tira a caixa e o número demora. O push
    # é silencioso se o display estiver desconectado, e o polling continua ali
    # como rede de segurança caso a mensagem se perca.
    if any(r["acao"] in ("saida", "reposicao") for r in resultados):
        conn = get_db()
        try:
            push_to_display({"push": "dispensers", "data": _dispensers_data(conn)})
        finally:
            conn.close()

    houve_erro = any(r["acao"] == "erro" for r in resultados)
    return jsonify({"ok": not houve_erro, "resultados": resultados}), 200 if not houve_erro else 207


@app.route("/api/visao/catalogo")
@api_token_required
def api_visao_catalogo():
    """Diz à estação de visão o que ela deve esperar em cada zona da câmera.

    É a perna que faltava para o backend ser de fato o centro: sem ela a
    estação decidia OK/ERRO_POSICAO por um arquivo local próprio, que não tinha
    relação nenhuma com o estoque daqui. Duas verdades sobre "que medicamento
    vive no dispenser 1" divergem no primeiro remanejamento — e a divergência
    aparece como veredito OK em cima do medicamento errado, que é o pior modo
    de falhar deste sistema.

    Só entra dispenser com zona E sku definidos: sem o código impresso a
    estação não tem como identificar nada, e um item sem `qr` quebraria a
    montagem do catálogo do lado dela.
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM medicamentos
           WHERE dispenser_visao IS NOT NULL ORDER BY dispenser_visao"""
    ).fetchall()
    conn.close()

    medicamentos, incompletos = [], []
    for r in rows:
        sku = (r["sku_visao"] or "").strip()
        if not sku:
            incompletos.append({"dispenser": r["dispenser_visao"], "nome": r["nome"],
                                "motivo": "sem SKU (QR) definido"})
            continue
        medicamentos.append({
            "qr": sku,
            "nome": r["nome"],
            "dispenser": r["dispenser_visao"],
            "aruco": r["aruco_visao"],
            "unidades_por_caixa": r["unidades_por_caixa"] or 1,
        })

    return jsonify({"medicamentos": medicamentos, "incompletos": incompletos})


@app.route("/api/visao/movimentos")
@api_token_required
def api_visao_movimentos():
    limite = min(int(request.args.get("limite", 100)), 1000)
    acao = request.args.get("acao", "")
    conn = get_db()
    sql = "SELECT * FROM estoque_visao WHERE 1=1"
    params = []
    if acao:
        sql += " AND acao=?"
        params.append(acao)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limite)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/visao")
@login_required
@requer("visao_ver")
def visao_estacao():
    conn = get_db()
    medicamentos = conn.execute("SELECT * FROM medicamentos ORDER BY id").fetchall()
    movimentos = conn.execute(
        "SELECT * FROM estoque_visao ORDER BY id DESC LIMIT 100"
    ).fetchall()
    monitorados = conn.execute(
        "SELECT COUNT(*) c FROM medicamentos WHERE dispenser_visao IS NOT NULL"
    ).fetchone()["c"]
    saidas = conn.execute(
        "SELECT COUNT(*) c FROM estoque_visao WHERE acao='saida'"
    ).fetchone()["c"]
    bloqueios = conn.execute(
        "SELECT COUNT(*) c FROM estoque_visao WHERE acao IN ('bloqueado','erro')"
    ).fetchone()["c"]
    ultima = conn.execute(
        "SELECT momento, estacao FROM estoque_visao ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return render_template(
        "visao.html", medicamentos=medicamentos, movimentos=movimentos,
        monitorados=monitorados, saidas=saidas, bloqueios=bloqueios, ultima=ultima,
        confianca_minima=VISAO_CONFIANCA_MINIMA,
    )


@app.route("/visao/mapeamento", methods=["POST"])
@login_required
@requer("visao_editar")
def visao_salvar_mapeamento():
    conn = get_db()
    vistos, skus_vistos, arucos_vistos = {}, {}, {}
    for med in conn.execute("SELECT id FROM medicamentos").fetchall():
        mid = med["id"]
        bruto = (request.form.get(f"dispenser_{mid}", "") or "").strip()
        dispenser = int(bruto) if bruto.isdigit() else None
        sku = (request.form.get(f"sku_{mid}", "") or "").strip()
        bruto_aruco = (request.form.get(f"aruco_{mid}", "") or "").strip()
        aruco = int(bruto_aruco) if bruto_aruco.isdigit() else None
        try:
            por_caixa = max(1, int(request.form.get(f"upc_{mid}", 1)))
        except ValueError:
            por_caixa = 1

        # Duas linhas apontando para a mesma zona fariam a leitura debitar de um
        # medicamento e depois do outro, alternando. SKU ou ArUco repetidos são
        # igualmente fatais: a estação recusa montar um catálogo ambíguo e cairia
        # no arquivo local. Melhor recusar aqui do que gravar um mapeamento que
        # produz estoque errado em silêncio.
        def recusar(msg):
            conn.close()
            flash(msg, "danger")
            return redirect(url_for("visao_estacao"))

        if dispenser is not None:
            if dispenser in vistos:
                return recusar(f"Dispenser {dispenser} está mapeado em dois "
                               f"medicamentos. Cada zona da câmera só pode apontar "
                               f"para um.")
            vistos[dispenser] = mid
            if sku and sku in skus_vistos:
                return recusar(f"O SKU '{sku}' está em dois medicamentos. "
                               f"Cada código precisa ser único.")
            if sku:
                skus_vistos[sku] = mid
            if aruco is not None and aruco in arucos_vistos:
                return recusar(f"O ArUco {aruco} está em dois medicamentos. "
                               f"Cada marcador precisa ser único.")
            if aruco is not None:
                arucos_vistos[aruco] = mid

        conn.execute(
            """UPDATE medicamentos SET dispenser_visao=?, sku_visao=?,
               unidades_por_caixa=?, aruco_visao=? WHERE id=?""",
            (dispenser, sku, por_caixa, aruco, mid),
        )

    registrar_historico(conn, session.get("op_nome", "---"), session.get("perfil", ""),
                        "Mapeamento da visão", f"{len(vistos)} dispenser(s) monitorado(s)")
    conn.commit()
    conn.close()
    flash("Mapeamento da estação de visão salvo.", "success")
    return redirect(url_for("visao_estacao"))


# ============================================================
# KPIs de Produção
# ============================================================
@app.route("/kpis")
@login_required
@requer("kpis_ver")
def kpis():
    conn = get_db()

    # Tempo de ciclo (Iniciar Ordem -> Concluir Ordem) pelos logs do histórico,
    # casando pelo numero_os armazenado em detalhes.
    inicios = conn.execute(
        "SELECT detalhes as numero_os, timestamp FROM historico WHERE acao='Iniciar Ordem'"
    ).fetchall()
    fins = conn.execute(
        "SELECT detalhes as numero_os, timestamp FROM historico WHERE acao='Concluir Ordem'"
    ).fetchall()
    inicio_map = {r["numero_os"]: r["timestamp"] for r in inicios}
    duracoes_min = []
    for f in fins:
        ini = inicio_map.get(f["numero_os"])
        if not ini:
            continue
        try:
            dt_ini = datetime.strptime(ini, "%Y-%m-%d %H:%M:%S")
            dt_fim = datetime.strptime(f["timestamp"], "%Y-%m-%d %H:%M:%S")
            delta_min = (dt_fim - dt_ini).total_seconds() / 60
            if delta_min >= 0:
                duracoes_min.append(delta_min)
        except ValueError:
            continue
    tempo_medio_min = round(sum(duracoes_min) / len(duracoes_min), 1) if duracoes_min else 0

    # Ranking de operador por ordens concluídas
    ranking = conn.execute(
        """SELECT operador, COUNT(*) as total FROM historico
           WHERE acao='Concluir Ordem' GROUP BY operador ORDER BY total DESC LIMIT 8"""
    ).fetchall()

    # Ordens concluídas por dia (últimos 14 dias)
    por_dia = conn.execute(
        """SELECT substr(timestamp,1,10) as dia, COUNT(*) as total FROM historico
           WHERE acao='Concluir Ordem' AND timestamp >= date('now','-14 days')
           GROUP BY dia ORDER BY dia ASC"""
    ).fetchall()

    # Ordens pendentes há mais de 4h (SLA estourado)
    limite_sla = (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
    sla_estourado = conn.execute(
        "SELECT COUNT(*) as c FROM ordens WHERE status='Pendente' AND data_criacao <= ?",
        (limite_sla,),
    ).fetchone()["c"]

    total_desvios = conn.execute("SELECT COUNT(*) as c FROM desvios").fetchone()["c"]
    total_ordens = conn.execute("SELECT COUNT(*) as c FROM ordens").fetchone()["c"]
    # 'Erro' e 'Cancelado' entram no vocabulario pelo espelho do central. Sem
    # um contador proprio, uma OS que a celula abortou nao apareceria em KPI
    # nenhum — nem em 'concluidas', nem em 'pendentes'.
    ordens_erro = conn.execute(
        "SELECT COUNT(*) as c FROM ordens WHERE status='Erro'"
    ).fetchone()["c"]
    ordens_canceladas = conn.execute(
        "SELECT COUNT(*) as c FROM ordens WHERE status='Cancelado'"
    ).fetchone()["c"]

    conn.close()

    grafico_dia = json.dumps({
        "labels": [r["dia"][5:] for r in por_dia],
        "data": [r["total"] for r in por_dia],
    })
    grafico_ranking = json.dumps({
        "labels": [r["operador"] for r in ranking],
        "data": [r["total"] for r in ranking],
    })

    return render_template(
        "kpis.html",
        tempo_medio_min=tempo_medio_min,
        sla_estourado=sla_estourado,
        total_desvios=total_desvios,
        total_ordens=total_ordens,
        ordens_erro=ordens_erro,
        ordens_canceladas=ordens_canceladas,
        taxa_desvio=round(100 * total_desvios / total_ordens, 1) if total_ordens else 0,
        grafico_dia=grafico_dia,
        grafico_ranking=grafico_ranking,
        tem_dados_dia=len(por_dia) > 0,
        tem_dados_ranking=len(ranking) > 0,
    )


# ============================================================
# API REST para ESP32
# ============================================================
@app.route("/api/ordens")
@api_token_required
def api_ordens():
    conn = get_db()
    status = request.args.get("status")
    if status:
        rows = conn.execute(
            "SELECT * FROM ordens WHERE status=? ORDER BY data_criacao DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ordens ORDER BY data_criacao DESC"
        ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["itens_lista"] = parse_itens(r)
        d["medicamento"] = itens_resumo(r)
        d["quantidade"] = itens_total_qtd(r)
        result.append(d)
    return jsonify(result)


@app.route("/api/ordens", methods=["POST"])
@api_token_required
def api_criar_ordem():
    data = request.get_json()
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    itens = data.get("itens", [])
    if not itens and "medicamento" in data:
        itens = [{"med": data["medicamento"], "qtd": data.get("quantidade", 1)}]
    conn = get_db()
    conn.execute(
        """INSERT INTO ordens
        (numero_os, itens, destino, prioridade, status, data_criacao, data_atualizacao)
        VALUES (?, ?, ?, ?, 'Pendente', ?, ?)""",
        (
            data["numero_os"],
            json.dumps(itens),
            data.get("destino", ""),
            data.get("prioridade", "Normal"),
            agora,
            agora,
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True}), 201


@app.route("/api/ordens/<int:id>/status", methods=["PUT"])
@api_token_required
def api_atualizar_status(id):
    data = request.get_json()
    conn = get_db()
    row = conn.execute("SELECT numero_os FROM ordens WHERE id=?", (id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "erro": "ordem nao encontrada"}), 404

    ok, erro = _set_status_by_numero_os(conn, row["numero_os"], data["status"])
    conn.close()
    if not ok:
        return jsonify({"ok": False, "erro": erro}), 409
    publish_order_action(row["numero_os"], data["status"], "display")
    return jsonify({"ok": True})


@app.route("/api/resumo")
@api_token_required
def api_resumo():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) as c FROM ordens").fetchone()["c"]
    pendentes = conn.execute(
        "SELECT COUNT(*) as c FROM ordens WHERE status='Pendente'"
    ).fetchone()["c"]
    em_processo = conn.execute(
        "SELECT COUNT(*) as c FROM ordens WHERE status='Em Processo'"
    ).fetchone()["c"]
    concluidas = conn.execute(
        "SELECT COUNT(*) as c FROM ordens WHERE status='Concluido'"
    ).fetchone()["c"]
    pausados = conn.execute(
        "SELECT COUNT(*) as c FROM ordens WHERE status='Pausado'"
    ).fetchone()["c"]
    com_erro = conn.execute(
        "SELECT COUNT(*) as c FROM ordens WHERE status='Erro'"
    ).fetchone()["c"]
    canceladas = conn.execute(
        "SELECT COUNT(*) as c FROM ordens WHERE status='Cancelado'"
    ).fetchone()["c"]
    # CASE explícito, não `prioridade DESC`: o DESC ordenava texto e punha
    # "Alta" atrás de "Baixa" — ver `PRIORIDADES`.
    rows = conn.execute(
        "SELECT * FROM ordens WHERE status IN ('Pendente','Em Processo','Pausado') "
        f"ORDER BY {_sql_ordem_prioridade()}, data_criacao ASC LIMIT 10"
    ).fetchall()
    conn.close()
    ordens_ativas = []
    for r in rows:
        d = dict(r)
        d["itens_lista"] = parse_itens(r)
        d["medicamento"] = itens_resumo(r)
        d["quantidade"] = itens_total_qtd(r)
        ordens_ativas.append(d)
    return jsonify(
        {
            "total": total,
            "pendentes": pendentes,
            "em_processo": em_processo,
            "pausados": pausados,
            "concluidas": concluidas,
            "com_erro": com_erro,
            "canceladas": canceladas,
            "ordens_ativas": ordens_ativas,
        }
    )


@app.route("/api/dispensers/sync", methods=["PUT"])
@api_token_required
def api_sync_dispensers():
    # `silent=True`: sem ele, corpo que não é JSON vira 400 do Werkzeug com uma
    # pagina HTML — numa rota que so fala JSON e cujo cliente e um processo.
    data = request.get_json(silent=True)
    conn = get_db()
    try:
        ok, erro = _sync_dispensers_data(conn, data)
    finally:
        conn.close()
    if not ok:
        # 409 continua para a recusa de SLOT ESPELHADO (conflito de dono);
        # corpo malformado e 400, que e quem errou.
        codigo = 409 if erro == MSG_SLOT_CENTRAL else 400
        return jsonify({"ok": False, "erro": erro}), codigo
    return jsonify({"ok": True})


@app.route("/api/medicamentos")
@api_token_required
def api_medicamentos():
    return jsonify(get_medicamentos_list())


@app.route("/api/dispensers")
@api_token_required
def api_dispensers():
    conn = get_db()
    dispensers = _dispensers_data(conn)
    conn.close()
    return jsonify(dispensers)


@app.route("/api/dispensers/<int:slot>/medicamento", methods=["PUT"])
@api_token_required
def api_atualizar_medicamento_dispenser(slot):
    data = request.get_json()
    nome = data.get("nome", "").strip()
    if not nome:
        return jsonify({"erro": "Nome obrigatorio"}), 400
    conn = get_db()
    try:
        ok, erro = _set_dispenser_med_data(conn, slot, nome)
    finally:
        conn.close()
    if not ok:
        return jsonify({"ok": False, "erro": erro}), 409
    return jsonify({"ok": True})


@app.route("/api/catalogo")
@api_token_required
def api_catalogo():
    conn = get_db()
    catalogo = _catalogo_data(conn)
    conn.close()
    return jsonify(catalogo)


# ============================================================
# Catalogo de Medicamentos (Web CRUD)
# ============================================================
@app.route("/catalogo")
@login_required
@requer("catalogo_ver")
def listar_catalogo():
    conn = get_db()
    itens = conn.execute("SELECT * FROM catalogo ORDER BY nome").fetchall()
    conn.close()
    return render_template("catalogo.html", itens=itens)


@app.route("/catalogo/novo", methods=["POST"])
@login_required
@requer("catalogo_editar")
def novo_catalogo():
    nome = request.form.get("nome", "").strip()
    if nome:
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO catalogo (nome) VALUES (?)", (nome,))
        conn.commit()
        conn.close()
    return redirect(url_for("listar_catalogo"))


@app.route("/catalogo/<int:id>/excluir", methods=["POST"])
@login_required
@requer("catalogo_editar")
def excluir_catalogo(id):
    conn = get_db()
    conn.execute("DELETE FROM catalogo WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("listar_catalogo"))


# ============================================================
# Clientes — quem recebe as ordens/caixas (rastreabilidade de destino)
# ============================================================
@app.route("/clientes")
@login_required
@requer("clientes_ver")
def listar_clientes():
    conn = get_db()
    clientes = conn.execute("SELECT * FROM clientes ORDER BY nome").fetchall()
    # Conta ordens por cliente (match pelo nome, que e o que fica em ordens.destino)
    contagem = {}
    for c in clientes:
        contagem[c["id"]] = conn.execute(
            "SELECT COUNT(*) as n FROM ordens WHERE destino=?", (c["nome"],)
        ).fetchone()["n"]
    conn.close()
    return render_template("clientes.html", clientes=clientes, contagem=contagem)


@app.route("/clientes/novo", methods=["POST"])
@login_required
@requer("clientes_editar")
def novo_cliente():
    nome = request.form.get("nome", "").strip()
    if nome:
        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = get_db()
        conn.execute(
            """INSERT OR IGNORE INTO clientes (nome, cnpj, endereco, contato, telefone, ativo, data_cadastro)
               VALUES (?,?,?,?,?,1,?)""",
            (
                nome,
                request.form.get("cnpj", "").strip(),
                request.form.get("endereco", "").strip(),
                request.form.get("contato", "").strip(),
                request.form.get("telefone", "").strip(),
                agora,
            ),
        )
        conn.commit()
        conn.close()
    return redirect(url_for("listar_clientes"))


@app.route("/clientes/<int:id>/editar", methods=["POST"])
@login_required
@requer("clientes_editar")
def editar_cliente(id):
    nome = request.form.get("nome", "").strip()
    ativo = 1 if request.form.get("ativo") else 0
    if nome:
        conn = get_db()
        conn.execute(
            """UPDATE clientes SET nome=?, cnpj=?, endereco=?, contato=?, telefone=?, ativo=? WHERE id=?""",
            (
                nome,
                request.form.get("cnpj", "").strip(),
                request.form.get("endereco", "").strip(),
                request.form.get("contato", "").strip(),
                request.form.get("telefone", "").strip(),
                ativo, id,
            ),
        )
        conn.commit()
        conn.close()
    return redirect(url_for("listar_clientes"))


@app.route("/clientes/<int:id>/excluir", methods=["POST"])
@login_required
@requer("clientes_editar")
def excluir_cliente(id):
    conn = get_db()
    conn.execute("DELETE FROM clientes WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("listar_clientes"))


# ============================================================
# Admin — Operadores (CRUD)
# ============================================================
@app.route("/admin")
@login_required
def admin_index():
    return redirect(url_for("admin_operadores"))


@app.route("/admin/operadores")
@login_required
@requer("operadores_ver")
def admin_operadores():
    conn = get_db()
    ops = conn.execute(
        "SELECT * FROM operadores ORDER BY perfil, nome"
    ).fetchall()
    total_por_perfil = {}
    for p in PERFIS:
        total_por_perfil[p] = conn.execute(
            "SELECT COUNT(*) as c FROM operadores WHERE perfil=? AND ativo=1", (p,)
        ).fetchone()["c"]
    inativos = conn.execute(
        "SELECT COUNT(*) as c FROM operadores WHERE ativo=0"
    ).fetchone()["c"]
    conn.close()
    return render_template(
        "admin_operadores.html",
        operadores=ops,
        perfis=PERFIS,
        total_por_perfil=total_por_perfil,
        inativos=inativos,
    )


@app.route("/admin/operadores/novo", methods=["POST"])
@login_required
@requer("operadores_editar")
def admin_novo_operador():
    nome = request.form.get("nome", "").strip()
    pin  = request.form.get("pin", "").strip()
    perfil = request.form.get("perfil", "Operador")
    rfid_uid = request.form.get("rfid_uid", "").strip().upper()
    if nome and pin and perfil in PERFIS:
        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = get_db()
        conn.execute(
            "INSERT INTO operadores (nome, pin_hash, perfil, ativo, data_criacao, rfid_uid) VALUES (?,?,?,1,?,?)",
            (nome, gerar_pin_hash(pin), perfil, agora, rfid_uid),
        )
        conn.commit()
        conn.close()
    return redirect(url_for("admin_operadores"))


@app.route("/admin/operadores/<int:id>/editar", methods=["POST"])
@login_required
@requer("operadores_editar")
def admin_editar_operador(id):
    nome   = request.form.get("nome", "").strip()
    pin    = request.form.get("pin", "").strip()
    perfil = request.form.get("perfil", "Operador")
    ativo  = 1 if request.form.get("ativo") else 0
    rfid_uid = request.form.get("rfid_uid", "").strip().upper()
    # PIN em branco MANTEM o que esta gravado. Com hash no lugar do texto, o
    # formulario nao tem mais como vir preenchido com o PIN atual — exigi-lo em
    # toda edicao obrigaria quem so corrige um nome a inventar um PIN novo, e
    # PIN inventado por acaso e PIN anotado no monitor.
    if nome and perfil in PERFIS:
        conn = get_db()
        if pin:
            conn.execute(
                "UPDATE operadores SET nome=?, pin_hash=?, perfil=?, ativo=?, rfid_uid=? WHERE id=?",
                (nome, gerar_pin_hash(pin), perfil, ativo, rfid_uid, id),
            )
        else:
            conn.execute(
                "UPDATE operadores SET nome=?, perfil=?, ativo=?, rfid_uid=? WHERE id=?",
                (nome, perfil, ativo, rfid_uid, id),
            )
        conn.commit()
        conn.close()
    return redirect(url_for("admin_operadores"))


@app.route("/admin/operadores/<int:id>/excluir", methods=["POST"])
@login_required
@requer("operadores_editar")
def admin_excluir_operador(id):
    conn = get_db()
    conn.execute("DELETE FROM operadores WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_operadores"))


# ============================================================
# Admin — Histórico / Audit Log
# ============================================================
@app.route("/admin/historico")
@login_required
@requer("historico_ver")
def admin_historico():
    filtro_perfil = request.args.get("perfil", "")
    filtro_acao   = request.args.get("acao", "")
    filtro_op     = request.args.get("operador", "")

    conn = get_db()
    query  = "SELECT * FROM historico WHERE 1=1"
    params = []
    if filtro_perfil:
        query += " AND perfil=?"
        params.append(filtro_perfil)
    if filtro_acao:
        query += " AND acao LIKE ?"
        params.append(f"%{filtro_acao}%")
    if filtro_op:
        query += " AND operador LIKE ?"
        params.append(f"%{filtro_op}%")
    query += " ORDER BY timestamp DESC LIMIT 300"

    logs  = conn.execute(query, params).fetchall()
    total = conn.execute("SELECT COUNT(*) as c FROM historico").fetchone()["c"]

    # contagem por ação para mini-stats
    acoes_count = conn.execute(
        "SELECT acao, COUNT(*) as c FROM historico GROUP BY acao ORDER BY c DESC LIMIT 6"
    ).fetchall()

    conn.close()
    return render_template(
        "admin_historico.html",
        logs=logs,
        total=total,
        acoes_count=acoes_count,
        perfis=PERFIS,
        filtro_perfil=filtro_perfil,
        filtro_acao=filtro_acao,
        filtro_op=filtro_op,
    )


@app.route("/admin/historico/limpar", methods=["POST"])
@login_required
@csrf_protegido
@requer("historico_limpar")
def admin_limpar_historico():
    conn = get_db()
    conn.execute("DELETE FROM historico")
    conn.commit()
    conn.close()
    return redirect(url_for("admin_historico"))


# ============================================================
# API — Operadores: a rota NAO EXISTE MAIS
# ============================================================
# `GET /api/operadores` devolvia nome, perfil e o PIN em claro de todos os
# operadores ativos, sem autenticacao nenhuma. Como o login web e nome + PIN,
# um `curl` nessa rota entrava como Admin — a rota era, na pratica, a senha de
# administrador publicada em JSON.
#
# Ela existia para o ESP32 buscar a lista por HTTP. O display fala por SERIAL
# desde a migracao, e o que ele pede la (`cmd: get_operadores`) ja nao carrega
# credencial nenhuma; o PIN e conferido por `cmd: validar_operador`. Ou seja: a
# rota nao tinha consumidor, so superficie.
#
# Reintroduzir esta rota, mesmo devolvendo so nome e perfil, e reintroduzir uma
# lista de usuarios legivel — por isso o teste cobra a ausencia dela, e nao o
# formato do corpo.


# ============================================================
# API — Histórico (display ESP32 loga ações aqui)
# ============================================================
@app.route("/api/historico", methods=["POST"])
@api_token_required
def api_registrar_historico():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"erro": "sem dados"}), 400
    operador = data.get("operador", "---")
    acao     = data.get("acao", "")
    detalhes = data.get("detalhes", "")

    conn = get_db()
    op_row = conn.execute(
        "SELECT perfil FROM operadores WHERE nome=? AND ativo=1", (operador,)
    ).fetchone()
    perfil = op_row["perfil"] if op_row else ""
    agora  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO historico (operador, perfil, acao, detalhes, timestamp) VALUES (?,?,?,?,?)",
        (operador, perfil, acao, detalhes, agora),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/historico")
@api_token_required
def api_listar_historico():
    conn = get_db()
    logs = conn.execute(
        "SELECT * FROM historico ORDER BY timestamp DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return jsonify([dict(l) for l in logs])


# ============================================================
# Subida do servidor
# ============================================================
# `debug=True` estava fixo, com `host=0.0.0.0`. O console do Werkzeug que vem
# com ele executa Python arbitrario no processo que possui a porta serial do
# display e o banco da bancada — ou seja, execucao remota atras de um PIN de
# debug, exposta a rede inteira da fabrica. Hoje:
#
#   * debug so com APSEN_DEBUG=1, e o padrao e desligado;
#   * com debug ligado o bind cai para 127.0.0.1, sempre. Depurar da propria
#     maquina e legitimo; abrir o console para a rede nao e, e essa e a unica
#     combinacao que nao tem uso bom.
def _porta_configurada(padrao: int = 5000) -> int:
    """Porta do painel, com o mesmo tratamento de `CENTRAL_TIMEOUT_S`: valor
    invalido cai no padrao COM AVISO, em vez de derrubar o import. O painel e
    iniciado por um `.bat` na bancada, e um erro de digitacao nao pode ser a
    diferenca entre ter e nao ter painel."""
    bruto = os.environ.get("APSEN_PORTA", "")
    if not bruto:
        return padrao
    try:
        return int(bruto)
    except ValueError:
        print(f"[servidor] APSEN_PORTA={bruto!r} nao e numero — usando {padrao}")
        return padrao


PORTA_PADRAO = _porta_configurada()
MODO_DEBUG = os.environ.get("APSEN_DEBUG", "0") == "1"
# 0.0.0.0 continua sendo o padrao SEM debug: a tela do relatorio gera um QR com
# a URL do painel para quem abre do celular ou de outra maquina da bancada.
HOST_PADRAO = os.environ.get("APSEN_HOST", "0.0.0.0")


def servir():
    """Sobe o servidor: waitress quando ele existir, Werkzeug como fallback.

    O `.bat` da bancada chama `python app.py`, e e de proposito que a escolha do
    servidor more AQUI e nao la. `waitress-serve app:app` importaria o modulo
    sem nunca executar `iniciar_workers()`: o painel subiria com a web de pe e
    SEM a ponte serial — display OFFLINE, espelho do central parado, e nada no
    log dizendo o porque. Um entrypoint so, e ele sempre passa pelos workers.
    """
    iniciar_workers()
    if MODO_DEBUG:
        print("[seguranca] APSEN_DEBUG=1 — console do Werkzeug ATIVO, "
              "escutando so em 127.0.0.1")
        app.run(host="127.0.0.1", port=PORTA_PADRAO, debug=True,
                use_reloader=USAR_RELOADER)
        return
    try:
        from waitress import serve as _waitress_serve
    except ImportError:
        print("[servidor] waitress nao instalado (pip install -r requirements.txt) — "
              "subindo com o servidor de desenvolvimento do Flask, sem debug.")
        app.run(host=HOST_PADRAO, port=PORTA_PADRAO, debug=False,
                use_reloader=USAR_RELOADER)
        return
    if USAR_RELOADER:
        # O reloader e do Werkzeug: sob waitress ele nao existe. Avisar aqui
        # evita a meia hora de "editei o arquivo e nada acontece" — quem quer
        # auto-reload quer APSEN_DEBUG=1.
        print("[servidor] APSEN_RELOADER=1 nao vale sob waitress — "
              "use APSEN_DEBUG=1 para desenvolver com auto-reload.")
    print(f"[servidor] waitress em http://{HOST_PADRAO}:{PORTA_PADRAO}")
    _waitress_serve(app, host=HOST_PADRAO, port=PORTA_PADRAO, threads=8)


if __name__ == "__main__":
    servir()
