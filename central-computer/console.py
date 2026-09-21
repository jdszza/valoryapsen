"""
APSEN — Console de operação: sessão, senha e o flag de pausa do gerador.

Este módulo é a parte SEM FastAPI do console: assinatura do cookie, conferência
da senha, freio de força bruta e o interruptor do erp-simulator. As rotas
ficam em `main.py`, junto do resto da API — o que mora aqui é o que dá para
testar chamando função, sem subir aplicação.

Por que uma senha própria, e não o JWT do sistema
─────────────────────────────────────────────────
O console é a mesa de quem opera a planta numa apresentação: escolhe qual das
dez ordens padrão entra, pausa o automático, libera a trava. Amarrá-lo ao login
do app de manutenção teria dois efeitos ruins e nenhum bom: exigiria abrir o app
que ele existe justamente para não precisar abrir, e criaria mais uma conta com
poder de disparar OS — conta que sobrevive à apresentação e que ninguém lembra
de desativar. `CONSOLE_SENHA` é uma porta só, que se fecha apagando a variável.

**Sem `CONSOLE_SENHA`, o console não existe**: toda rota `/console*` responde
503. Nunca uma senha default embutida — ela estaria neste arquivo, versionada,
e abriria o disparo de OS para quem lesse o repositório.

Por que 503 e não 404
─────────────────────
As duas escondem o console de quem não tem a senha, e nenhuma das duas o abre —
então a escolha se decide pelo outro leitor, o operador que configurou errado.
404 manda essa pessoa caçar o problema na URL, no build ou no proxy; 503 com
"defina CONSOLE_SENHA" diz o que fazer numa linha. O que um atacante ganha com
a diferença é saber que existe um console — o que o `/docs` do central e este
repositório já contam —, e continuar sem a senha do mesmo jeito.

A sessão é um HMAC, não uma tabela
──────────────────────────────────
Não há segundo sistema de usuários: não existe usuário. O cookie carrega apenas
o instante de expiração e um HMAC-SHA256 desse instante. Sem estado no servidor,
a sessão sobrevive a restart do central — e some quando a senha muda, porque a
chave de assinatura é derivada de `SECRET_KEY` **e** de `CONSOLE_SENHA`: trocar
a senha invalida todo cookie emitido, que é o que se espera ao trocar uma senha.

`itsdangerous` faria o mesmo; `hmac` e `hashlib` são stdlib, e a task pede para
não acrescentar dependência ao central sem necessidade real.

O flag de pausa mora aqui, e é de memória
─────────────────────────────────────────
O erp-simulator é outro container. Fazer o central pará-lo exigiria falar com
o daemon do Docker — socket montado no container, permissão de administrador da
máquina, e um acoplamento novo entre o central e o runtime que o hospeda, tudo
para não dispensar medicamento por alguns minutos. O caminho barato é o que já
existe entre os dois: HTTP. O central publica um booleano em
`GET /api/v1/gerador`, o gerador o consulta antes de cada envio, e o container
segue de pé — o que também significa que ele volta a produzir no instante em que
o console despausa, sem esperar orquestração de container.

A pausa **não é persistida**, e isso é escolha: restart do central retoma o
automático. O modo default do sistema é gerar OS; uma pausa gravada em banco
sobreviveria à apresentação que a motivou, e o sintoma — planta em silêncio,
fila vazia, nenhum erro em lugar nenhum — é dos piores de diagnosticar. Retomar
sozinho erra para o lado visível.
"""
import hashlib
import hmac
import logging
import threading
import time
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

COOKIE_SESSAO = "apsen_console"
# O cookie só é enviado nas rotas do console. `/ws`, `/estado` e o resto da API
# não o recebem nem precisam dele — nada mais no central lê este cookie.
COOKIE_PATH = "/console"

_DIR = Path(__file__).resolve().parent
_PAGINA_CONSOLE = _DIR / "console.html"
_PAGINA_LOGIN   = _DIR / "console_login.html"
_PAGINA_PREVOO  = _DIR / "console_prevoo.html"

