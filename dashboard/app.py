"""
APSEN - Dashboard de Monitoramento v2.0
========================================
Aplicação Plotly Dash de leitura EXCLUSIVA — sem autenticação, sem controle.

Exibe em tempo real:
  • OS ativa: medicamentos, progresso por dispenser
  • CNC: posição XY, status, ciclo atual
  • Dispensers: fase (carregamento / dispensa), progresso, validação IA
  • Atribuição da IA para a OS atual
  • Alarmes ativos
  • Log de eventos recentes
  • Histórico de OS concluídas

Atualização: polling a cada 2 segundos via dcc.Interval.
Backend: GET http://backend:8000/estado + endpoints REST individuais.
"""

import os

import dash
import requests
from dash import Input, Output, callback, dcc, html
import dash_bootstrap_components as dbc

BACKEND_URL = os.getenv("BACKEND_URL", "http://central-computer:8000")
POLL_MS     = int(os.getenv("POLL_MS", "2000"))

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.DARKLY],
    title="APSEN — Monitoramento",
    update_title=None,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get(endpoint: str) -> dict | list:
    try:
        r = requests.get(f"{BACKEND_URL}{endpoint}", timeout=3)
        return r.json()
    except Exception:
        return {}


def _badge(label: str, color: str = "secondary") -> dbc.Badge:
    return dbc.Badge(label, color=color, className="ms-1")


def _kpi(label: str, valor, unidade: str = "", tom: str = "",
         dica: str = "") -> html.Div:
    """Bloco da faixa de indicadores no topo.

    `tom` é uma das classes de severidade do tema (`is-gold`, `is-blue`,
    `is-ok`, `is-warn`, `is-err`) e pinta só o filete lateral — o valor fica
    legível em qualquer caso. `dica` vira `title`, para o valor que aparece
    abreviado ainda poder ser lido por inteiro.
    """
    return html.Div(
        [
            html.Span(label, className="ap-kpi-label"),
            html.Div(
                [str(valor), html.Span(unidade, className="unit") if unidade else ""],
                className="ap-kpi-value",
            ),
        ],
        className=f"ap-kpi {tom}".strip(),
        title=dica or None,
    )


def _vazio(icone: str, texto: str) -> html.Div:
    """Estado vazio: um "sem dados" que ocupa o cartão em vez de uma linha solta."""
    return html.Div(
        [html.Span(icone, className="ico"), html.Span(texto)],
        className="ap-empty",
    )


def _hora(ts, com_data: bool = False) -> str:
    """`2026-08-18T14:03:07` → `14:03:07` (ou `08-18 14:03` com data).

    O log mostra dezenas de linhas por tela e a data se repete em todas; ela só
    aparece onde a linha pode ser de outro dia (histórico de OS).
    """
    s = str(ts or "").replace("T", " ")
    if len(s) < 19:
        return s[:16]
    return f"{s[5:10]} {s[11:16]}" if com_data else s[11:19]


STATUS_COR = {
    "idle":                     "secondary",
    "aguardando_atribuicao":    "info",
    "aguardando_abastecimento": "warning",
    "iniciando":                "primary",
    "movendo":                  "primary",
    "posicionado":              "success",
    "retornando":               "info",
    "concluido":                "success",
    "erro":                     "danger",
    "carregando":               "warning",
    "carregando_iniciado":      "warning",
    "carregando_ok":            "success",
    "carregando_erro":          "danger",
    "pronto":                   "success",
    "dispensando":              "primary",
    "aguardando":               "secondary",
    "em_andamento":             "primary",
    "em_operacao":              "primary",
    "atribuicao_recebida":      "info",
}


def _cor(status: str) -> str:
    return STATUS_COR.get(str(status).lower(), "secondary")


# ── Layout ─────────────────────────────────────────────────────────────────────

