"""
APSEN — Seed de histórico de DEMONSTRAÇÃO.

⚠️  DADO FABRICADO. Nada aqui aconteceu na planta.

Estas ordens, dispensas, alarmes e leituras existem para que o dashboard e o app
de manutenção tenham FORMA numa instalação recém-criada. Um banco novo abre com
todas as telas vazias — sem histórico de OS, sem alarme resolvido, sem curva de
temperatura —, e a leitura natural de quem vê isso é "o sistema nunca rodou", o
que é exatamente a impressão errada a dar numa apresentação.

Três regras, e as três são de segurança, não de estilo:

1. **Nunca roda sozinho.** Não há chamada no `init_db`, no `lifespan` nem em
   healthcheck nenhum. Só por ação manual no console, atrás da mesma
   confirmação do reset. Um seed que rodasse no boot transformaria o primeiro
   `docker compose up` de uma instalação de verdade em dado inventado no banco
   de produção.

2. **O dado se identifica.** Todo `os_id` daqui começa com `DEMO-`
   (`e_demo()`), então ele se distingue de uma OS real em qualquer tela, em
   qualquer log e em qualquer consulta SQL — sem depender de olhar a data. Isso
   também torna impossível colidir com uma OS real: nenhum `template_id`
   começa com esse prefixo.

3. **A geração é PURA.** Este módulo não abre conexão, não importa `database` e
   não conhece SQL: devolve listas de dicionários. Quem grava é
   `database.semear_historico_demo`, onde o `tests/test_schema.py` enxerga as
   queries. A separação é a mesma de `os_templates.py` (decide) × `database.py`
   (persiste), e é o que permite testar a coerência do histórico — quantidades
   que batem com os templates, datas dentro da janela, horário comercial — sem
   MySQL nenhum.

Coerência do que é gerado
─────────────────────────
As OS saem das DEZ ordens padrão (`os_templates.TEMPLATES`), com SKU e categoria
resolvidos do catálogo real, exatamente como um disparo de verdade. As dispensas
carregam as quantidades dos itens do template. Uma fração das OS termina em
`erro`, com um item curto e um alarme correspondente — porque um histórico em
que nada deu errado é tão pouco convincente quanto um em que nada aconteceu.

As leituras de sensor seguem um ciclo diário (aquece durante o turno, esfria à
noite) com deriva lenta e ruído pequeno. Ruído puro não desenha nada e linha
reta denuncia o dado fabricado; o que se quer no gráfico é forma.
"""
import math
import random
import uuid
from datetime import datetime, timedelta, timezone

import os_templates

# Prefixo que marca TODA linha vinda daqui. Ver a regra 2 do cabeçalho.
PREFIXO_DEMO = "DEMO-"

# Faixas aceitas. Fora delas o pedido é recusado em vez de ajustado: quem pede
# 10 000 ordens de demonstração digitou errado, e semear isso enche a tabela que
# o `_loop_expurgo` só vai limpar daqui a `RETENCAO_DIAS`.
MIN_ORDENS, MAX_ORDENS = 1, 200
MIN_DIAS,   MAX_DIAS   = 1, 60

# Turno de trabalho das OS fabricadas. Ordem de dispensação de madrugada existe
# em hospital, mas aqui o objetivo é o oposto do realismo máximo: é que a
# distribuição por hora no gráfico tenha a cara de uma operação em turno.
HORA_INICIO, HORA_FIM = 7, 19

# Intervalo entre duas OS do mesmo dia, em minutos. A faixa é larga de propósito
# — intervalo constante vira serrilha perfeita no gráfico, que é outra forma de
# denunciar o dado fabricado.
INTERVALO_MIN_MIN, INTERVALO_MAX_MIN = 18, 95

# Fração das OS que termina em erro. Não é chute: a seção "Triple Check" do
# CLAUDE.md registra 2 de 5 OS travando com `PROB_ERRO_MECANICO=0.01`, mas
# travar não é terminar em erro — a maioria das travas é liberada e a OS
# conclui. 12% é a ordem de grandeza de quanto sobra como abort de verdade.
FRACAO_ERRO = 0.12

