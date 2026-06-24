"""
APSEN - Sistema Integrado de Controle de Produção
=================================================
Unifica Dashboard Executivo + IHM Operacional em uma única aplicação Dash.
Porta: 8050

Roles:
  operador   → Dashboard, Operação, Ordens
  manutencao → + Manutenção
  admin      → + Usuários

Padrão de hidratação:
  dcc.Interval(_hydrated) dispara 100ms após o mount do React,
  garantindo que o sessionStorage já foi lido antes de o router renderizar.
  Todos os callbacks de botões dinâmicos têm guard `if not n: return no_update`
  para evitar disparos com n_clicks=0 quando o componente aparece no DOM.
"""

import os

import dash
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import requests
from dash import Input, Output, State, callback, ctx, dcc, html

# ── Config ─────────────────────────────────────────────────────────────────────
BACKEND = os.getenv("BACKEND_URL", "http://backend:8000")

# ── Cores ──────────────────────────────────────────────────────────────────────
AZUL  = "#003087"
AZUL2 = "#0057b8"
VERDE = "#28a745"
VERM  = "#dc3545"
AMAR  = "#ffc107"
CINZA = "#6c757d"
SIDEBAR_BG = "#0f1923"

STATUS_COR   = {"idle": CINZA, "running": VERDE, "paused": AMAR, "alarm": VERM}
STATUS_LABEL = {
    "idle":    "AGUARDANDO",
    "running": "EM PRODUÇÃO",
    "paused":  "PAUSADO",
    "alarm":   "ALARME",
}
STATUS_COR_OS = {
    "aberto":       AZUL,
    "em_andamento": VERDE,
    "pausado":      AMAR,
    "concluido":    CINZA,
    "cancelado":    VERM,
}

_ROLES = {"operador": 1, "manutencao": 2, "admin": 3}


def _tem_acesso(role: str, minimo: str) -> bool:
    return _ROLES.get(role, 0) >= _ROLES.get(minimo, 0)


# ── API helper ─────────────────────────────────────────────────────────────────