# Freio de força bruta: a porta é uma senha só, sem usuário e sem segundo
# fator. Uma janela curta com poucas tentativas não atrapalha quem digitou
# errado e derruba a taxa de quem varre.
#
# O freio serve DUAS portas: este console e o `POST /auth/login` do central,
# que também não tinha nenhum — sendo que é ele quem emite o JWT de `admin`. As
# funções nunca souberam o que é `origem`, então compartilhar não custou
# parâmetro nenhum: o console usa o IP e o login usa `login:{ip}|{username}`,
# baldes separados no mesmo dicionário. Um segundo contador para a mesma regra
# divergiria no primeiro ajuste de janela, e a metade esquecida seria a que
# ninguém está olhando.
MAX_TENTATIVAS = 5
JANELA_TENTATIVAS_S = 60.0

_tentativas: dict[str, list[float]] = {}
_tentativas_lock = threading.Lock()

# Interruptor do erp-simulator. Escrito só por `definir_pausa`, lido por
# `GET /api/v1/gerador` (o gerador) e publicado em `_estado` (o console).
_pausado = False
_pausado_desde: float | None = None
_pausa_lock = threading.Lock()


# ── Disponibilidade ───────────────────────────────────────────────────────────

def habilitado() -> bool:
    """`CONSOLE_SENHA` definida e não vazia. Falso = todas as rotas dão 503."""
    return bool(settings.CONSOLE_SENHA)


def motivo_indisponivel() -> str:
    return ("Console de operação desabilitado: defina CONSOLE_SENHA no .env e "
            "reinicie o central-computer.")


# ── Senha ─────────────────────────────────────────────────────────────────────

def senha_confere(candidata: str) -> bool:
    """Comparação em tempo constante. Console desabilitado nunca confere.

    `compare_digest` e não `==`: a comparação ingênua sai no primeiro byte
    diferente, e o tempo de resposta passa a contar quantos caracteres do
    palpite estavam certos.
    """
    if not habilitado():
        return False
    return hmac.compare_digest((candidata or "").encode("utf-8"),
                               settings.CONSOLE_SENHA.encode("utf-8"))


# ── Sessão assinada ───────────────────────────────────────────────────────────

def _chave_assinatura() -> bytes:
    """Chave do HMAC: `SECRET_KEY` + `CONSOLE_SENHA`.

    A senha entra na chave para que trocá-la derrube as sessões abertas. Sem
    isso, revogar o acesso de alguém exigiria também trocar a `SECRET_KEY` —
    que é a chave dos JWT do sistema inteiro, e derrubaria os técnicos junto.
    """
    material = f"{settings.SECRET_KEY}|{settings.CONSOLE_SENHA}".encode("utf-8")
    return hashlib.sha256(material).digest()


def _assinar(expira_em: int) -> str:
    return hmac.new(_chave_assinatura(), str(expira_em).encode("ascii"),
                    hashlib.sha256).hexdigest()


def criar_sessao(agora: float | None = None) -> str:
    """Valor do cookie: `{expiração unix}.{HMAC da expiração}`.

    Nada além da expiração vai no cookie porque não há nada mais: o console tem
    uma porta, não usuários. O que o valor precisa provar é só "alguém digitou
    a senha, e faz menos de `CONSOLE_SESSAO_HORAS`".
    """
    expira_em = int((agora if agora is not None else time.time())
                    + settings.CONSOLE_SESSAO_HORAS * 3600)
    return f"{expira_em}.{_assinar(expira_em)}"


def sessao_valida(cookie: str | None, agora: float | None = None) -> bool:
    """Assinatura íntegra e prazo não vencido. Console desabilitado: nunca."""
    if not habilitado() or not cookie:
        return False
    bruto, _, assinatura = cookie.partition(".")
    if not assinatura:
        return False
    try:
        expira_em = int(bruto)
    except ValueError:
        return False
    if not hmac.compare_digest(assinatura, _assinar(expira_em)):
        return False
    return (agora if agora is not None else time.time()) < expira_em


def duracao_cookie_s() -> int:
    return int(settings.CONSOLE_SESSAO_HORAS * 3600)


# ── Freio de força bruta ──────────────────────────────────────────────────────