# Componentes que ganham série de temperatura, com a faixa de cada um. Os nomes
# são os MESMOS que os simuladores emitem: uma série sob outro nome não
# apareceria em tela nenhuma, porque o app de manutenção consulta
# `/manutencao/sensores/{componente}` pelo nome que está no banco.
COMPONENTES_CNC = [
    ("motor_eixo_x", 35.0, 55.0),
    ("motor_eixo_y", 33.0, 50.0),
    ("driver_x",     40.0, 70.0),
    ("driver_y",     38.0, 68.0),
    ("placa_cnc",    45.0, 65.0),
]
COMPONENTES_VISAO = [
    ("camera_dispenser_esq", 35.0, 50.0),
    ("camera_dispenser_dir", 35.0, 50.0),
    ("camera_mesa",          33.0, 48.0),
    ("processador_visao",    45.0, 70.0),
]
COMPONENTE_BALANCA = ("hx711_balanca_mesa", 24.0, 33.0)

# Uma leitura por componente a cada 30 min. Mais denso que isto multiplica
# linhas sem acrescentar forma ao gráfico — e `leituras_sensores` é justamente
# a tabela que o expurgo existe para conter.
PASSO_LEITURA_MIN = 30

# Alarmes de demonstração: tipos que o sistema realmente emite, com a fonte no
# formato que `_camera_dispenser` e o orquestrador produzem.
_ALARMES_DEMO = [
    ("camera_dispenser_esq_{slot}", "leitura_dispenser_falha",
     "Falha de leitura no dispenser D{slot} — código danificado."),
    ("camera_dispenser_dir_{slot}", "leitura_dispenser_falha",
     "Falha de leitura no dispenser D{slot} — iluminação insuficiente."),
    ("camera_mesa", "leitura_mesa_divergencia",
     "Contagem da câmera da mesa divergiu no slot D{slot}."),
    ("balanca", "peso_divergencia",
     "Peso fora da tolerância no slot D{slot}."),
    ("dispenser_{slot}", "erro_mecanico_carga",
     "Atolamento mecânico durante o carregamento do D{slot}."),
]


class SeedInvalido(ValueError):
    """Pedido de seed fora das faixas — vira 422, não 500."""


def e_demo(os_id: str) -> bool:
    """Esta OS veio do seed de demonstração?"""
    return bool(os_id) and os_id.startswith(PREFIXO_DEMO)


def _novo_os_id(template_id: str, momento: datetime, rnd: random.Random) -> str:
    """`DEMO-` + o mesmo formato de um disparo real.

    Reaproveita `os_templates.novo_os_id` em vez de montar a string aqui: o
    formato do id tem dono, e uma terceira grafia dele seria a primeira a ficar
    para trás. O prefixo é o que separa demonstração de operação.
    """
    sufixo = uuid.UUID(int=rnd.getrandbits(128)).hex[:6]
    return PREFIXO_DEMO + os_templates.novo_os_id(template_id, momento, sufixo)


def _momentos_das_ordens(n: int, dias: int, agora: datetime,
                         rnd: random.Random) -> list[datetime]:
    """Instantes de criação, do mais antigo ao mais recente.

    Caminha para TRÁS a partir de agora, pulando para o dia anterior quando o
    horário cai fora do turno. Distribuir uniformemente na janela daria dias
    idênticos; assim os dias têm cargas diferentes, como uma operação real.
    """
    momentos: list[datetime] = []
    limite = agora - timedelta(days=dias)
    cursor = agora.replace(minute=rnd.randrange(0, 60), second=0, microsecond=0)

    while len(momentos) < n:
        cursor -= timedelta(minutes=rnd.randint(INTERVALO_MIN_MIN, INTERVALO_MAX_MIN))
        if cursor < limite:
            # Acabou a janela e ainda faltam ordens: recomeça do topo. Melhor
            # adensar os mesmos dias do que estourar o intervalo que o operador
            # pediu — a janela é o que ele enxerga no filtro da tela.
            cursor = agora
            continue
        if not HORA_INICIO <= cursor.hour < HORA_FIM:
            # Pula para o fim do turno do dia anterior.
            cursor = (cursor - timedelta(days=1)).replace(
                hour=HORA_FIM - 1, minute=rnd.randrange(0, 60))
            continue
        momentos.append(cursor)

    return sorted(momentos)