app.layout = dbc.Container(
    fluid=True,
    children=[
        dcc.Interval(id="poll", interval=POLL_MS, n_intervals=0),
        dcc.Store(id="store-estado"),
        dcc.Store(id="store-eventos"),
        dcc.Store(id="store-os"),
        dcc.Store(id="store-alarmes"),

        # ── Header ────────────────────────────────────────────────────────────
        html.Div(
            [
                html.Div("AP", className="ap-logo"),
                html.Div([
                    html.H1("APSEN", className="ap-title"),
                    html.Span("Sistema de Contagem de Medicamentos",
                              className="ap-subtitle"),
                ]),
                # `ms-auto` empurra o bloco de status para a direita — a marca
                # fica à esquerda em qualquer largura, sem `float`.
                html.Span(id="badge-alarmes", className="ms-auto"),
            ],
            className="ap-header",
        ),

        # ── Faixa de indicadores ──────────────────────────────────────────────
        html.Div(id="kpi-strip", className="ap-kpis"),

        # ── Row 1: OS ativa + CNC ─────────────────────────────────────────────
        dbc.Row([
            dbc.Col(dbc.Card([
                dbc.CardHeader([
                    html.Span("📋 Ordem de Saída Ativa"),
                    html.Span(id="badge-os-status", className="float-end"),
                ]),
                dbc.CardBody(id="card-os"),
            ]), md=6, className="mb-3"),

            dbc.Col(dbc.Card([
                dbc.CardHeader("🤖 Mesa CNC"),
                dbc.CardBody(id="card-cnc"),
            ]), md=6, className="mb-3"),
        ], className="mt-3"),

        # ── Row 2: Dispensers ─────────────────────────────────────────────────
        dbc.Row(
            dbc.Col(dbc.Card([
                dbc.CardHeader("💊 Dispensers (6 unidades)"),
                dbc.CardBody(id="card-dispensers"),
            ]), className="mb-3")
        ),

        # ── Row 3: Alarmes + Log eventos ──────────────────────────────────────
        dbc.Row([
            dbc.Col(dbc.Card([
                dbc.CardHeader("🚨 Alarmes Ativos"),
                dbc.CardBody(
                    id="card-alarmes",
                    className="ap-scroll",
                    style={"maxHeight": "220px", "overflowY": "auto"},
                ),
            ]), md=5, className="mb-3"),

            dbc.Col(dbc.Card([
                dbc.CardHeader("📡 Log de Eventos"),
                dbc.CardBody(
                    id="card-log",
                    className="ap-scroll",
                    style={"maxHeight": "220px", "overflowY": "auto"},
                ),
            ]), md=7, className="mb-3"),
        ]),

        # ── BANNER: Trava de Emergência (Triple Check) ────────────────────────
        html.Div(id="banner-trava", className="ap-trava"),

        # ── Row 4: Visão Computacional + Balança ─────────────────────────────
        dbc.Row([
            dbc.Col(dbc.Card([
                dbc.CardHeader("📷 Visão Computacional"),
                dbc.CardBody(id="card-visao"),
            ]), md=6, className="mb-3"),

            dbc.Col(dbc.Card([
                dbc.CardHeader("⚖️ Balança HX711"),
                dbc.CardBody(id="card-peso"),
            ]), md=6, className="mb-3"),
        ]),

        # ── Row 5: Histórico OS ───────────────────────────────────────────────
        dbc.Row(
            dbc.Col(dbc.Card([
                dbc.CardHeader("📂 Histórico de Ordens"),
                dbc.CardBody(
                    id="card-historico",
                    className="ap-scroll",
                    style={"maxHeight": "250px", "overflowY": "auto"},
                ),
            ]), className="mb-3")
        ),
    ],
)


# ── Callbacks: busca dados ─────────────────────────────────────────────────────

@callback(
    Output("store-estado",  "data"),
    Output("store-eventos", "data"),
    Output("store-os",      "data"),
    Output("store-alarmes", "data"),
    Input("poll", "n_intervals"),
)
def _fetch(_):
    """Único ponto de I/O do dashboard.

    Os alarmes eram buscados DENTRO do `_render`, o que fazia uma quarta
    requisição a cada 2s por cliente conectado — fora do lugar onde as outras
    três estão, e num callback que deveria ser função pura do que já veio nos
    stores.
    """
    estado  = _get("/estado")
    eventos = _get("/log/eventos?limite=30")
    os_hist = _get("/os/historico?limite=10")
    alarmes = _get("/alarmes?resolvido=false&limite=20")
    return estado, eventos, os_hist, alarmes


# ── Callbacks: render ──────────────────────────────────────────────────────────