def api(method: str, path: str, token: str = None, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = getattr(requests, method)(
            f"{BACKEND}{path}", headers=headers, timeout=5, **kwargs
        )
        return r.json() if r.ok else None
    except Exception:
        return None


# ── Componentes reutilizáveis ──────────────────────────────────────────────────

def kpi_card(titulo: str, valor, sub: str = "", cor: str = AZUL):
    return html.Div(
        [
            html.Div(titulo, className="kpi-label"),
            html.Div(str(valor), className="kpi-value"),
            html.Div(sub, className="kpi-sub"),
        ],
        className="kpi-card",
        style={"borderLeftColor": cor},
    )


def section_card(titulo: str, conteudo, cor: str = AZUL):
    return dbc.Card(
        [
            dbc.CardHeader(
                html.Span(titulo, style={"fontWeight": "700", "fontSize": "14px"}),
                style={"borderLeft": f"4px solid {cor}", "background": "#f8f9fa"},
            ),
            dbc.CardBody(conteudo, style={"padding": "16px"}),
        ],
        className="mb-3 shadow-sm",
    )


# ── Sidebar ────────────────────────────────────────────────────────────────────

def _sidebar(role: str, pathname: str):
    nav = [
        ("/",            "📊", "Dashboard",   "operador"),
        ("/operacao",    "⚙️",  "Operação",    "operador"),
        ("/ordens",      "📋", "Ordens de Serviço", "operador"),
        ("/manutencao",  "🔧", "Manutenção",  "manutencao"),
        ("/usuarios",    "👥", "Usuários",    "admin"),
    ]
    links = []
    for href, icon, label, req in nav:
        if not _tem_acesso(role, req):
            continue
        active = pathname == href
        links.append(
            html.A(
                [
                    html.Span(icon, className="nav-icon"),
                    html.Span(label, className="nav-label"),
                ],
                href=href,
                className=f"sidebar-link{'  active' if active else ''}",
            )
        )

    return html.Div(
        [
            html.Div(
                [
                    html.Div("APSEN", className="sidebar-logo"),
                    html.Div("Controle de Produção", className="sidebar-tagline"),
                ],
                className="sidebar-header",
            ),
            html.Div(links, className="sidebar-nav"),
            dbc.Button(
                "⏻  Sair",
                id="btn-logout",
                size="sm",
                className="sidebar-logout",
            ),
        ],
        className="sidebar",
    )


def _topbar(nome: str, pathname: str):
    titles = {
        "/":           "Dashboard",
        "/operacao":   "Operação",
        "/ordens":     "Ordens de Serviço",
        "/manutencao": "Manutenção",
        "/usuarios":   "Usuários",
    }
    return html.Div(
        [
            dbc.Button(
                "☰",
                id="btn-hamburguer",
                color="link",
                className="topbar-hamburger",
            ),
            html.Span(titles.get(pathname, "APSEN"), className="topbar-title"),
            html.Span(nome, className="topbar-user"),
        ],
        className="topbar d-md-none",
    )


# ── Login ──────────────────────────────────────────────────────────────────────

def pagina_login(erro: str = ""):
    return html.Div(
        html.Div(
            [
                html.Div(
                    [
                        html.H1("APSEN", className="login-logo"),
                        html.P("Sistema de Controle de Produção", className="login-sub"),
                    ],
                    className="text-center mb-4",
                ),
                dbc.Input(
                    id="login-user", placeholder="Usuário", type="text",
                    className="mb-3", size="lg",
                ),
                dbc.Input(
                    id="login-senha", placeholder="Senha", type="password",
                    className="mb-3", size="lg",
                ),
                dbc.Button(
                    "ENTRAR", id="btn-login", size="lg",
                    className="w-100 btn-entrar",
                ),
                html.Div(
                    erro, className="login-erro mt-2 text-center",
                    style={"display": "none" if not erro else "block"},
                ),
            ],
            className="login-box",
        ),
        className="login-bg",
    )


# ── Páginas ────────────────────────────────────────────────────────────────────

def pg_dashboard(_auth):
    return html.Div(
        [
            html.H4("Dashboard de Produção", className="page-title"),
            dbc.Row(id="dash-kpis", className="g-3 mb-4"),
            html.Div(id="dash-alarme", className="mb-3"),
            dbc.Row(
                [
                    dbc.Col(
                        section_card(
                            "Produção ao Longo do Tempo",
                            dcc.Graph(
                                id="dash-graph",
                                style={"height": "300px"},
                                config={"displayModeBar": False},
                            ),
                        ),
                        width=12, lg=8,
                    ),
                    dbc.Col(
                        [
                            section_card(
                                "Progresso do Lote",
                                dcc.Graph(
                                    id="dash-gauge",
                                    style={"height": "180px"},
                                    config={"displayModeBar": False},
                                ),
                            ),
                            html.Div(id="dash-painel-os"),
                        ],
                        width=12, lg=4,
                    ),
                ],
                className="g-3 mb-3",
            ),
            # ── Log de Comunicação CNC ────────────────────────────────────────
            dbc.Row(
                [
                    dbc.Col(
                        section_card(
                            "📡 Comunicação Backend ↔ CNC",
                            html.Div(id="dash-cnc-log"),
                            cor="#6f42c1",
                        ),
                        width=12,
                    ),
                ],
                className="g-3",
            ),
            dcc.Interval(id="iv-dash", interval=3000, n_intervals=0),
        ]
    )


def pg_operacao(_auth):
    return html.Div(
        [
            html.H4("Operação", className="page-title"),
            dbc.Row(id="op-kpis", className="g-3 mb-4"),
            dbc.Row(
                [
                    dbc.Col(
                        section_card(
                            "Controle da Máquina",
                            html.Div(
                                [
                                    dbc.Row(
                                        [
                                            dbc.Col(
                                                dbc.Button(
                                                    "▶ Iniciar", id="btn-start",
                                                    color="success", className="w-100",
                                                ),
                                                width=6, sm=3,
                                            ),
                                            dbc.Col(
                                                dbc.Button(
                                                    "⏸ Pausar", id="btn-pause",
                                                    color="warning", className="w-100",
                                                ),
                                                width=6, sm=3,
                                            ),
                                            dbc.Col(
                                                dbc.Button(
                                                    "⏹ Parar", id="btn-stop",
                                                    color="danger", className="w-100",
                                                ),
                                                width=6, sm=3,
                                            ),
                                            dbc.Col(
                                                dbc.Button(
                                                    "↺ Reset", id="btn-reset",
                                                    color="secondary", className="w-100",
                                                ),
                                                width=6, sm=3,
                                            ),
                                        ],
                                        className="g-2 mb-3",
                                    ),
                                    html.Div(id="cmd-fb"),
                                ]
                            ),
                            cor=AZUL,
                        ),
                        width=12, lg=6,
                    ),
                    dbc.Col(
                        section_card(
                            "Iniciar Novo Lote",
                            html.Div(
                                [
                                    dbc.Input(
                                        id="inp-lote-id",
                                        placeholder="ID do Lote  (ex: LOTE-002)",
                                        className="mb-2", size="sm",
                                    ),
                                    dbc.Input(
                                        id="inp-lote-prod",
                                        placeholder="Produto  (ex: Comprimido 500mg)",
                                        className="mb-2", size="sm",
                                    ),
                                    dbc.Input(
                                        id="inp-lote-meta",
                                        placeholder="Meta (unidades)",
                                        type="number", className="mb-2", size="sm",
                                    ),
                                    dbc.Button(
                                        "Iniciar Lote", id="btn-iniciar-lote",
                                        color="success", size="sm", className="w-100",
                                    ),
                                    html.Div(id="lote-fb", className="mt-2"),
                                ]
                            ),
                            cor=VERDE,
                        ),
                        width=12, lg=6,
                    ),
                ],
                className="g-3",
            ),
            dcc.Interval(id="iv-op", interval=2000, n_intervals=0),
        ]
    )


def pg_ordens(_auth):
    return html.Div(
        [
            html.H4("Ordens de Serviço", className="page-title"),
            dbc.Row(
                [
                    dbc.Col(
                        dbc.Button(
                            "+ Nova OS", id="btn-nova-os",
                            color="primary", size="sm",
                        ),
                        width="auto",
                    ),
                    dbc.Col(
                        dbc.Button(
                            "↺ Atualizar", id="btn-reload-os",
                            color="outline-secondary", size="sm",
                        ),
                        width="auto",
                    ),
                ],
                className="mb-3 g-2",
            ),
            html.Div(id="tbl-ordens"),
            dcc.Interval(id="iv-ordens-init", interval=200, max_intervals=1),
            # ── Modal Nova OS ───────────────────────────────────────────────
            dbc.Modal(
                [
                    dbc.ModalHeader(dbc.ModalTitle("Nova Ordem de Serviço")),
                    dbc.ModalBody(
                        [
                            dbc.Label("Produto"),
                            dbc.Input(id="inp-os-produto", placeholder="Nome do produto", className="mb-2"),
                            dbc.Label("Lote ID"),
                            dbc.Input(id="inp-os-lote", placeholder="ex: LOTE-003", className="mb-2"),
                            dbc.Label("Meta (unidades)"),
                            dbc.Input(id="inp-os-meta", placeholder="ex: 5000", type="number", className="mb-2"),
                            dbc.Label("Responsável"),
                            dbc.Input(id="inp-os-resp", placeholder="Nome do responsável", className="mb-2"),
                            html.Div(id="os-fb"),
                        ]
                    ),
                    dbc.ModalFooter(
                        [
                            dbc.Button("Cancelar", id="btn-nos-cancel", color="secondary", size="sm"),
                            dbc.Button("Criar OS",  id="btn-os-submit",  color="primary",   size="sm"),
                        ]
                    ),
                ],
                id="modal-os",
                is_open=False,
                backdrop="static",
            ),
        ]
    )


def pg_manutencao(_auth):
    return html.Div(
        [
            html.H4("Manutenção", className="page-title"),
            dbc.Row(
                [
                    dbc.Col(
                        section_card(
                            "Diagnóstico da Máquina",
                            html.Div(id="manut-diag"),
                        ),
                        width=12, lg=5,
                    ),
                    dbc.Col(
                        section_card(
                            "Comandos de Manutenção",
                            html.Div(
                                [
                                    dbc.Select(
                                        id="sel-manut-cmd",
                                        options=[
                                            {"label": "Teste de Sensores",   "value": "teste_sensores"},
                                            {"label": "Calibrar Contagem",    "value": "calibrar"},
                                            {"label": "Flush de Esteira",     "value": "flush_esteira"},
                                            {"label": "Reset de Alarmes",     "value": "reset_alarme"},
                                            {"label": "Reiniciar Máquina",    "value": "reiniciar"},
                                        ],
                                        placeholder="Selecione o comando...",
                                        className="mb-2",
                                    ),
                                    dbc.Input(
                                        id="inp-manut-comp",
                                        placeholder="Componente (ex: sensor-1)",
                                        className="mb-2", size="sm",
                                    ),
                                    dbc.Button(
                                        "Executar Comando", id="btn-manut-cmd",
                                        color="warning", size="sm",
                                    ),
                                    html.Div(id="manut-cmd-fb", className="mt-2"),
                                ]
                            ),
                            cor=AMAR,
                        ),
                        width=12, lg=7,
                    ),
                ],
                className="g-3 mb-3",
            ),
            section_card(
                "Registrar Ocorrência de Manutenção",
                html.Div(
                    [
                        dbc.Row(
                            [
                                dbc.Col(
                                    dbc.Select(
                                        id="inp-manut-tipo",
                                        options=[
                                            {"label": "Preventiva", "value": "preventiva"},
                                            {"label": "Corretiva",  "value": "corretiva"},
                                            {"label": "Preditiva",  "value": "preditiva"},
                                            {"label": "Inspeção",   "value": "inspecao"},
                                        ],
                                        placeholder="Tipo...",
                                    ),
                                    width=12, md=4,
                                ),
                                dbc.Col(
                                    dbc.Input(
                                        id="inp-manut-comp2",
                                        placeholder="Componente",
                                        size="sm",
                                    ),
                                    width=12, md=5,
                                ),
                                dbc.Col(
                                    dbc.Button(
                                        "Registrar", id="btn-manut-log",
                                        color="primary", size="sm", className="w-100",
                                    ),
                                    width=12, md=3,
                                ),
                            ],
                            className="g-2 mb-2",
                        ),
                        dbc.Textarea(
                            id="inp-manut-desc",
                            placeholder="Descrição detalhada da manutenção...",
                            rows=2, className="mb-2",
                        ),
                        html.Div(id="manut-log-fb"),
                    ]
                ),
                cor=VERDE,
            ),
            html.H6("Histórico de Manutenção", className="mt-2 mb-2", style={"color": AZUL}),
            html.Div(id="tbl-manut-log"),
            dcc.Interval(id="iv-manut", interval=10000, n_intervals=0),
        ]
    )


def pg_usuarios(_auth):
    return html.Div(
        [
            html.H4("Gestão de Usuários", className="page-title"),
            dbc.Button(
                "+ Novo Usuário", id="btn-novo-user",
                color="primary", size="sm", className="mb-3",
            ),
            html.Div(id="tbl-users"),
            dcc.Interval(id="iv-users-init", interval=200, max_intervals=1),
            dbc.Modal(
                [
                    dbc.ModalHeader(dbc.ModalTitle("Novo Usuário")),
                    dbc.ModalBody(
                        [
                            dbc.Label("Username"),
                            dbc.Input(id="inp-user-name", placeholder="ex: joao.silva", className="mb-2"),
                            dbc.Label("Senha"),
                            dbc.Input(id="inp-user-senha", type="password", placeholder="Senha", className="mb-2"),
                            dbc.Label("Nome Completo"),
                            dbc.Input(id="inp-user-nome", placeholder="João da Silva", className="mb-2"),
                            dbc.Label("Função"),
                            dbc.Select(
                                id="inp-user-role",
                                options=[
                                    {"label": "Operador",   "value": "operador"},
                                    {"label": "Manutenção", "value": "manutencao"},
                                    {"label": "Admin",      "value": "admin"},
                                ],
                                placeholder="Selecione a função...",
                                className="mb-2",
                            ),
                            html.Div(id="user-fb"),
                        ]
                    ),
                    dbc.ModalFooter(
                        [
                            dbc.Button("Cancelar",      id="btn-user-cancel", color="secondary", size="sm"),
                            dbc.Button("Criar Usuário", id="btn-user-submit", color="primary",   size="sm"),
                        ]
                    ),
                ],
                id="modal-user",
                is_open=False,
                backdrop="static",
            ),
        ]
    )


# ── Layout autenticado ─────────────────────────────────────────────────────────

_PAGES = {
    "/":           pg_dashboard,
    "/operacao":   pg_operacao,
    "/ordens":     pg_ordens,
    "/manutencao": pg_manutencao,
    "/usuarios":   pg_usuarios,
}

_PAGE_ROLE = {
    "/manutencao": "manutencao",
    "/usuarios":   "admin",
}


def _layout_autenticado(auth, pathname):
    role     = (auth or {}).get("role", "operador")
    nome     = (auth or {}).get("nome", "")
    pathname = pathname or "/"

    # Redireciona se sem permissão
    req = _PAGE_ROLE.get(pathname)
    if req and not _tem_acesso(role, req):
        pathname = "/"

    page_fn = _PAGES.get(pathname, pg_dashboard)

    return html.Div(
        [
            _sidebar(role, pathname),
            _topbar(nome, pathname),
            # Offcanvas para mobile
            dbc.Offcanvas(
                _sidebar(role, pathname),
                id="sidebar-offcanvas",
                is_open=False,
                placement="start",
                style={"width": "220px", "background": SIDEBAR_BG},
            ),
            html.Div(page_fn(auth), className="main-content"),
        ],
        className="app-wrapper",
    )


# ── App ────────────────────────────────────────────────────────────────────────

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    suppress_callback_exceptions=True,
    title="APSEN – Sistema de Controle",
    update_title=None,
)
server = app.server

