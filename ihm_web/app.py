"""
APSEN - IHM de Manutenção v2.0 (porta 8051)
=============================================
Aplicação separada — exclusivamente para técnicos de manutenção.
Requer autenticação JWT (usuários gerenciados no banco MySQL).

Funcionalidades:
  • Login com JWT (técnico de manutenção ou admin)
  • Dashboard de temperaturas por componente (CNC + dispensers)
  • Indicadores de desgaste (%) e horas de uso
  • Log de manutenções realizadas
  • Alarmes: visualização e resolução
  • Registro de nova manutenção

Backend: http://backend:8000 (endpoints /manutencao/* e /auth/*)
Usuários padrão: admin/admin123 — manut1/mnt123
"""

import os

import dash
import requests
from dash import Input, Output, State, callback, ctx, dcc, html
import dash_bootstrap_components as dbc

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
POLL_MS     = int(os.getenv("POLL_MS", "5000"))

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.CYBORG],
    title="APSEN — Manutenção",
    update_title=None,
    suppress_callback_exceptions=True,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _api(method: str, path: str, token: str = None, json_body=None) -> tuple[int, any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        fn = getattr(requests, method)
        r  = fn(f"{BACKEND_URL}{path}", json=json_body, headers=headers, timeout=5)
        return r.status_code, r.json()
    except Exception as exc:
        return 0, {"detail": str(exc)}


def _label(text: str, color: str = "secondary") -> dbc.Badge:
    return dbc.Badge(text, color=color, className="me-1")


# Cor por % de desgaste
def _cor_desgaste(pct: float) -> str:
    if pct >= 80:
        return "danger"
    if pct >= 60:
        return "warning"
    return "success"


# ── Layout ─────────────────────────────────────────────────────────────────────

_SIDEBAR_LINKS = [
    ("🌡 Temperaturas",    "temp"),
    ("⚙ Desgaste / Uso",  "uso"),
    ("📋 Log Manutenção",  "log"),
    ("🚨 Alarmes",         "alarmes"),
    ("➕ Nova Manutenção", "nova"),
]

app.layout = dbc.Container(
    fluid=True,
    children=[
        dcc.Interval(id="poll", interval=POLL_MS, n_intervals=0),
        dcc.Store(id="jwt-token",   storage_type="session"),
        dcc.Store(id="user-nome",   storage_type="session"),
        dcc.Store(id="active-tab",  data="temp"),

        # ── Tela de Login ─────────────────────────────────────────────────────
        html.Div(
            id="tela-login",
            children=[
                dbc.Row(
                    dbc.Col(
                        dbc.Card([
                            dbc.CardHeader(
                                html.H4("APSEN — Manutenção", className="text-center mb-0")
                            ),
                            dbc.CardBody([
                                dbc.Input(
                                    id="inp-user", placeholder="Usuário",
                                    type="text", className="mb-2",
                                ),
                                dbc.Input(
                                    id="inp-senha", placeholder="Senha",
                                    type="password", className="mb-3",
                                ),
                                dbc.Button(
                                    "Entrar", id="btn-login",
                                    color="primary", className="w-100",
                                ),
                                html.Div(id="msg-login", className="mt-2 text-danger small"),
                            ]),
                        ]),
                        md=4, lg=3,
                        className="mx-auto mt-5",
                    )
                )
            ],
        ),

        # ── Tela Principal (oculta até login) ─────────────────────────────────
        html.Div(
            id="tela-principal",
            style={"display": "none"},
            children=[
                # Navbar
                dbc.Navbar(
                    dbc.Container([
                        dbc.NavbarBrand("APSEN Manutenção", className="fw-bold"),
                        html.Span(id="span-username", className="text-muted me-auto ms-3 small"),
                        dbc.Button("Sair", id="btn-logout", color="outline-secondary", size="sm"),
                    ], fluid=True),
                    dark=True, color="dark", className="mb-3",
                ),

                dbc.Row([
                    # ── Sidebar ───────────────────────────────────────────────
                    dbc.Col(
                        dbc.Nav(
                            [
                                dbc.NavLink(
                                    label, id=f"nav-{tab_id}", href="#",
                                    className="text-light border-bottom py-2",
                                )
                                for label, tab_id in _SIDEBAR_LINKS
                            ],
                            vertical=True, pills=True,
                        ),
                        md=2, className="border-end pt-2",
                    ),

                    # ── Conteúdo ──────────────────────────────────────────────
                    dbc.Col(
                        html.Div(id="conteudo-principal"),
                        md=10,
                    ),
                ]),
            ],
        ),
    ],
)


# ── Login / Logout ─────────────────────────────────────────────────────────────

@callback(
    Output("jwt-token",       "data"),
    Output("user-nome",       "data"),
    Output("msg-login",       "children"),
    Output("tela-login",      "style"),
    Output("tela-principal",  "style"),
    Output("span-username",   "children"),
    Input("btn-login",  "n_clicks"),
    State("inp-user",   "value"),
    State("inp-senha",  "value"),
    prevent_initial_call=True,
)
def _login(_, username, senha):
    oculto = {"display": "none"}
    visivel = {"display": "block"}
    if not username or not senha:
        return None, None, "Preencha usuário e senha.", visivel, oculto, ""

    code, data = _api("post", "/auth/login", json_body={
        "username": username, "senha": senha,
    })
    if code == 200:
        token = data.get("token", "")
        nome  = data.get("nome", username)
        return token, nome, "", oculto, visivel, f"Logado como {nome}"
    else:
        return None, None, "Credenciais inválidas.", visivel, oculto, ""


@callback(
    Output("jwt-token",      "data",  allow_duplicate=True),
    Output("tela-login",     "style", allow_duplicate=True),
    Output("tela-principal", "style", allow_duplicate=True),
    Input("btn-logout", "n_clicks"),
    prevent_initial_call=True,
)
def _logout(_):
    return None, {"display": "block"}, {"display": "none"}


# ── Navegação ──────────────────────────────────────────────────────────────────

@callback(
    Output("active-tab", "data"),
    [Input(f"nav-{t}", "n_clicks") for _, t in _SIDEBAR_LINKS],
    prevent_initial_call=True,
)
def _nav(*_):
    triggered = ctx.triggered_id or "nav-temp"
    return triggered.replace("nav-", "")


# ── Conteúdo principal ────────────────────────────────────────────────────────

@callback(
    Output("conteudo-principal", "children"),
    Input("active-tab",   "data"),
    Input("poll",         "n_intervals"),
    State("jwt-token",    "data"),
    prevent_initial_call=True,
)
def _render_conteudo(tab: str, _, token: str):
    if not token:
        return html.P("Faça login para continuar.", className="text-muted")

    if tab == "temp":
        return _render_temp(token)
    elif tab == "uso":
        return _render_uso(token)
    elif tab == "log":
        return _render_log(token)
    elif tab == "alarmes":
        return _render_alarmes(token)
    elif tab == "nova":
        return _render_nova_manut()
    return html.P("Selecione uma seção.", className="text-muted")


# ─── Temperaturas ──────────────────────────────────────────────────────────────

def _render_temp(token: str):
    _, leituras = _api("get", "/manutencao/sensores", token=token)
    if not isinstance(leituras, list):
        return html.P("Erro ao carregar sensores.", className="text-danger small")

    cards = []
    for leit in leituras:
        if leit.get("tipo") != "temperatura":
            continue
        val = leit.get("valor", 0)
        cor = "danger" if val > 65 else ("warning" if val > 50 else "success")
        cards.append(
            dbc.Col(
                dbc.Card([
                    dbc.CardHeader(
                        html.Small(leit.get("componente", "?"), className="fw-bold")
                    ),
                    dbc.CardBody([
                        html.H3(f"{val}°C", className=f"text-{cor} text-center"),
                        dbc.Progress(
                            value=min(val / 85 * 100, 100),
                            color=cor, style={"height": "8px"},
                        ),
                        html.Small(
                            str(leit.get("coletado_em", ""))[:16],
                            className="text-muted d-block text-end mt-1",
                        ),
                    ]),
                ]),
                md=3, sm=6, xs=12, className="mb-3",
            )
        )

    return html.Div([
        html.H5("🌡 Temperaturas dos Componentes", className="mb-3"),
        dbc.Row(cards) if cards else html.P("Sem leituras.", className="text-muted small"),
    ])


# ─── Desgaste / Uso ────────────────────────────────────────────────────────────

def _render_uso(token: str):
    _, leituras = _api("get", "/manutencao/sensores", token=token)
    if not isinstance(leituras, list):
        return html.P("Erro ao carregar sensores.", className="text-danger small")

    rows = []
    for leit in leituras:
        tipo = leit.get("tipo", "")
        if tipo == "temperatura":
            continue
        val  = leit.get("valor", 0)
        unid = leit.get("unidade", "")
        cor  = _cor_desgaste(val) if "%" in unid else "info"
        rows.append(
            dbc.Row([
                dbc.Col(
                    html.Small(leit.get("componente", "?"), className="fw-bold"),
                    md=4,
                ),
                dbc.Col(
                    html.Small(tipo, className="text-muted"),
                    md=3,
                ),
                dbc.Col([
                    html.Span(f"{val} {unid}", className=f"text-{cor} fw-bold small"),
                    dbc.Progress(
                        value=min(val, 100) if "%" in unid else 0,
                        color=cor,
                        style={"height": "5px", "marginTop": "4px"},
                    ) if "%" in unid else html.Div(),
                ], md=5),
            ], className="mb-2 border-bottom pb-1")
        )

    return html.Div([
        html.H5("⚙ Desgaste e Horas de Uso", className="mb-3"),
        html.Div(rows) if rows else html.P("Sem leituras.", className="text-muted small"),
    ])


# ─── Log de Manutenção ─────────────────────────────────────────────────────────

def _render_log(token: str):
    _, logs = _api("get", "/manutencao/log?limite=50", token=token)
    if not isinstance(logs, list):
        return html.P("Erro ao carregar log.", className="text-danger small")

    rows = [
        dbc.Row([
            dbc.Col(html.Small(str(l.get("realizado_em", ""))[:16],
                               className="text-muted"), md=2),
            dbc.Col(html.Small(l.get("componente", "?"), className="fw-bold"), md=3),
            dbc.Col(html.Small(l.get("tipo", "?"),
                               className="text-info"), md=2),
            dbc.Col(html.Small(l.get("descricao", ""),
                               className="text-light"), md=4),
            dbc.Col(html.Small(l.get("tecnico", "?"),
                               className="text-muted"), md=1),
        ], className="mb-1 border-bottom pb-1")
        for l in logs
    ]

    return html.Div([
        html.H5("📋 Log de Manutenções", className="mb-3"),
        dbc.Row([
            dbc.Col(html.Small("Data", className="text-muted fw-bold"), md=2),
            dbc.Col(html.Small("Componente", className="text-muted fw-bold"), md=3),
            dbc.Col(html.Small("Tipo", className="text-muted fw-bold"), md=2),
            dbc.Col(html.Small("Descrição", className="text-muted fw-bold"), md=4),
            dbc.Col(html.Small("Técnico", className="text-muted fw-bold"), md=1),
        ], className="mb-2"),
        html.Div(rows) if rows else html.P(
            "Nenhuma manutenção registrada.", className="text-muted small"
        ),
    ])


# ─── Alarmes ───────────────────────────────────────────────────────────────────

def _render_alarmes(token: str):
    _, alarmes = _api("get", "/manutencao/alarmes?resolvido=false&limite=50", token=token)
    if not isinstance(alarmes, list):
        return html.P("Erro ao carregar alarmes.", className="text-danger small")

    if not alarmes:
        return html.Div([
            html.H5("🚨 Alarmes", className="mb-3"),
            dbc.Alert("Nenhum alarme ativo.", color="success"),
        ])

    items = [
        dbc.ListGroupItem([
            dbc.Row([
                dbc.Col([
                    html.Strong(f"[{a.get('tipo','').upper()}] "),
                    html.Span(a.get("descricao", "")),
                    html.Br(),
                    html.Small(
                        f"Fonte: {a.get('fonte','?')} | "
                        f"{str(a.get('criado_em',''))[:16]}",
                        className="text-muted",
                    ),
                ], md=9),
                dbc.Col(
                    dbc.Button(
                        "Resolver",
                        id={"type": "btn-resolver", "index": a.get("id", 0)},
                        color="warning", size="sm",
                    ),
                    md=3, className="d-flex align-items-center",
                ),
            ])
        ], color="danger", className="mb-1")
        for a in alarmes
    ]

    return html.Div([
        html.H5("🚨 Alarmes Ativos", className="mb-3"),
        dbc.ListGroup(items),
    ])


# ─── Nova Manutenção ───────────────────────────────────────────────────────────

def _render_nova_manut():
    return html.Div([
        html.H5("➕ Registrar Manutenção", className="mb-3"),
        dbc.Form([
            dbc.Row([
                dbc.Col([
                    dbc.Label("Tipo"),
                    dbc.Select(
                        id="nm-tipo",
                        options=[
                            {"label": "Preventiva",  "value": "preventiva"},
                            {"label": "Corretiva",   "value": "corretiva"},
                            {"label": "Preditiva",   "value": "preditiva"},
                            {"label": "Calibração",  "value": "calibracao"},
                            {"label": "Substituição","value": "substituicao"},
                        ],
                        value="preventiva",
                    ),
                ], md=3),
                dbc.Col([
                    dbc.Label("Componente"),
                    dbc.Input(id="nm-componente", placeholder="ex: motor_eixo_x"),
                ], md=4),
            ], className="mb-3"),
            dbc.Row([
                dbc.Col([
                    dbc.Label("Descrição"),
                    dbc.Textarea(
                        id="nm-descricao",
                        placeholder="Descreva o serviço realizado...",
                        rows=4,
                    ),
                ]),
            ], className="mb-3"),
            dbc.Button(
                "Salvar", id="btn-salvar-manut",
                color="success", className="me-2",
            ),
            html.Div(id="msg-manut", className="mt-2"),
        ]),
    ])


# ── Callback: resolver alarme ──────────────────────────────────────────────────

@callback(
    Output("active-tab", "data", allow_duplicate=True),
    Input({"type": "btn-resolver", "index": dash.ALL}, "n_clicks"),
    State("jwt-token", "data"),
    prevent_initial_call=True,
)
def _resolver_alarme(n_clicks_list, token):
    if not any(n for n in (n_clicks_list or []) if n):
        return dash.no_update
    if not token:
        return dash.no_update

    triggered = ctx.triggered_id
    if triggered and isinstance(triggered, dict):
        alarme_id = triggered.get("index", 0)
        _api("put", f"/manutencao/alarmes/{alarme_id}/resolver", token=token)

    return "alarmes"  # Recarrega a aba de alarmes


# ── Callback: salvar manutenção ───────────────────────────────────────────────

@callback(
    Output("msg-manut",     "children"),
    Output("nm-descricao",  "value"),
    Output("nm-componente", "value"),
    Input("btn-salvar-manut", "n_clicks"),
    State("nm-tipo",        "value"),
    State("nm-componente",  "value"),
    State("nm-descricao",   "value"),
    State("jwt-token",      "data"),
    prevent_initial_call=True,
)
def _salvar_manut(_, tipo, componente, descricao, token):
    if not token:
        return dbc.Alert("Sessão expirada. Faça login novamente.", color="danger"), "", ""
    if not componente or not descricao:
        return dbc.Alert("Preencha componente e descrição.", color="warning"), descricao, componente

    code, data = _api("post", "/manutencao/log", token=token, json_body={
        "tipo":       tipo,
        "componente": componente,
        "descricao":  descricao,
    })
    if code == 200:
        return dbc.Alert("Manutenção registrada com sucesso!", color="success"), "", ""
    else:
        return dbc.Alert(f"Erro: {data}", color="danger"), descricao, componente


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8051")),
        debug=os.getenv("DEBUG", "false").lower() == "true",
    )