@callback(
    Output("kpi-strip",       "children"),
    Output("badge-alarmes",   "children"),
    Output("badge-os-status", "children"),
    Output("card-os",         "children"),
    Output("card-cnc",        "children"),
    Output("card-dispensers", "children"),
    Output("card-alarmes",    "children"),
    Output("card-log",        "children"),
    Output("card-historico",  "children"),
    Output("banner-trava",    "children"),
    Output("card-visao",      "children"),
    Output("card-peso",       "children"),
    Input("store-estado",  "data"),
    Input("store-eventos", "data"),
    Input("store-os",      "data"),
    Input("store-alarmes", "data"),
)
def _render(estado: dict, eventos: list, os_hist: list, alarmes_data: list):
    if not estado:
        # Defensivo, não um monitor de conexão: medido nesta versão do Dash, com
        # o central fora do ar o `_fetch` roda a cada tique mas o `_render` NÃO é
        # chamado — os stores ficam com o último valor bom e a tela congela com
        # dado velho, sem avisar. Um indicador de "sem conexão" honesto precisa
        # de outro caminho (o `WS /ws` do central), não do diff dos stores.
        vazio = _vazio("🔌", "Sem resposta do central-computer.")
        return ([], "", "") + (vazio,) * 9

    estado    = estado or {}
    eventos   = eventos or []
    os_hist   = os_hist or []
    cnc       = estado.get("cnc", {})
    os_ativa  = estado.get("os_ativa")
    disp_map  = estado.get("dispensers", {})
    n_alarmes = estado.get("alarmes_ativos", 0)

    # ── Badge de alarmes ──────────────────────────────────────────────────────
    badge_alarmes = dbc.Badge(
        f"⚠ {n_alarmes} alarme(s)" if n_alarmes else "✓ Sem alarmes",
        color="danger" if n_alarmes else "success",
        className="fs-6 px-3 py-2",
    )

    # ── OS ativa ──────────────────────────────────────────────────────────────
    fila_tamanho = estado.get("fila_tamanho", 0)
    if os_ativa:
        os_status = os_ativa.get("status", "?")
        badge_os  = _badge(os_status.upper(), _cor(os_status))

        # Itens da OS — usa atribuição IA (top-level do estado) ou itens originais
        # A atribuição IA fica em estado["atribuicao_ia"], não dentro de os_ativa
        atribuicoes = estado.get("atribuicao_ia", [])
        os_itens    = atribuicoes if atribuicoes else os_ativa.get("itens", [])

        itens_rows = []
        for item in os_itens:
            d_id  = item.get("dispenser_id")
            slot  = f"→ D{d_id}" if d_id else "→ aguardando slot"
            itens_rows.append(
                dbc.Row([
                    dbc.Col(html.Small(slot, className="text-info"),    width=3),
                    dbc.Col(html.Small(item.get("medicamento", "?"),
                                       className="fw-bold"),            width=6),
                    dbc.Col(html.Small(f"× {item.get('quantidade', 0)}",
                                       className="text-warning"),       width=3),
                ], className="mb-1")
            )

        fila_badge = (
            dbc.Badge(f"📋 {fila_tamanho} na fila", color="warning", className="ms-2")
            if fila_tamanho else ""
        )
        card_os = html.Div([
            html.H5([os_ativa.get("os_id", ""), fila_badge],
                    className="text-primary mb-1"),
            html.P(
                [os_ativa.get("descricao", ""),
                 html.Small(f" [{os_ativa.get('categoria', '')}]",
                            className="text-muted ms-1")],
                className="text-muted small mb-2",
            ),
            *itens_rows,
        ])
    else:
        badge_os = _badge("SEM OS", "secondary")
        if fila_tamanho:
            card_os = html.P(
                [f"{fila_tamanho} OS(s) aguardando na fila…",
                 dbc.Badge("FILA", color="warning", className="ms-2")],
                className="text-warning",
            )
        else:
            card_os = _vazio("💤", "Nenhuma OS em andamento.")

    # ── CNC ───────────────────────────────────────────────────────────────────
    cnc_status   = cnc.get("status", "idle")
    cnc_disp_alv = cnc.get("dispenser_alvo")
    ciclo_atual  = cnc.get("ciclo_atual", 0)
    total_ciclos = cnc.get("total_ciclos", 0)
    pct_cnc      = (ciclo_atual / max(total_ciclos, 1)) * 100 if total_ciclos else 0

    card_cnc = html.Div([
        dbc.Row([
            dbc.Col([
                html.Div([
                    html.Span("Status: "),
                    _badge(cnc_status.upper(), _cor(cnc_status)),
                ], className="mb-2"),
                html.Div(f"X: {cnc.get('posicao_x', 0):.1f} mm", className="small"),
                html.Div(f"Y: {cnc.get('posicao_y', 0):.1f} mm", className="small"),
            ], md=6),
            dbc.Col([
                html.Div(
                    f"Dispenser Alvo: {cnc_disp_alv or '—'}",
                    className="small",
                ),
                html.Div(
                    f"Ciclo: {ciclo_atual} / {total_ciclos}",
                    className="small mb-2",
                ),
                dbc.Progress(
                    value=pct_cnc,
                    color=_cor(cnc_status),
                    style={"height": "10px"},
                    label=f"{pct_cnc:.0f}%",
                ) if total_ciclos > 0 else html.Div(),
            ], md=6),
        ])
    ])

    # ── Dispensers ────────────────────────────────────────────────────────────
    # Slots são dinâmicos — nenhum é fixo para um medicamento.
    # Estado recebido via GET /estado (polling REST) do central-computer.
    disp_cards = []
    for disp_id in range(1, 7):
        info    = disp_map.get(str(disp_id), {})
        d_sts   = info.get("status", "idle")
        med     = info.get("medicamento")
        cat     = info.get("categoria")
        qtd_d   = info.get("quantidade_dispensada", 0)
        qtd_a   = info.get("quantidade_alvo", 0)
        qtd_res = info.get("quantidade", info.get("quantidade_residual", 0))
        pct     = (qtd_d / qtd_a * 100) if qtd_a > 0 else 0

        # Rótulo do slot
        if med and qtd_res > 0:
            med_label  = med
            cat_label  = html.Small(f"[{cat}]", className="text-muted ms-1") if cat else ""
            res_label  = html.Small(f" {qtd_res} un. residuais", className="text-muted")
        elif med and qtd_res == 0:
            # Slot ficou sem estoque — aguardando limpeza manual ou nova OS
            med_label  = "— Vazio —"
            cat_label  = html.Small(f"(era {med})", className="text-muted ms-1")
            res_label  = html.Small("sem estoque", className="text-danger fst-italic")
        else:
            med_label  = "— Slot livre —"
            cat_label  = ""
            res_label  = html.Small("aguardando OS", className="text-muted fst-italic")

        disp_cards.append(
            dbc.Col(
                dbc.Card([
                    dbc.CardHeader(
                        html.Small([
                            html.Strong(f"D{disp_id} "),
                            _badge(d_sts, _cor(d_sts)),
                        ]),
                        className="py-1 px-2",
                    ),
                    dbc.CardBody([
                        html.P(
                            [med_label, cat_label],
                            className="mb-0 fw-semibold",
                            style={"fontSize": "0.68rem"},
                        ),
                        html.Div(res_label, className="mb-1"),
                        html.Div(
                            f"{qtd_d} / {qtd_a}" if qtd_a else "—",
                            className="text-center fw-bold mb-1 small",
                        ),
                        dbc.Progress(
                            value=pct,
                            color=_cor(d_sts),
                            style={"height": "6px"},
                        ) if qtd_a > 0 else html.Div(),
                    ], className="py-1 px-2"),
                ], className="h-100"),
                md=2, sm=4, xs=6,
                # `is-active` acende a borda dourada do slot que está em
                # operação — num painel de 6 cartões iguais, é o que faz o
                # operador achar o slot certo sem ler cada um.
                className=("ap-slot mb-2 is-active"
                           if d_sts not in ("idle", None) else "ap-slot mb-2"),
            )
        )

    card_dispensers = dbc.Row(disp_cards)

    # ── Alarmes ───────────────────────────────────────────────────────────────
    if alarmes_data and isinstance(alarmes_data, list):
        card_alarmes_content = html.Div([
            dbc.Alert([
                html.Strong(f"[{a.get('tipo', '?').upper()}] "),
                html.Span(a.get("descricao", "")),
                html.Small(
                    f" — {_hora(a.get('ts'))}",
                    className="text-muted ms-2",
                ),
            ], color="danger", className="py-1 px-2 mb-1 small")
            for a in alarmes_data
        ])
    else:
        card_alarmes_content = _vazio("✓", "Nenhum alarme ativo.")

    # ── Log eventos ───────────────────────────────────────────────────────────
    ICONE = {
        "os_nova":          "📋",
        "ia_atribuicao":    "🧠",
        "carregamento":     "📦",
        "dispenser_pronto": "✅",
        "dispensa":         "💊",
        "falha_ia":         "⚠️",
        "alarme":           "🚨",
        "cnc_concluido":    "🏁",
    }
    # Ícone, hora e mensagem em colunas próprias: a hora vira uma coluna fixa e
    # as mensagens alinham à esquerda, em vez de começarem em posições
    # diferentes conforme o tamanho do carimbo de tempo.
    log_items = [
        html.Div(
            [
                html.Span(ICONE.get(e.get("tipo", ""), "•"), className="ico"),
                html.Span(_hora(e.get("ts")), className="ts"),
                html.Span(e.get("msg", ""), className="msg"),
            ],
            className="ap-log-line",
        )
        for e in (eventos or [])
    ]
    card_log_content = (
        html.Div(log_items)
        if log_items
        else _vazio("📡", "Nenhum evento registrado ainda.")
    )

    # ── Histórico OS ──────────────────────────────────────────────────────────
    if os_hist and isinstance(os_hist, list):
        hist_rows = []
        for h in os_hist:
            criado = _hora(h.get("criado_em"), com_data=True)
            hist_rows.append(
                dbc.Row([
                    dbc.Col(
                        html.Small(h.get("os_id", "?")[:22],
                                   className="fw-bold text-primary"), md=4),
                    dbc.Col(
                        [
                            html.Small(h.get("categoria", ""),
                                       className="text-info me-1"),
                            html.Small(criado, className="text-muted"),
                        ],
                        md=5,
                    ),
                    dbc.Col(
                        _badge(h.get("status", "?"), _cor(h.get("status", ""))),
                        md=3,
                    ),
                ], className="ap-row mb-1")
            )
        card_historico_content = html.Div(hist_rows)
    else:
        card_historico_content = _vazio("📂", "Nenhuma OS concluída ainda.")

    # ── Banner de Trava (Triple Check) ───────────────────────────────────────
    trava = estado.get("trava", {})
    if trava.get("ativa"):
        banner_trava = dbc.Alert([
            html.H4("⛔ TRAVA DE EMERGÊNCIA ATIVA", className="alert-heading"),
            html.Hr(),
            html.P([
                html.Strong("OS: "), trava.get("os_id", "?"), " | ",
                html.Strong("Slot: "), f"D{trava.get('slot_id', '?')}", html.Br(),
                html.Strong("Motivo: "), trava.get("motivo", ""),
            ], className="mb-0 small"),
            html.P(
                "⚠ O sistema está aguardando intervenção de supervisor. "
                "Acesse a IHM para liberar a OS.",
                className="mt-2 mb-0 fw-bold",
            ),
        ], color="danger", className="mx-0 my-3")
    else:
        banner_trava = html.Div()

    # ── Visão Computacional ───────────────────────────────────────────────────
    visao = estado.get("visao", {})
    cam_disp = visao.get("camera_dispenser", {})
    cam_mesa = visao.get("camera_mesa", {})

    def _cor_leitura(tipo):
        if tipo is None:
            return "secondary"
        if "ok" in tipo:
            return "success"
        if "divergencia" in tipo or "falha" in tipo:
            return "danger"
        return "info"

    card_visao = dbc.Row([
        dbc.Col([
            html.P("📷 Câmera Dispenser (SKU)", className="fw-bold mb-1 small"),
            html.Div([
                html.Span("Última: "),
                _badge(cam_disp.get("ultima_leitura") or "—",
                       _cor_leitura(cam_disp.get("ultima_leitura"))),
            ], className="small mb-1"),
            html.Div([
                html.Small(f"Slot D{cam_disp.get('slot_id') or '?'}", className="text-muted me-2"),
                html.Small(
                    f"Conf: {int((cam_disp.get('confianca') or 0)*100)}%",
                    className="text-muted",
                ),
            ], className="mb-1"),
            html.Small(
                f"Match SKU: {'✓' if cam_disp.get('match_sku') else ('✗' if cam_disp.get('match_sku') is False else '—')}",
                className=("text-success" if cam_disp.get("match_sku")
                           else "text-danger" if cam_disp.get("match_sku") is False
                           else "text-muted"),
            ),
        ], md=6),
        dbc.Col([
            html.P("📷 Câmera Mesa (Contagem)", className="fw-bold mb-1 small"),
            html.Div([
                html.Span("Última: "),
                _badge(cam_mesa.get("ultima_leitura") or "—",
                       _cor_leitura(cam_mesa.get("ultima_leitura"))),
            ], className="small mb-1"),
            html.Div([
                html.Small(f"Slot D{cam_mesa.get('slot_id') or '?'}", className="text-muted me-2"),
                html.Small(
                    f"Conf: {int((cam_mesa.get('confianca') or 0)*100)}%",
                    className="text-muted",
                ),
            ], className="mb-1"),
            html.Small(
                f"Detectado: {cam_mesa.get('quantidade_detectada') or '—'} "
                f"/ Esperado: {cam_mesa.get('quantidade_esperada') or '—'}",
                className="text-muted",
            ),
        ], md=6),
    ])

    # ── Balança HX711 ─────────────────────────────────────────────────────────
    peso = estado.get("peso", {})
    tipo_peso  = peso.get("ultima_leitura")
    cor_peso   = ("success" if tipo_peso == "peso_ok"
                  else "danger" if tipo_peso == "peso_divergencia"
                  else "warning" if tipo_peso == "tara_ok"
                  else "secondary")
    card_peso = html.Div([
        html.Div([
            html.Span("Status: "),
            _badge(tipo_peso or "—", cor_peso),
            html.Small(f"  Slot: D{peso.get('slot_id') or '?'}", className="text-muted ms-2"),
        ], className="small mb-1"),
        html.Small(
            f"Leitura: {peso.get('peso_medido_g') or '—'} g"
            f"  |  Esperado: {peso.get('peso_esperado_g') or '—'} g"
            f"  |  Desvio: {peso.get('desvio_pct') or '—'}%"
            if tipo_peso in ("peso_ok", "peso_divergencia") else
            "Aguardando pesagem…",
            className="text-muted",
        ),
    ])

    # ── Faixa de indicadores ──────────────────────────────────────────────────
    # Os seis números que respondem "o sistema está indo bem?" sem precisar ler
    # cartão nenhum. Todos saem do mesmo `estado` que os cartões já usam — a
    # faixa não custa requisição extra.
    fila_cap   = estado.get("fila_capacidade", 5)
    slots_uso  = sum(
        1 for d in disp_map.values()
        if d.get("status") not in (None, "idle") or d.get("quantidade", 0) > 0
    )
    # `OS-20260818-000412` não cabe na coluna do KPI, e cortar pelo meio some
    # justamente com o número que distingue uma OS da outra. Mostra-se o
    # sufixo (`#000412`) e o id inteiro vai no `title`; o cartão da OS ativa,
    # logo abaixo, continua exibindo a forma completa.
    os_id_full  = (os_ativa or {}).get("os_id", "") or ""
    os_id_curto = ("#" + os_id_full.rsplit("-", 1)[-1]) if os_id_full else "—"

    kpis = [
        _kpi("OS em execução", os_id_curto, dica=os_id_full,
             tom="is-gold" if os_ativa else ""),
        _kpi("Progresso CNC", f"{pct_cnc:.0f}", "%", tom="is-blue" if total_ciclos else ""),
        _kpi("Fila", f"{fila_tamanho}/{fila_cap}",
             tom="is-err" if fila_tamanho >= fila_cap else ("is-warn" if fila_tamanho else "")),
        _kpi("Slots em uso", f"{slots_uso}/6", tom="is-blue" if slots_uso else ""),
        _kpi("Mesa CNC", cnc_status.replace("_", " "),
             tom="is-text is-err" if cnc_status == "erro"
                 else "is-text is-ok" if cnc_status != "idle" else "is-text"),
        _kpi("Alarmes", n_alarmes, tom="is-err" if n_alarmes else "is-ok"),
    ]

    return (
        kpis,
        badge_alarmes,
        badge_os,
        card_os,
        card_cnc,
        card_dispensers,
        card_alarmes_content,
        card_log_content,
        card_historico_content,
        banner_trava,
        card_visao,
        card_peso,
    )


# ── Entry point ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8050")),
        debug=os.getenv("DEBUG", "false").lower() == "true",
    )