app.layout = html.Div(
    [
        dcc.Location(id="url"),
        dcc.Store(id="auth-store", storage_type="session"),
        # Guard de hidratação: _hydrated dispara 100ms após mount do React,
        # garantindo que sessionStorage já foi lido pelo dcc.Store.
        dcc.Store(id="_ready", data=False),
        dcc.Interval(id="_hydrated", interval=100, max_intervals=1),
        html.Div(id="app-root"),
    ]
)


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACKS GLOBAIS
# ══════════════════════════════════════════════════════════════════════════════

@callback(
    Output("_ready", "data"),
    Input("_hydrated", "n_intervals"),
    prevent_initial_call=True,
)
def _mark_ready(_):
    return True


@callback(
    Output("app-root", "children"),
    Input("auth-store", "data"),
    Input("url", "pathname"),
    Input("_ready", "data"),
)
def router(auth, pathname, ready):
    if not ready:
        return html.Div()
    if not auth or not auth.get("token"):
        return pagina_login()
    return _layout_autenticado(auth, pathname)


@callback(
    Output("auth-store", "data"),
    Output("app-root", "children", allow_duplicate=True),
    Input("btn-login", "n_clicks"),
    State("login-user",  "value"),
    State("login-senha", "value"),
    prevent_initial_call=True,
)
def fazer_login(n, username, senha):
    if not n:
        return dash.no_update, dash.no_update
    if not username or not senha:
        return dash.no_update, pagina_login("Preencha usuário e senha.")
    resp = api("post", "/auth/login", json={"username": username, "senha": senha})
    if resp and "token" in resp:
        return resp, dash.no_update
    return dash.no_update, pagina_login("Usuário ou senha incorretos.")