def _duracao_os(n_itens: int, rnd: random.Random) -> timedelta:
    """Quanto uma OS de `n_itens` slots leva, em segundos.

    A conta é a do ciclo real: carga em paralelo mais, por slot, CNC + dispensa
    + câmera da mesa + pesagem. O número não precisa ser exato — precisa é
    CRESCER com o número de itens, senão a OS de 8 slots e a de 2 apareceriam
    com a mesma duração no relatório de tempo de ciclo.
    """
    return timedelta(seconds=int(25 + n_itens * rnd.uniform(14.0, 22.0)))


def gerar_historico(n_ordens: int, catalogo: dict | None = None, *,
                    dias: int = 7, agora: datetime | None = None,
                    semente: int | None = None) -> dict:
    """Monta o histórico de demonstração. NÃO grava nada.

    `catalogo` é `{nome: {"sku": ..., "categoria": ...}}`, o mesmo formato que
    `os_templates.instanciar` consome — assim o SKU do histórico é o SKU real do
    catálogo, e não uma string inventada que não casaria com nenhum medicamento.

    `semente` fixa o sorteio: existe para o teste poder afirmar o mesmo conteúdo
    duas vezes, não para a demonstração ser sempre igual.
    """
    if not MIN_ORDENS <= n_ordens <= MAX_ORDENS:
        raise SeedInvalido(
            f"n_ordens deve estar entre {MIN_ORDENS} e {MAX_ORDENS} "
            f"(recebido {n_ordens})."
        )
    if not MIN_DIAS <= dias <= MAX_DIAS:
        raise SeedInvalido(
            f"dias deve estar entre {MIN_DIAS} e {MAX_DIAS} (recebido {dias})."
        )

    rnd = random.Random(semente)
    momento_final = agora or datetime.now(timezone.utc)
    templates = os_templates.listar()

    ordens, itens, dispensas, alarmes = [], [], [], []

    for criado_em in _momentos_das_ordens(n_ordens, dias, momento_final, rnd):
        template = rnd.choice(templates)
        corpo = os_templates.instanciar(template, catalogo, criado_em)
        os_id = _novo_os_id(template["template_id"], criado_em, rnd)
        corpo["os_id"] = os_id

        medicamentos = corpo["medicamentos"]
        com_erro = rnd.random() < FRACAO_ERRO
        # O item que falhou. Escolhido ANTES do laço para que a dispensa curta,
        # o alarme e o status da OS contem a MESMA história — histórico em que
        # a OS está "erro" e todas as dispensas fecharam certo é pior que
        # histórico nenhum: ele ensina a ler o painel errado.
        idx_falho = rnd.randrange(len(medicamentos)) if com_erro else -1

        concluida_em = criado_em + _duracao_os(len(medicamentos), rnd)
        ordens.append({
            "os_id":        os_id,
            "descricao":    corpo["descricao"],
            "categoria":    corpo.get("categoria", ""),
            "status":       "erro" if com_erro else "concluida",
            "payload_json": corpo,
            "criado_em":    criado_em,
            "concluida_em": concluida_em,
        })

        for i, med in enumerate(medicamentos):
            slot = i + 1
            alvo = med["quantidade"]
            real = max(0, alvo - 1) if i == idx_falho else alvo
            itens.append({
                "os_id":           os_id,
                "dispenser_id":    slot,
                "medicamento":     med["medicamento"],
                "sku":             med.get("sku", ""),
                "categoria":       med.get("categoria", ""),
                "quantidade_alvo": alvo,
                "quantidade_real": real,
                "status":          "erro" if i == idx_falho else "concluido",
            })
            dispensas.append({
                "os_id":                 os_id,
                "dispenser_id":          slot,
                "medicamento":           med["medicamento"],
                "quantidade_dispensada": real,
                "quantidade_alvo":       alvo,
                "validado":              i != idx_falho,
                "motivo_falha":          ("Triple Check: divergência de contagem"
                                          if i == idx_falho else None),
                # Uma dispensa por slot, espaçadas dentro da janela da OS —
                # é o que dá curva ao relatório de tempo de ciclo.
                "ts": criado_em + (concluida_em - criado_em) * ((i + 1) / len(medicamentos)),
            })

        if com_erro:
            modelo = rnd.choice(_ALARMES_DEMO)
            slot_alarme = idx_falho + 1
            alarmes.append({
                "fonte":     modelo[0].format(slot=slot_alarme),
                "tipo":      modelo[1],
                "descricao": modelo[2].format(slot=slot_alarme),
                # Alarme de OS antiga já foi tratado; o painel de necessidades
                # só deve mostrar o que ainda precisa de alguém. Deixar tudo
                # aberto faria o badge nascer com dezenas de pendências falsas.
                "resolvido": True,
                "ts":        concluida_em,
            })

    return {
        "ordens":    ordens,
        "itens":     itens,
        "dispensas": dispensas,
        "alarmes":   alarmes,
        "leituras":  gerar_leituras(dias, momento_final, rnd),
    }


