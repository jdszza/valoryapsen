"""`APSEN_DISPLAY_PORTA`: porta fixa do display, e o fim da varredura.

Com a variável vazia o painel varre todas as portas seriais procurando o ping
do display, e repete a cada 3 s. Na célula montada há CINCO portas e cinco
donos, e cada processo que varre abre as portas DOS OUTROS por até 9,5 s só
para descobrir se a placa dele está ali — enquanto segura a porta da CNC, o
cnc-adapter toma ACCESS_DENIED na própria porta. Fixar a porta elimina a
varredura, e sem varredura não há competição.

O que estes testes prendem: definida, o painel abre SÓ aquela porta e não
enumera as demais; e a porta fixa continua passando pela sondagem de sempre, com
DTR/RTS desligados — fixar o número não dispensa conferir que há um display do
outro lado.

**A varredura virou OPT-IN** (`APSEN_DISPLAY_VARRER=1`), e não sumiu: ela é o
caminho de quem desenvolve sem hardware ou com uma placa só na mesa. O que ela
deixou de ser é o default, porque na bancada montada ela é o problema e não a
conveniência. Sem nenhuma das duas variáveis, o display fica OFFLINE com uma
linha dizendo o que definir — e o painel SOBE assim mesmo, que é a diferença
deliberada para os adapters: eles existem para falar com uma porta, este
processo também serve a tela que o gestor está olhando.
"""
import re


class _Porta:
    def __init__(self, device):
        self.device = device


class _Sonda:
    """Duplo de `_probe_port`: registra as portas sondadas e responde por uma."""

    def __init__(self, responde_em=None):
        self.responde_em = responde_em
        self.sondadas: list = []

    def __call__(self, porta):
        self.sondadas.append(porta)
        return object() if porta == self.responde_em else None


def _instalar(painel, monkeypatch, sonda, portas_do_sistema):
    monkeypatch.setattr(painel.modulo, "_probe_port", sonda)
    enumeracoes = []

    def _comports():
        enumeracoes.append(1)
        return [_Porta(p) for p in portas_do_sistema]

    monkeypatch.setattr(painel.modulo.serial.tools.list_ports, "comports", _comports)
    return enumeracoes


def test_porta_fixa_abre_so_ela_e_nao_varre(carregar_painel, monkeypatch):
    painel = carregar_painel(env={"APSEN_DISPLAY_PORTA": "COM7"})
    sonda = _Sonda(responde_em="COM7")
    enumeracoes = _instalar(painel, monkeypatch, sonda, ["COM3", "COM4", "COM7"])

    conn = painel.modulo.find_display_port()

    assert conn is not None
    assert sonda.sondadas == ["COM7"]
    assert enumeracoes == [], "com a porta fixa, as outras portas nem são listadas"


def test_porta_fixa_sem_display_do_outro_lado_nao_cai_na_varredura(carregar_painel,
                                                                    monkeypatch):
    """Fixar o número não dispensa o ping — mas errar o número também não
    autoriza sair abrindo as portas dos outros processos."""
    painel = carregar_painel(env={"APSEN_DISPLAY_PORTA": "COM9"})
    sonda = _Sonda(responde_em="COM4")
    enumeracoes = _instalar(painel, monkeypatch, sonda, ["COM3", "COM4"])

    assert painel.modulo.find_display_port() is None
    assert sonda.sondadas == ["COM9"]
    assert enumeracoes == []


def test_sem_nenhuma_das_duas_nao_varre_e_diz_o_que_fazer(carregar_painel,
                                                          monkeypatch, capsys):
    """O default mudou: varrer faz este processo abrir as portas dos outros
    quatro, e o sintoma é boot não-determinístico em que uma placa às vezes
    simplesmente não é achada."""
    painel = carregar_painel(env={"APSEN_DISPLAY_PORTA": None,
                                  "APSEN_DISPLAY_VARRER": None})
    sonda = _Sonda(responde_em="COM4")
    enumeracoes = _instalar(painel, monkeypatch, sonda, ["COM3", "COM4"])

    assert painel.modulo.find_display_port() is None
    assert enumeracoes == []
    assert sonda.sondadas == []
    assert "APSEN_DISPLAY_PORTA" in capsys.readouterr().out


def test_varredura_religada_volta_a_ser_a_de_antes(carregar_painel, monkeypatch):
    painel = carregar_painel(env={"APSEN_DISPLAY_PORTA": None,
                                  "APSEN_DISPLAY_VARRER": "1"})
    sonda = _Sonda(responde_em="COM4")
    enumeracoes = _instalar(painel, monkeypatch, sonda, ["COM3", "COM4", "COM5"])

    conn = painel.modulo.find_display_port()

    assert conn is not None
    assert enumeracoes == [1]
    assert sonda.sondadas == ["COM3", "COM4"]        # para na primeira que responde


def test_variavel_em_branco_conta_como_ausente(carregar_painel, monkeypatch):
    painel = carregar_painel(env={"APSEN_DISPLAY_PORTA": "   ",
                                  "APSEN_DISPLAY_VARRER": "1"})
    sonda = _Sonda(responde_em="COM3")
    enumeracoes = _instalar(painel, monkeypatch, sonda, ["COM3"])

    assert painel.modulo.find_display_port() is not None
    assert enumeracoes == [1]


def test_a_variavel_e_lida_a_cada_tentativa(carregar_painel, monkeypatch):
    """O `.bat` da bancada é reiniciado com frequência, mas o `serial_worker`
    reprocura a cada 3 s: a porta fixada depois do boot vale na tentativa
    seguinte, sem reiniciar o painel."""
    painel = carregar_painel(env={"APSEN_DISPLAY_PORTA": None,
                                  "APSEN_DISPLAY_VARRER": "1"})
    sonda = _Sonda(responde_em="COM7")
    enumeracoes = _instalar(painel, monkeypatch, sonda, ["COM3"])

    painel.modulo.find_display_port()
    monkeypatch.setenv("APSEN_DISPLAY_PORTA", "COM7")
    painel.modulo.find_display_port()

    assert enumeracoes == [1]
    assert sonda.sondadas == ["COM3", "COM7"]


def test_a_porta_fixa_passa_pela_mesma_sondagem_com_dtr_rts_desligados(carregar_painel):
    """`find_display_port` chama `_probe_port` também para a porta fixa — e é
    `_probe_port` que desliga DTR/RTS antes de abrir (o circuito de reset do
    ESP32-S3). Um `serial.Serial(porta)` direto reintroduziria o ciclo de boot
    infinito que o comentário de lá descreve."""
    import inspect

    painel = carregar_painel()
    fonte = inspect.getsource(painel.modulo.find_display_port)
    assert "_probe_port(fixa)" in fonte
    assert "serial.Serial(" not in fonte

    sonda = inspect.getsource(painel.modulo._probe_port)
    assert re.search(r"conn\.dtr\s*=\s*False", sonda)
    assert re.search(r"conn\.rts\s*=\s*False", sonda)