@callback(
    Output("auth-store", "data", allow_duplicate=True),
    Output("url", "pathname", allow_duplicate=True),
    Input("btn-logout", "n_clicks"),
    prevent_initial_call=True,
)
def logout(n):
    # Guard: btn-logout aparece dinamicamente com n_clicks=0
    if not n:
        return dash.no_update, dash.no_update
    return None, "/"


@callback(
    Output("sidebar-offcanvas", "is_open"),
    Input("btn-hamburguer", "n_clicks"),
    State("sidebar-offcanvas", "is_open"),
    prevent_initial_call=True,
)
def toggle_sidebar(n, is_open):
    if not n:
        return is_open
    return not is_open


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

_CNC_STATUS_COR = {
    "enviada":   "#0057b8",
    "recebido":  "#17a2b8",
    "iniciando": "#28a745",
    "concluido": "#6c757d",
    "erro":      "#dc3545",
}
_CNC_STATUS_ICON = {
    "enviada":   "→",
    "recebido":  "✓",
    "iniciando": "▶",
    "concluido": "✅",
    "erro":      "✗",
}


@callback(
    Output("dash-kpis",      "children"),
    Output("dash-alarme",    "children"),
    Output("dash-graph",     "figure"),
    Output("dash-gauge",     "figure"),
    Output("dash-painel-os", "children"),
    Output("dash-cnc-log",   "children"),
    Input("iv-dash", "n_intervals"),
    State("auth-store", "data"),
    prevent_initial_call=True,
)
def atualizar_dashboard(_, auth):
    token    = (auth or {}).get("token")
    est      = api("get", "/estado", token=token) or {}
    contagem = est.get("contagem", 0)
    meta     = est.get("meta", 1)
    status   = est.get("status", "idle")
    vel      = est.get("velocidade", 0)
    lote_id  = est.get("lote_id", "—")
    alarme   = est.get("alarme")
    prog     = min(100, round(contagem / max(meta, 1) * 100, 1))

    kpis = [
        dbc.Col(kpi_card("Contagem Atual", f"{contagem:,}", f"Lote: {lote_id}", AZUL),  width=6, lg=2),
        dbc.Col(kpi_card("Meta",           f"{meta:,}",     "unidades",         CINZA), width=6, lg=2),
        dbc.Col(kpi_card("Progresso",      f"{prog}%",
                         f"{contagem:,} / {meta:,}",
                         VERDE if prog >= 80 else AMAR),                                width=6, lg=2),
        dbc.Col(kpi_card("Velocidade",     f"{vel}",         "un/min",          AZUL2), width=6, lg=3),
        dbc.Col(kpi_card("Status",
                         STATUS_LABEL.get(status, status.upper()),
                         "",
                         STATUS_COR.get(status, CINZA)),                                width=12, lg=3),
    ]

    banner = (
        html.Div(
            [html.Strong("⚠ ALARME: "), alarme],
            style={
                "background": "#fff3cd", "border": f"1px solid {AMAR}",
                "borderRadius": "8px", "padding": "12px 16px",
                "color": "#856404", "fontWeight": "600",
            },
        )
        if alarme
        else html.Div()
    )

    hist = list(reversed(api("get", f"/historico?lote_id={lote_id}&limite=200", token=token) or []))
    fig  = go.Figure()
    if hist:
        fig.add_trace(
            go.Scatter(
                x=[r["ts"] for r in hist],
                y=[r["valor"] for r in hist],
                mode="lines",
                line=dict(color=AZUL, width=2.5),
                fill="tozeroy",
                fillcolor="rgba(0,48,135,0.08)",
                name="Contagem",
            )
        )
        fig.add_hline(
            y=meta, line_dash="dash", line_color=VERM,
            annotation_text=f"Meta: {meta:,}",
            annotation_position="top right",
        )
    fig.update_layout(
        paper_bgcolor="white", plot_bgcolor="white",
        margin=dict(l=8, r=8, t=8, b=30),
        xaxis=dict(showgrid=False, tickangle=-30, tickfont=dict(size=10)),
        yaxis=dict(showgrid=True, gridcolor="#f0f0f0"),
        hovermode="x unified", showlegend=False,
    )

    gauge = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=prog,
            number={"suffix": "%", "font": {"size": 28, "color": AZUL}},
            gauge={
                "axis": {"range": [0, 100], "tickcolor": "#ccc"},
                "bar":  {"color": AZUL},
                "steps": [
                    {"range": [0,  50],  "color": "#f8f9fa"},
                    {"range": [50, 80],  "color": "#e8f4fd"},
                    {"range": [80, 100], "color": "#d4edda"},
                ],
                "threshold": {
                    "line": {"color": VERDE, "width": 3},
                    "thickness": 0.75,
                    "value": 100,
                },
            },
        )
    )
    gauge.update_layout(
        paper_bgcolor="white",
        margin=dict(l=20, r=20, t=10, b=10),
        height=180,
    )

    # ── Painel OS ativas (aberto + em_andamento) ──────────────────────────────
    todas_ordens = api("get", "/ordens", token=token) or []
    ordens_ativas = [
        o for o in todas_ordens
        if o.get("status") in ("aberto", "em_andamento")
    ][:5]

    _cor_os = {"aberto": AZUL, "em_andamento": VERDE}
    os_items = [
        html.Div(
            [
                html.Div(
                    [
                        html.Strong(o["os_id"], style={"color": AZUL, "fontSize": "13px"}),
                        html.Span(
                            o["status"].replace("_", " ").upper(),
                            style={
                                "fontSize": "10px",
                                "background": _cor_os.get(o["status"], CINZA),
                                "color": "#fff",
                                "borderRadius": "4px",
                                "padding": "1px 7px",
                                "marginLeft": "8px",
                            },
                        ),
                    ]
                ),
                html.Div(o["produto"], style={"fontSize": "12px", "color": "#666"}),
                html.Div(
                    f"Meta: {o['meta']:,} un | Resp: {o.get('responsavel', '—')}",
                    style={"fontSize": "11px", "color": "#aaa"},
                ),
            ],
            style={
                "borderBottom": "1px solid #f0f0f0",
                "paddingBottom": "10px",
                "marginBottom": "10px",
            },
        )
        for o in ordens_ativas
    ]

    painel = html.Div(
        [
            html.Div("Ordens Ativas", className="kpi-label mb-2"),
            *(
                os_items
                or [html.P("Nenhuma OS ativa.", style={"color": "#aaa", "fontSize": "13px"})]
            ),
        ]
    )

    # ── Log CNC ───────────────────────────────────────────────────────────────
    cnc_entries = api("get", "/cnc/log?limite=10", token=token) or []
    if not cnc_entries:
        cnc_view = html.P(
            "Nenhuma comunicação CNC registrada ainda. Inicie um lote para ver o fluxo.",
            style={"color": "#aaa", "fontSize": "13px", "marginTop": "8px"},
        )
    else:
        cnc_items = []
        for e in cnc_entries:
            sts   = e.get("status", "enviada")
            cor   = _CNC_STATUS_COR.get(sts, "#6c757d")
            icon  = _CNC_STATUS_ICON.get(sts, "•")
            ts    = (e.get("ts") or "")[:19].replace("T", " ")
            direcao = "Backend → CNC" if e.get("tipo") == "os_enviada" else "CNC → Backend"
            cnc_items.append(
                html.Div(
                    [
                        html.Span(
                            icon,
                            style={
                                "width": "22px", "display": "inline-block",
                                "color": cor, "fontWeight": "700",
                            },
                        ),
                        html.Span(
                            f"[{ts}] ",
                            style={"color": "#aaa", "fontSize": "11px", "marginRight": "6px"},
                        ),
                        html.Span(
                            direcao + " ",
                            style={"color": cor, "fontSize": "11px", "fontWeight": "600"},
                        ),
                        html.Span(
                            f"{e.get('os_id', '')}",
                            style={"color": AZUL, "fontSize": "11px", "fontWeight": "700",
                                   "marginRight": "6px"},
                        ),
                        html.Span(
                            e.get("mensagem", ""),
                            style={"color": "#555", "fontSize": "12px"},
                        ),
                    ],
                    style={
                        "padding": "5px 0",
                        "borderBottom": "1px solid #f5f5f5",
                        "lineHeight": "1.5",
                    },
                )
            )
        cnc_view = html.Div(cnc_items)

    return kpis, banner, fig, gauge, painel, cnc_view