def gerar_leituras(dias: int, agora: datetime,
                   rnd: random.Random | None = None,
                   num_slots: int = 8) -> list[dict]:
    """Séries de temperatura com FORMA: ciclo diário + deriva + ruído.

    A componente diária é o que faz o gráfico contar alguma coisa — a bancada
    aquece durante o turno e esfria à noite. A deriva lenta dá a inclinação que
    um relatório de manutenção procura. O ruído é pequeno de propósito: ruído
    grande esconde as duas primeiras, e aí o gráfico volta a não dizer nada.
    """
    rnd = rnd or random.Random()
    componentes = (
        [(f"dispenser_{i}", 22.0 + i * 0.5, 30.0 + i * 0.5)
         for i in range(1, num_slots + 1)]
        + COMPONENTES_CNC + COMPONENTES_VISAO + [COMPONENTE_BALANCA]
    )

    inicio = agora - timedelta(days=dias)
    passos = int(dias * 24 * 60 / PASSO_LEITURA_MIN)
    leituras: list[dict] = []

    for nome, t_min, t_max in componentes:
        amplitude = (t_max - t_min) / 2.0
        base      = t_min + amplitude
        # Deriva: cada componente aquece ou esfria um pouco ao longo da janela,
        # em direção e intensidade próprias. Todos derivando junto pareceria
        # falha de ambiente, e todos estáveis, um dado sintético.
        deriva_total = rnd.uniform(-0.35, 0.55) * amplitude
        # Defasagem PEQUENA — cerca de ±1 hora. Ela existe para que os
        # componentes não aqueçam todos no mesmo minuto (o que denuncia o dado
        # sintético), mas o pico tem que continuar caindo no turno: uma fase
        # grande jogaria o máximo para a madrugada, e aí a série teria forma
        # sem ter SENTIDO.
        fase = rnd.uniform(-0.04, 0.04)

        for passo in range(passos):
            ts = inicio + timedelta(minutes=passo * PASSO_LEITURA_MIN)
            # Hora do dia em fração de volta. Pico no meio do turno.
            fracao_dia = (ts.hour * 60 + ts.minute) / 1440.0
            ciclo = math.sin(2 * math.pi * (fracao_dia - 0.25 + fase))
            valor = (base
                     + amplitude * 0.62 * ciclo
                     + deriva_total * (passo / max(1, passos - 1))
                     + rnd.gauss(0.0, amplitude * 0.06))
            leituras.append({
                "componente": nome,
                "tipo":       "temperatura",
                "valor":      round(valor, 2),
                "unidade":    "°C",
                "ts":         ts,
            })

    return leituras