def registrar_falha(origem: str, agora: float | None = None) -> None:
    momento = agora if agora is not None else time.monotonic()
    with _tentativas_lock:
        # Varre TODOS os baldes, não só o desta origem. A chave é
        # `ip|username`, ou seja, ela é escolhida por quem tenta: uma varredura
        # com um username novo a cada tentativa criava uma entrada que ninguém
        # mais consultaria — `bloqueado` só limpa a chave que recebe —, e o
        # dicionário crescia sem teto num processo que não reinicia.
        #
        # O custo é O(nº de baldes) por tentativa FALHA, e é ele mesmo que
        # mantém o número pequeno: sobrevive aqui só o que tentou nos últimos
        # `JANELA_TENTATIVAS_S`. Login que dá certo não passa por aqui.
        for chave in [k for k, ts in _tentativas.items()
                      if all(momento - t >= JANELA_TENTATIVAS_S for t in ts)]:
            del _tentativas[chave]

        recentes = [t for t in _tentativas.get(origem, [])
                    if momento - t < JANELA_TENTATIVAS_S]
        recentes.append(momento)
        _tentativas[origem] = recentes


def limpar_falhas(origem: str) -> None:
    """Login bem-sucedido zera o contador — quem sabe a senha não é varredura."""
    with _tentativas_lock:
        _tentativas.pop(origem, None)


def bloqueado(origem: str, agora: float | None = None) -> float:
    """Segundos que faltam para a origem poder tentar de novo. 0 = liberada."""
    momento = agora if agora is not None else time.monotonic()
    with _tentativas_lock:
        recentes = [t for t in _tentativas.get(origem, [])
                    if momento - t < JANELA_TENTATIVAS_S]
        # Balde que esvaziou é REMOVIDO, e não regravado vazio. A chave é
        # `ip|username`, ou seja, ela é escolhida por quem tenta: uma varredura
        # com um username diferente por tentativa deixava uma entrada
        # permanente por tentativa, e o dicionário crescia sem teto num
        # processo que não reinicia. Custa um `pop` e fecha o caminho.
        if recentes:
            _tentativas[origem] = recentes
        else:
            _tentativas.pop(origem, None)
        if len(recentes) < MAX_TENTATIVAS:
            return 0.0
        return max(JANELA_TENTATIVAS_S - (momento - min(recentes)), 1.0)


# ── Pausa do erp-simulator ──────────────────────────────────────────────────

def gerador_status() -> dict:
    """O que `GET /api/v1/gerador` devolve — o gerador só lê `pausado`."""
    with _pausa_lock:
        return {
            "pausado": _pausado,
            "desde":   _pausado_desde,
        }


def definir_pausa(pausado: bool) -> dict:
    """Liga/desliga a pausa. Devolve o status novo, já para publicar em `_estado`."""
    global _pausado, _pausado_desde
    with _pausa_lock:
        mudou = pausado != _pausado
        _pausado = bool(pausado)
        _pausado_desde = time.time() if _pausado else None
        estado = {"pausado": _pausado, "desde": _pausado_desde}
    if mudou:
        logger.warning("[CONSOLE] Emissão automática de OS %s pelo console.",
                       "PAUSADO" if pausado else "RETOMADO")
    return estado


def resetar_pausa() -> None:
    """Só para teste: devolve o interruptor ao estado de boot."""
    global _pausado, _pausado_desde
    with _pausa_lock:
        _pausado = False
        _pausado_desde = None


# ── Páginas ───────────────────────────────────────────────────────────────────
#
# Lidas do disco a cada requisição. O console tem um operador, não tráfego: o
# custo é irrelevante e em troca editar o HTML vale sem reiniciar o container —
# que é exatamente o que se faz na véspera de uma apresentação.

def pagina_console() -> str:
    return _PAGINA_CONSOLE.read_text(encoding="utf-8")


def pagina_prevoo() -> str:
    """Tela de conferência pré-apresentação. Mesma sessão do console."""
    return _PAGINA_PREVOO.read_text(encoding="utf-8")


def pagina_login(erro: str = "") -> str:
    """Tela de senha. `erro` é INJETADO ESCAPADO — nunca ecoa o que foi digitado.

    O texto vem sempre de uma constante do `main.py`; o escape existe para que
    isso continue verdade se um dia alguém montar a mensagem com dado de fora.
    """
    seguro = (erro.replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))
    bloco = f'<p class="erro" role="alert">{seguro}</p>' if seguro else ""
    return _PAGINA_LOGIN.read_text(encoding="utf-8").replace("<!--ERRO-->", bloco)