# ══════════════════════════════════════════════════════════════════════════════
# OPERAÇÃO
# ══════════════════════════════════════════════════════════════════════════════

@callback(
    Output("op-kpis", "children"),
    Input("iv-op", "n_intervals"),
    State("auth-store", "data"),
    prevent_initial_call=True,
)
def atualizar_operacao(_, auth):
    token    = (auth or {}).get("token")
    est      = api("get", "/estado", token=token) or {}
    contagem = est.get("contagem", 0)
    meta     = est.get("meta", 1)
    status   = est.get("status", "idle")
    vel      = est.get("velocidade", 0)
    lote_id  = est.get("lote_id", "—")
    prog     = min(100, round(contagem / max(meta, 1) * 100, 1))
    return [
        dbc.Col(kpi_card("Contagem", f"{contagem:,}", f"Lote: {lote_id}", AZUL),   width=6, md=3),
        dbc.Col(kpi_card("Meta",     f"{meta:,}",     "unidades",         CINZA),  width=6, md=3),
        dbc.Col(kpi_card("Progresso", f"{prog}%",     "",
                         VERDE if prog >= 80 else AMAR),                            width=6, md=3),
        dbc.Col(kpi_card("Status",
                         STATUS_LABEL.get(status, status),
                         f"{vel} un/min",
                         STATUS_COR.get(status, CINZA)),                            width=6, md=3),
    ]


