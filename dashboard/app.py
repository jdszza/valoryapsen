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

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
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

        # ── Header ────────────────────────────────────────────────────────────
        dbc.Row(
            dbc.Col(
                html.Div([
                    html.H1("APSEN", className="d-inline me-3 fw-bold text-primary"),
                    html.Span(
                        "Sistema de Contagem de Medicamentos",
                        className="text-muted fs-5",
                    ),
                    html.Span(id="badge-alarmes", className="float-end mt-1"),
                ]),
                className="py-3 border-bottom",
            )
        ),

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
                    style={"maxHeight": "220px", "overflowY": "auto"},
                ),
            ]), md=5, className="mb-3"),

            dbc.Col(dbc.Card([
                dbc.CardHeader("📡 Log de Eventos"),
                dbc.CardBody(
                    id="card-log",
                    style={"maxHeight": "220px", "overflowY": "auto"},
                ),
            ]), md=7, className="mb-3"),
        ]),

        # ── Row 4: Histórico OS ───────────────────────────────────────────────
        dbc.Row(
            dbc.Col(dbc.Card([
                dbc.CardHeader("📂 Histórico de Ordens"),
                dbc.CardBody(
                    id="card-historico",
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
    Input("poll", "n_intervals"),
)
def _fetch(_):
    estado  = _get("/estado")
    eventos = _get("/log/eventos?limite=30")
    os_hist = _get("/os/historico?limite=10")
    return estado, eventos, os_hist


# ── Callbacks: render ──────────────────────────────────────────────────────────

@callback(
    Output("badge-alarmes",   "children"),
    Output("badge-os-status", "children"),
    Output("card-os",         "children"),
    Output("card-cnc",        "children"),
    Output("card-dispensers", "children"),
    Output("card-alarmes",    "children"),
    Output("card-log",        "children"),
    Output("card-historico",  "children"),
    Input("store-estado",  "data"),
    Input("store-eventos", "data"),
    Input("store-os",      "data"),
)
def _render(estado: dict, eventos: list, os_hist: list):
    if not estado:
        vazio = html.P("Aguardando backend...", className="text-muted")
        return (vazio,) * 8

    estado    = estado or {}
    eventos   = eventos or []
    os_hist   = os_hist or []
    cnc       = estado.get("cnc", {})
    os_ativa  = estado.get("os_ativa")
    disp_map  = estado.get("dispensers", {})
    n_alarmes = estado.get("alarmes_ativos", 0)

    # ── Badge alarmes ─────────────────────────────────────────────────────────
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

        # Itens da OS — sem dispenser_id fixo (roteamento dinâmico)
        # Prioriza atribuicoes (com slot atribuído) ou itens originais
        atribuicoes = os_ativa.get("atribuicoes", [])
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
            card_os = html.P("Nenhuma OS em andamento.", className="text-muted")

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
    # O dispenser_simulator publica o medicamento atual via MQTT.
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
            # Slot ficou sem estoque mas ainda não foi limpo pelo MQTT
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
                md=2, sm=4, xs=6, className="mb-2",
            )
        )

    card_dispensers = dbc.Row(disp_cards)

    # ── Alarmes ───────────────────────────────────────────────────────────────
    alarmes_data = _get("/alarmes?resolvido=false&limite=20")
    if alarmes_data and isinstance(alarmes_data, list):
        card_alarmes_content = html.Div([
            dbc.Alert([
                html.Strong(f"[{a.get('tipo', '?').upper()}] "),
                html.Span(a.get("descricao", "")),
                html.Small(
                    f" — {str(a.get('criado_em', ''))[:16]}",
                    className="text-muted ms-2",
                ),
            ], color="danger", className="py-1 px-2 mb-1 small")
            for a in alarmes_data
        ])
    else:
        card_alarmes_content = html.P("Nenhum alarme ativo.", className="text-muted small")

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
    log_items = [
        html.Div(
            html.Small(
                f"{ICONE.get(e.get('tipo', ''), '•')} "
                f"[{str(e.get('ts', ''))[:19].replace('T', ' ')}] "
                f"{e.get('msg', '')}",
                className="d-block text-muted",
            ),
            className="mb-1",
        )
        for e in (eventos or [])
    ]
    card_log_content = (
        html.Div(log_items)
        if log_items
        else html.P("Sem eventos.", className="text-muted small")
    )

    # ── Histórico OS ──────────────────────────────────────────────────────────
    if os_hist and isinstance(os_hist, list):
        hist_rows = []
        for h in os_hist:
            criado = str(h.get("criado_em", ""))[:16].replace("T", " ")
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
                ], className="mb-1 border-bottom pb-1")
            )
        card_historico_content = html.Div(hist_rows)
    else:
        card_historico_content = html.P("Sem histórico de OS.", className="text-muted small")

    return (
        badge_alarmes,
        badge_os,
        card_os,
        card_cnc,
        card_dispensers,
        card_alarmes_content,
        card_log_content,
        card_historico_content,
    )


# ── Entry point ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8050")),
        debug=os.getenv("DEBUG", "false").lower() == "true",
    )