@callback(
    Output("cmd-fb", "children"),
    Input("btn-start", "n_clicks"),
    Input("btn-pause", "n_clicks"),
    Input("btn-stop",  "n_clicks"),
    Input("btn-reset", "n_clicks"),
    State("auth-store", "data"),
    prevent_initial_call=True,
)
def cmd_maquina(s, p, st, r, auth):
    if not any([s, p, st, r]):
        return dash.no_update
    token = (auth or {}).get("token")
    tid   = ctx.triggered_id

    if tid == "btn-reset":
        result = api("post", "/cmd/reset", token=token)
        msg, cor = ("↺ Contagem resetada.", CINZA) if result else ("Falha ao resetar.", VERM)
    else:
        cmd_map  = {"btn-start": "start", "btn-pause": "pause", "btn-stop": "stop"}
        label_map = {"start": "▶ Máquina iniciada.", "pause": "⏸ Pausado.", "stop": "⏹ Máquina parada."}
        cor_map   = {"start": VERDE, "pause": AMAR, "stop": VERM}
        cmd    = cmd_map[tid]
        result = api("post", f"/cmd/status?cmd={cmd}", token=token)
        msg    = label_map[cmd] if result else f"Falha ao executar '{cmd}'."
        cor    = cor_map.get(cmd, CINZA) if result else VERM

    return html.Span(msg, style={"color": cor, "fontSize": "13px"})


@callback(
    Output("lote-fb", "children"),
    Input("btn-iniciar-lote", "n_clicks"),
    State("inp-lote-id",   "value"),
    State("inp-lote-prod", "value"),
    State("inp-lote-meta", "value"),
    State("auth-store",    "data"),
    prevent_initial_call=True,
)
def iniciar_lote(n, lote_id, produto, meta, auth):
    if not n:
        return dash.no_update
    token = (auth or {}).get("token")
    if not token:
        return html.Span("Sem autorização.", style={"color": VERM})
    if not lote_id or not meta:
        return html.Span("Preencha Lote ID e Meta.", style={"color": AMAR})
    produto = produto or "Produto APSEN"
    r = api(
        "post",
        f"/cmd/lote?lote_id={lote_id}&meta={int(meta)}&produto={produto}",
        token=token,
    )
    if r and r.get("ok"):
        return html.Span(
            f"✔ Lote {lote_id} iniciado — OS gerada automaticamente.",
            style={"color": VERDE},
        )
    return html.Span("Falha ao iniciar lote.", style={"color": VERM})


# ══════════════════════════════════════════════════════════════════════════════
# ORDENS DE SERVIÇO
# ══════════════════════════════════════════════════════════════════════════════

@callback(
    Output("tbl-ordens", "children"),
    Input("btn-reload-os",      "n_clicks"),
    Input("btn-os-submit",      "n_clicks"),
    Input("iv-ordens-init",     "n_intervals"),
    State("auth-store", "data"),
    prevent_initial_call=True,
)
def carregar_ordens(reload_n, submit_n, _init, auth):
    # iv-ordens-init dispara 200ms após a página montar → carregamento inicial
    if not (reload_n or submit_n or _init):
        return dash.no_update
    token  = (auth or {}).get("token")
    ordens = api("get", "/ordens", token=token) or []
    if not ordens:
        return html.P("Nenhuma OS encontrada.", style={"color": "#aaa", "marginTop": "16px"})
    rows = [
        html.Tr(
            [
                html.Td(o["os_id"], style={"fontWeight": "700", "color": AZUL}),
                html.Td(o["produto"]),
                html.Td(o["lote_id"]),
                html.Td(f"{o['meta']:,} un"),
                html.Td(
                    html.Span(
                        o["status"].replace("_", " ").upper(),
                        style={
                            "background": STATUS_COR_OS.get(o["status"], CINZA),
                            "color": "#fff", "borderRadius": "4px",
                            "padding": "2px 8px", "fontSize": "11px",
                        },
                    )
                ),
                html.Td(o.get("responsavel", "—")),
                html.Td((o.get("criado_em") or "")[:10]),
            ]
        )
        for o in ordens
    ]
    return dbc.Table(
        [
            html.Thead(
                html.Tr([html.Th(h) for h in ["OS", "Produto", "Lote", "Meta", "Status", "Responsável", "Data"]])
            ),
            html.Tbody(rows),
        ],
        striped=True, bordered=False, hover=True, responsive=True, size="sm",
        style={"fontSize": "13px"},
    )


@callback(
    Output("modal-os", "is_open"),
    Output("os-fb",    "children"),
    Input("btn-nova-os",   "n_clicks"),
    Input("btn-nos-cancel","n_clicks"),
    Input("btn-os-submit", "n_clicks"),
    State("inp-os-produto", "value"),
    State("inp-os-lote",    "value"),
    State("inp-os-meta",    "value"),
    State("inp-os-resp",    "value"),
    State("modal-os",       "is_open"),
    State("auth-store",     "data"),
    prevent_initial_call=True,
)
def modal_os_cb(open_n, cancel_n, submit_n, produto, lote_id, meta, resp, is_open, auth):
    tid = ctx.triggered_id
    if tid == "btn-nova-os":
        if not open_n: return is_open, ""
        return True, ""
    if tid == "btn-nos-cancel":
        if not cancel_n: return is_open, ""
        return False, ""
    if tid == "btn-os-submit":
        if not submit_n: return is_open, ""
        if not produto or not lote_id or not meta:
            return is_open, html.Span("Preencha todos os campos obrigatórios.", style={"color": AMAR})
        token = (auth or {}).get("token")
        r = api(
            "post", "/ordens", token=token,
            json={"produto": produto, "lote_id": lote_id,
                  "meta": int(meta), "responsavel": resp or ""},
        )
        if r and r.get("os_id"):
            return False, ""
        return is_open, html.Span("Erro ao criar OS.", style={"color": VERM})
    return is_open, ""


# ══════════════════════════════════════════════════════════════════════════════
# MANUTENÇÃO
# ══════════════════════════════════════════════════════════════════════════════

@callback(
    Output("manut-diag",    "children"),
    Output("tbl-manut-log", "children"),
    Input("iv-manut", "n_intervals"),
    State("auth-store", "data"),
    prevent_initial_call=True,
)
def atualizar_manutencao(_, auth):
    token = (auth or {}).get("token")
    diag  = api("get", "/manutencao/diagnostico", token=token) or {}
    est   = diag.get("estado_maquina", {})
    mqtt_ok = diag.get("mqtt_conectado", False)

    diag_view = html.Div(
        [
            html.Div([html.Strong("Status MQTT: "),
                      html.Span("✓ Conectado" if mqtt_ok else "✗ Desconectado",
                                style={"color": VERDE if mqtt_ok else VERM})]),
            html.Div([html.Strong("Máquina: "),
                      html.Span(STATUS_LABEL.get(est.get("status", "idle"), "—"))]),
            html.Div([html.Strong("Lote atual: "),   html.Span(est.get("lote_id", "—"))]),
            html.Div([html.Strong("Contagem: "),      html.Span(f"{est.get('contagem', 0):,}")]),
            html.Div([html.Strong("Meta: "),           html.Span(f"{est.get('meta', 0):,}")]),
            html.Div([html.Strong("Velocidade: "),     html.Span(f"{est.get('velocidade', 0)} un/min")]),
            html.Div([
                html.Strong("Alarme: "),
                html.Span(
                    est.get("alarme") or "Nenhum",
                    style={"color": VERM if est.get("alarme") else VERDE},
                ),
            ]),
        ],
        style={"fontSize": "13px", "lineHeight": "2"},
    )

    logs = api("get", "/manutencao/log?limite=20", token=token) or []
    if not logs:
        log_view = html.P("Sem registros de manutenção.", style={"color": "#aaa", "fontSize": "13px"})
    else:
        rows = [
            html.Tr([
                html.Td(l["tipo"],       style={"textTransform": "capitalize"}),
                html.Td((l["descricao"] or "")[:60] + ("…" if len(l.get("descricao", "")) > 60 else "")),
                html.Td(l.get("componente", "—")),
                html.Td(l.get("responsavel", "—")),
                html.Td((l.get("ts") or "")[:16]),
            ])
            for l in logs
        ]
        log_view = dbc.Table(
            [
                html.Thead(html.Tr([html.Th(h) for h in
                    ["Tipo", "Descrição", "Componente", "Técnico", "Data/Hora"]])),
                html.Tbody(rows),
            ],
            striped=True, bordered=False, hover=True, responsive=True, size="sm",
            style={"fontSize": "12px"},
        )
    return diag_view, log_view


@callback(
    Output("manut-cmd-fb", "children"),
    Input("btn-manut-cmd", "n_clicks"),
    State("sel-manut-cmd",  "value"),
    State("inp-manut-comp", "value"),
    State("auth-store",     "data"),
    prevent_initial_call=True,
)
def executar_cmd_manut(n, cmd, comp, auth):
    if not n:
        return dash.no_update
    if not cmd:
        return html.Span("Selecione um comando.", style={"color": AMAR})
    token = (auth or {}).get("token")
    r = api("post", f"/manutencao/cmd?cmd={cmd}&componente={comp or ''}", token=token)
    if r and r.get("ok"):
        return html.Span(f"✔ Comando '{cmd}' enviado.", style={"color": VERDE})
    return html.Span("Falha ao enviar comando.", style={"color": VERM})


@callback(
    Output("manut-log-fb", "children"),
    Input("btn-manut-log",  "n_clicks"),
    State("inp-manut-tipo",  "value"),
    State("inp-manut-desc",  "value"),
    State("inp-manut-comp2", "value"),
    State("auth-store",      "data"),
    prevent_initial_call=True,
)
def registrar_manut(n, tipo, desc, comp, auth):
    if not n:
        return dash.no_update
    if not tipo or not desc:
        return html.Span("Tipo e Descrição são obrigatórios.", style={"color": AMAR})
    token = (auth or {}).get("token")
    r = api(
        "post", "/manutencao/log", token=token,
        json={"tipo": tipo, "descricao": desc, "componente": comp or ""},
    )
    if r and r.get("ok"):
        return html.Span("✔ Ocorrência registrada com sucesso.", style={"color": VERDE})
    return html.Span("Falha ao registrar ocorrência.", style={"color": VERM})


# ══════════════════════════════════════════════════════════════════════════════
# USUÁRIOS
# ══════════════════════════════════════════════════════════════════════════════

@callback(
    Output("tbl-users", "children"),
    Input("btn-novo-user",   "n_clicks"),
    Input("btn-user-submit", "n_clicks"),
    Input("iv-users-init",   "n_intervals"),
    State("auth-store", "data"),
    prevent_initial_call=True,
)
def carregar_usuarios(open_n, sub_n, _init, auth):
    if not (open_n or sub_n or _init):
        return dash.no_update
    token = (auth or {}).get("token")
    users = api("get", "/usuarios", token=token) or []
    if not users:
        return html.P("Nenhum usuário encontrado.", style={"color": "#aaa"})
    rows = [
        html.Tr(
            [
                html.Td(u["username"], style={"fontWeight": "700"}),
                html.Td(u["nome_completo"]),
                html.Td(
                    html.Span(
                        u["role"].upper(),
                        style={"background": AZUL, "color": "#fff",
                               "borderRadius": "4px", "padding": "1px 8px", "fontSize": "11px"},
                    )
                ),
                html.Td(
                    html.Span(
                        "✓ Ativo" if u["ativo"] else "Inativo",
                        style={"color": VERDE if u["ativo"] else CINZA},
                    )
                ),
                html.Td((u.get("criado_em") or "")[:10]),
            ]
        )
        for u in users
    ]
    return dbc.Table(
        [
            html.Thead(html.Tr([html.Th(h) for h in
                ["Username", "Nome Completo", "Função", "Status", "Criado em"]])),
            html.Tbody(rows),
        ],
        striped=True, bordered=False, hover=True, responsive=True, size="sm",
    )


@callback(
    Output("modal-user", "is_open"),
    Output("user-fb",    "children"),
    Input("btn-novo-user",   "n_clicks"),
    Input("btn-user-cancel", "n_clicks"),
    Input("btn-user-submit", "n_clicks"),
    State("inp-user-name",  "value"),
    State("inp-user-senha", "value"),
    State("inp-user-nome",  "value"),
    State("inp-user-role",  "value"),
    State("modal-user",     "is_open"),
    State("auth-store",     "data"),
    prevent_initial_call=True,
)
def modal_user_cb(open_n, cancel_n, submit_n, username, senha, nome, role, is_open, auth):
    tid = ctx.triggered_id
    if tid == "btn-novo-user":
        if not open_n: return is_open, ""
        return True, ""
    if tid == "btn-user-cancel":
        if not cancel_n: return is_open, ""
        return False, ""
    if tid == "btn-user-submit":
        if not submit_n: return is_open, ""
        if not username or not senha or not role:
            return is_open, html.Span("Username, senha e função são obrigatórios.", style={"color": AMAR})
        token = (auth or {}).get("token")
        r = api(
            "post", "/usuarios", token=token,
            json={
                "username": username, "senha": senha,
                "role": role, "nome_completo": nome or username,
            },
        )
        if r and r.get("ok"):
            return False, ""
        return is_open, html.Span("Erro ao criar usuário. Username pode já existir.", style={"color": VERM})
    return is_open, ""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8050, debug=False)
