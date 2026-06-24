"""
APSEN - IHM de Manutenção v2.1 (porta 8051)
=============================================
Requer autenticação JWT. Usuários: admin/admin123 | manut1/mnt123

Funcionalidades:
  • Login / Logout com JWT (role: admin | manutencao)
  • 🌡 Temperaturas por componente
  • ⚙  Desgaste / Horas de uso
  • 📋 Log de manutenções
  • 🚨 Alarmes (visualizar + resolver)
  • ➕ Nova Manutenção
  • 📦 Dispensers — estado residual + botão Limpar (novo)
  • 🗂  Ordens (OS) — histórico + alterar status + ver JSON (novo)
  • 👥 Usuários — criar / editar role / ativar / desativar (novo, admin)
"""

import json
import os

import dash
import requests
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update
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
        r  = fn(f"{BACKEND_URL}{path}", json=json_body, headers=headers, timeout=6)
        return r.status_code, r.json()
    except Exception as exc:
        return 0, {"detail": str(exc)}


def _cor_desgaste(pct: float) -> str:
    if pct >= 80:  return "danger"
    if pct >= 60:  return "warning"
    return "success"


def _cor_status_os(status: str) -> str:
    return {
        "aguardando":   "secondary",
        "em_andamento": "warning",
        "concluida":    "success",
        "erro":         "danger",
        "cancelada":    "dark",
    }.get(status, "secondary")


def _cor_disp(qty: int, cap: int = 100) -> str:
    pct = qty / cap * 100 if cap else 0
    if pct >= 60: return "success"
    if pct >= 20: return "warning"
    return "danger" if qty > 0 else "secondary"


# ── Sidebar links ──────────────────────────────────────────────────────────────

_SIDEBAR_LINKS = [
    ("🌡 Temperaturas",    "temp"),
    ("⚙ Desgaste / Uso",  "uso"),
    ("📋 Log Manutenção",  "log"),
    ("🚨 Alarmes",         "alarmes"),
    ("➕ Nova Manutenção", "nova"),
    ("📦 Dispensers",      "dispensers"),
    ("🗂 Ordens (OS)",     "ordens"),
    ("👥 Usuários",        "usuarios"),
]

# ── Layout ─────────────────────────────────────────────────────────────────────

app.layout = dbc.Container(
    fluid=True,
    children=[
        dcc.Interval(id="poll", interval=POLL_MS, n_intervals=0),
        dcc.Store(id="jwt-token",  storage_type="session"),
        dcc.Store(id="user-nome",  storage_type="session"),
        dcc.Store(id="user-role",  storage_type="session"),
        dcc.Store(id="active-tab", data="temp"),
        dcc.Store(id="os-modal-id", data=None),  # OS selecionada para modal

        # ── Modal: detalhe JSON da OS ──────────────────────────────────────────
        dbc.Modal(
            [
                dbc.ModalHeader(dbc.ModalTitle(id="os-modal-title")),
                dbc.ModalBody(id="os-modal-body"),
                dbc.ModalFooter(
                    dbc.Button("Fechar", id="os-modal-close", color="secondary", size="sm")
                ),
            ],
            id="os-modal",
            size="xl",
            scrollable=True,
            is_open=False,
        ),

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
                                    debounce=True,
                                ),
                                dbc.Button(
                                    "Entrar", id="btn-login",
                                    color="primary", className="w-100",
                                ),
                                html.Div(id="msg-login", className="mt-2 text-danger small"),
                            ]),
                        ]),
                        md=4, lg=3, className="mx-auto mt-5",
                    )
                )
            ],
        ),

        # ── Tela Principal ────────────────────────────────────────────────────
        html.Div(
            id="tela-principal",
            style={"display": "none"},
            children=[
                dbc.Navbar(
                    dbc.Container([
                        dbc.NavbarBrand("APSEN Manutenção", className="fw-bold"),
                        html.Span(id="span-username", className="text-muted me-auto ms-3 small"),
                        dbc.Button("Sair", id="btn-logout", color="outline-secondary", size="sm"),
                    ], fluid=True),
                    dark=True, color="dark", className="mb-3",
                ),
                dbc.Row([
                    # Sidebar
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
                    # Conteúdo
                    dbc.Col(html.Div(id="conteudo-principal"), md=10),
                ]),
            ],
        ),
    ],
)


# ── Login / Logout ─────────────────────────────────────────────────────────────

@callback(
    Output("jwt-token",      "data"),
    Output("user-nome",      "data"),
    Output("user-role",      "data"),
    Output("msg-login",      "children"),
    Output("tela-login",     "style"),
    Output("tela-principal", "style"),
    Output("span-username",  "children"),
    Input("btn-login",  "n_clicks"),
    Input("inp-senha",  "n_submit"),
    State("inp-user",   "value"),
    State("inp-senha",  "value"),
    prevent_initial_call=True,
)
def _login(_, __, username, senha):
    oculto  = {"display": "none"}
    visivel = {"display": "block"}
    if not username or not senha:
        return None, None, None, "Preencha usuário e senha.", visivel, oculto, ""

    code, data = _api("post", "/auth/login", json_body={"username": username, "senha": senha})
    if code == 200:
        token = data.get("token", "")
        nome  = data.get("nome", username)
        role  = data.get("role", "manutencao")
        return token, nome, role, "", oculto, visivel, f"{nome} [{role}]"
    return None, None, None, "Credenciais inválidas.", visivel, oculto, ""


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
    Input("active-tab",  "data"),
    Input("poll",        "n_intervals"),
    State("jwt-token",   "data"),
    State("user-role",   "data"),
    prevent_initial_call=True,
)
def _render_conteudo(tab, _, token, role):
    if not token:
        return html.P("Faça login para continuar.", className="text-muted")

    renderers = {
        "temp":       lambda: _render_temp(token),
        "uso":        lambda: _render_uso(token),
        "log":        lambda: _render_log(token),
        "alarmes":    lambda: _render_alarmes(token),
        "nova":       lambda: _render_nova_manut(),
        "dispensers": lambda: _render_dispensers(token),
        "ordens":     lambda: _render_ordens(token),
        "usuarios":   lambda: _render_usuarios(token, role),
    }
    fn = renderers.get(tab)
    return fn() if fn else html.P("Selecione uma seção.", className="text-muted")


# ═══════════════════════════════════════════════════════════════════════════════
# Renderizadores de cada aba
# ═══════════════════════════════════════════════════════════════════════════════

# ── Temperaturas ───────────────────────────────────────────────────────────────

def _render_temp(token):
    _, leituras = _api("get", "/manutencao/sensores", token=token)
    if not isinstance(leituras, list):
        return html.P("Erro ao carregar sensores.", className="text-danger small")

    cards = []
    for leit in leituras:
        if leit.get("tipo") != "temperatura":
            continue
        val = leit.get("valor", 0)
        cor = "danger" if val > 65 else ("warning" if val > 50 else "success")
        cards.append(dbc.Col(dbc.Card([
            dbc.CardHeader(html.Small(leit.get("componente","?"), className="fw-bold")),
            dbc.CardBody([
                html.H3(f"{val}°C", className=f"text-{cor} text-center"),
                dbc.Progress(value=min(val/85*100,100), color=cor, style={"height":"8px"}),
                html.Small(str(leit.get("ts",""))[:16], className="text-muted d-block text-end mt-1"),
            ]),
        ]), md=3, sm=6, xs=12, className="mb-3"))

    return html.Div([
        html.H5("🌡 Temperaturas dos Componentes", className="mb-3"),
        dbc.Row(cards) if cards else html.P("Sem leituras.", className="text-muted small"),
    ])


# ── Desgaste / Uso ─────────────────────────────────────────────────────────────

def _render_uso(token):
    _, leituras = _api("get", "/manutencao/sensores", token=token)
    if not isinstance(leituras, list):
        return html.P("Erro ao carregar sensores.", className="text-danger small")

    rows = []
    for leit in leituras:
        if leit.get("tipo") == "temperatura":
            continue
        val = leit.get("valor", 0); unid = leit.get("unidade","")
        cor = _cor_desgaste(val) if "%" in unid else "info"
        rows.append(dbc.Row([
            dbc.Col(html.Small(leit.get("componente","?"), className="fw-bold"), md=4),
            dbc.Col(html.Small(leit.get("tipo","?"), className="text-muted"), md=3),
            dbc.Col([
                html.Span(f"{val} {unid}", className=f"text-{cor} fw-bold small"),
                dbc.Progress(value=min(val,100) if "%" in unid else 0, color=cor,
                             style={"height":"5px","marginTop":"4px"}) if "%" in unid else html.Div(),
            ], md=5),
        ], className="mb-2 border-bottom pb-1"))

    return html.Div([
        html.H5("⚙ Desgaste e Horas de Uso", className="mb-3"),
        html.Div(rows) if rows else html.P("Sem leituras.", className="text-muted small"),
    ])


# ── Log de Manutenção ──────────────────────────────────────────────────────────

def _render_log(token):
    _, logs = _api("get", "/manutencao/log?limite=50", token=token)
    if not isinstance(logs, list):
        return html.P("Erro ao carregar log.", className="text-danger small")

    header = dbc.Row([
        dbc.Col(html.Small("Data", className="text-muted fw-bold"), md=2),
        dbc.Col(html.Small("Componente", className="text-muted fw-bold"), md=3),
        dbc.Col(html.Small("Tipo", className="text-muted fw-bold"), md=2),
        dbc.Col(html.Small("Descrição", className="text-muted fw-bold"), md=4),
        dbc.Col(html.Small("Técnico", className="text-muted fw-bold"), md=1),
    ], className="mb-2")

    rows = [dbc.Row([
        dbc.Col(html.Small(str(l.get("ts",""))[:16], className="text-muted"), md=2),
        dbc.Col(html.Small(l.get("componente","?"), className="fw-bold"), md=3),
        dbc.Col(html.Small(l.get("tipo","?"), className="text-info"), md=2),
        dbc.Col(html.Small(l.get("descricao",""), className="text-light"), md=4),
        dbc.Col(html.Small(l.get("tecnico","?"), className="text-muted"), md=1),
    ], className="mb-1 border-bottom pb-1") for l in logs]

    return html.Div([
        html.H5("📋 Log de Manutenções", className="mb-3"),
        header,
        html.Div(rows) if rows else html.P("Nenhuma manutenção registrada.", className="text-muted small"),
    ])


# ── Alarmes ────────────────────────────────────────────────────────────────────

def _render_alarmes(token):
    _, alarmes = _api("get", "/manutencao/alarmes?resolvido=false&limite=50", token=token)
    if not isinstance(alarmes, list):
        return html.P("Erro ao carregar alarmes.", className="text-danger small")

    if not alarmes:
        return html.Div([html.H5("🚨 Alarmes", className="mb-3"),
                         dbc.Alert("Nenhum alarme ativo.", color="success")])

    items = [dbc.ListGroupItem([
        dbc.Row([
            dbc.Col([
                html.Strong(f"[{a.get('tipo','').upper()}] "),
                html.Span(a.get("descricao","")),
                html.Br(),
                html.Small(f"Fonte: {a.get('fonte','?')} | {str(a.get('ts',''))[:16]}",
                           className="text-muted"),
            ], md=9),
            dbc.Col(dbc.Button("Resolver",
                               id={"type":"btn-resolver","index":a.get("id",0)},
                               color="warning", size="sm"),
                    md=3, className="d-flex align-items-center"),
        ])
    ], color="danger", className="mb-1") for a in alarmes]

    return html.Div([html.H5("🚨 Alarmes Ativos", className="mb-3"), dbc.ListGroup(items)])


# ── Nova Manutenção ────────────────────────────────────────────────────────────

def _render_nova_manut():
    return html.Div([
        html.H5("➕ Registrar Manutenção", className="mb-3"),
        dbc.Form([
            dbc.Row([
                dbc.Col([
                    dbc.Label("Tipo"),
                    dbc.Select(id="nm-tipo", value="preventiva", options=[
                        {"label":"Preventiva",  "value":"preventiva"},
                        {"label":"Corretiva",   "value":"corretiva"},
                        {"label":"Preditiva",   "value":"preditiva"},
                        {"label":"Calibração",  "value":"calibracao"},
                        {"label":"Substituição","value":"substituicao"},
                        {"label":"Limpeza",     "value":"limpeza"},
                    ]),
                ], md=3),
                dbc.Col([
                    dbc.Label("Componente"),
                    dbc.Input(id="nm-componente", placeholder="ex: motor_eixo_x"),
                ], md=4),
            ], className="mb-3"),
            dbc.Row([
                dbc.Col([
                    dbc.Label("Descrição"),
                    dbc.Textarea(id="nm-descricao",
                                 placeholder="Descreva o serviço realizado...", rows=4),
                ]),
            ], className="mb-3"),
            dbc.Button("Salvar", id="btn-salvar-manut", color="success", className="me-2"),
            html.Div(id="msg-manut", className="mt-2"),
        ]),
    ])


# ── Dispensers ─────────────────────────────────────────────────────────────────

def _render_dispensers(token):
    _, dispensers = _api("get", "/dispensers/estado", token=token)
    if not isinstance(dispensers, list):
        dispensers = []

    cards = []
    for d in dispensers:
        d_id  = d.get("dispenser_id", "?")
        med_raw = d.get("medicamento")
        qty   = d.get("quantidade_atual", 0)
        med   = med_raw if med_raw and qty > 0 else ("— Vazio —" if med_raw and qty == 0 else "— Livre —")
        cat   = d.get("categoria") or "—"
        cap   = d.get("capacidade", 100)
        os_id = d.get("ultima_os_id") or "—"
        cor   = _cor_disp(qty, cap)
        pct   = round(qty / cap * 100) if cap else 0

        cards.append(dbc.Col(dbc.Card([
            dbc.CardHeader(
                dbc.Row([
                    dbc.Col(html.Strong(f"Dispenser {d_id}"), width="auto"),
                    dbc.Col(dbc.Badge(cat.upper(), color="info", className="ms-1"), width="auto"),
                ])
            ),
            dbc.CardBody([
                html.P([html.Strong("Medicamento: "), med], className="mb-1 small"),
                html.P([html.Strong("Residual: "),
                        html.Span(f"{qty} / {cap} un.", className=f"text-{cor} fw-bold")],
                       className="mb-1 small"),
                dbc.Progress(value=pct, color=cor, style={"height":"10px"}, className="mb-2"),
                html.P([html.Strong("Última OS: "), html.Small(os_id, className="text-muted")],
                       className="mb-2 small"),
                dbc.Button(
                    "🧹 Limpar Dispenser",
                    id={"type": "btn-limpar-disp", "index": d_id},
                    color="outline-warning", size="sm", className="w-100",
                ),
            ]),
        ], className="h-100"), md=4, className="mb-3"))

    return html.Div([
        html.H5("📦 Estado dos Dispensers", className="mb-1"),
        html.P("O residual persiste entre ordens. Use Limpar para zerar manualmente antes "
               "de trocar o medicamento.", className="text-muted small mb-3"),
        dbc.Row(cards),
        html.Div(id="msg-limpar-disp", className="mt-2"),
    ])


# ── Ordens (OS) ───────────────────────────────────────────────────────────────

def _render_ordens(token):
    _, historico = _api("get", "/os/historico?limite=50", token=token)
    if not isinstance(historico, list):
        historico = []

    STATUS_OPS = [
        {"label": "Aguardando",   "value": "aguardando"},
        {"label": "Em Andamento", "value": "em_andamento"},
        {"label": "Concluída",    "value": "concluida"},
        {"label": "Erro",         "value": "erro"},
        {"label": "Cancelada",    "value": "cancelada"},
    ]

    rows = []
    for os_ in historico:
        os_id  = os_.get("os_id","?")
        sts    = os_.get("status","?")
        cor    = _cor_status_os(sts)
        cat    = os_.get("categoria","—")
        criado = str(os_.get("criado_em",""))[:16]

        rows.append(dbc.Row([
            dbc.Col(
                dbc.Button(os_id, id={"type":"btn-os-detalhe","index":os_id},
                           color="link", size="sm", className="p-0 text-info"),
                md=3,
            ),
            dbc.Col(html.Small(cat, className="text-muted"), md=2),
            dbc.Col(dbc.Badge(sts, color=cor, className="small"), md=2),
            dbc.Col(html.Small(criado, className="text-muted"), md=2),
            dbc.Col(
                dbc.Row([
                    dbc.Col(dbc.Select(
                        id={"type":"sel-status-os","index":os_id},
                        options=STATUS_OPS,
                        value=sts,
                        size="sm",
                    ), width=8),
                    dbc.Col(dbc.Button("✓", id={"type":"btn-alterar-status","index":os_id},
                                       color="outline-light", size="sm"), width=4),
                ], className="g-1"),
                md=3,
            ),
        ], className="mb-1 align-items-center border-bottom pb-1"))

    header = dbc.Row([
        dbc.Col(html.Small("OS ID", className="text-muted fw-bold"), md=3),
        dbc.Col(html.Small("Categoria", className="text-muted fw-bold"), md=2),
        dbc.Col(html.Small("Status", className="text-muted fw-bold"), md=2),
        dbc.Col(html.Small("Criado em", className="text-muted fw-bold"), md=2),
        dbc.Col(html.Small("Alterar Status", className="text-muted fw-bold"), md=3),
    ], className="mb-2")

    return html.Div([
        html.H5("🗂 Histórico de Ordens (OS)", className="mb-1"),
        html.P("Clique no ID da OS para ver o JSON completo. Use o seletor para alterar o status.",
               className="text-muted small mb-3"),
        header,
        html.Div(rows) if rows else html.P("Sem ordens registradas.", className="text-muted small"),
        html.Div(id="msg-alterar-status", className="mt-2"),
    ])


# ── Usuários ───────────────────────────────────────────────────────────────────

def _render_usuarios(token, role):
    if role != "admin":
        return dbc.Alert(
            "⛔ Acesso restrito a administradores. Faça login com uma conta admin.",
            color="danger",
        )

    _, usuarios = _api("get", "/manutencao/usuarios", token=token)
    if not isinstance(usuarios, list):
        return html.P("Erro ao carregar usuários.", className="text-danger small")

    rows = []
    for u in usuarios:
        username = u.get("username","?")
        nome     = u.get("nome_completo","?")
        r        = u.get("role","?")
        ativo    = u.get("ativo", 1)

        rows.append(dbc.Row([
            dbc.Col(html.Small(username, className="fw-bold"), md=2),
            dbc.Col(html.Small(nome, className="text-light"), md=3),
            dbc.Col(dbc.Badge(r, color="primary" if r=="admin" else "secondary"), md=2),
            dbc.Col(dbc.Badge("Ativo" if ativo else "Inativo",
                              color="success" if ativo else "danger"), md=1),
            dbc.Col([
                dbc.Select(
                    id={"type":"sel-role","index":username},
                    options=[
                        {"label":"admin",      "value":"admin"},
                        {"label":"manutencao", "value":"manutencao"},
                    ],
                    value=r, size="sm",
                ),
            ], md=2),
            dbc.Col([
                dbc.Button("Salvar", id={"type":"btn-salvar-role","index":username},
                           color="outline-info", size="sm", className="me-1"),
                dbc.Button(
                    "Desativar" if ativo else "Ativar",
                    id={"type":"btn-toggle-ativo","index":username},
                    color="outline-danger" if ativo else "outline-success",
                    size="sm",
                ),
            ], md=2),
        ], className="mb-2 align-items-center border-bottom pb-1"))

    # Formulário de criação
    form_criar = dbc.Card([
        dbc.CardHeader(html.Strong("➕ Novo Usuário")),
        dbc.CardBody([
            dbc.Row([
                dbc.Col(dbc.Input(id="nou-username", placeholder="username"), md=3),
                dbc.Col(dbc.Input(id="nou-nome", placeholder="Nome completo"), md=3),
                dbc.Col(dbc.Input(id="nou-senha", placeholder="Senha", type="password"), md=2),
                dbc.Col(dbc.Select(id="nou-role", value="manutencao", options=[
                    {"label":"admin","value":"admin"},
                    {"label":"manutencao","value":"manutencao"},
                ]), md=2),
                dbc.Col(dbc.Button("Criar", id="btn-criar-usuario",
                                   color="success", size="sm"), md=2),
            ], className="g-2 align-items-center"),
            html.Div(id="msg-usuario", className="mt-2"),
        ]),
    ], className="mt-4")

    header = dbc.Row([
        dbc.Col(html.Small("Username", className="fw-bold text-muted"), md=2),
        dbc.Col(html.Small("Nome", className="fw-bold text-muted"), md=3),
        dbc.Col(html.Small("Role", className="fw-bold text-muted"), md=2),
        dbc.Col(html.Small("Status", className="fw-bold text-muted"), md=1),
        dbc.Col(html.Small("Novo Role", className="fw-bold text-muted"), md=2),
        dbc.Col(html.Small("Ações", className="fw-bold text-muted"), md=2),
    ], className="mb-2")

    return html.Div([
        html.H5("👥 Gerenciamento de Usuários", className="mb-3"),
        header,
        html.Div(rows),
        form_criar,
    ])


# ═══════════════════════════════════════════════════════════════════════════════
# Callbacks de ações
# ═══════════════════════════════════════════════════════════════════════════════

# ── Resolver alarme ────────────────────────────────────────────────────────────

@callback(
    Output("active-tab", "data", allow_duplicate=True),
    Input({"type":"btn-resolver","index":ALL}, "n_clicks"),
    State("jwt-token", "data"),
    prevent_initial_call=True,
)
def _resolver_alarme(n_clicks_list, token):
    if not any(n for n in (n_clicks_list or []) if n):
        return no_update
    if not token:
        return no_update
    triggered = ctx.triggered_id
    if triggered and isinstance(triggered, dict):
        _api("put", f"/manutencao/alarmes/{triggered['index']}/resolver", token=token)
    return "alarmes"


# ── Salvar manutenção ──────────────────────────────────────────────────────────

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
        return dbc.Alert("Sessão expirada.", color="danger"), "", ""
    if not componente or not descricao:
        return dbc.Alert("Preencha componente e descrição.", color="warning"), descricao, componente
    code, data = _api("post", "/manutencao/log", token=token,
                      json_body={"tipo":tipo,"componente":componente,"descricao":descricao})
    if code == 200:
        return dbc.Alert("Manutenção registrada!", color="success"), "", ""
    return dbc.Alert(f"Erro: {data}", color="danger"), descricao, componente


# ── Limpar dispenser ───────────────────────────────────────────────────────────

@callback(
    Output("msg-limpar-disp", "children"),
    Input({"type":"btn-limpar-disp","index":ALL}, "n_clicks"),
    State("jwt-token", "data"),
    prevent_initial_call=True,
)
def _limpar_dispenser(n_clicks_list, token):
    if not any(n for n in (n_clicks_list or []) if n):
        return no_update
    if not token:
        return dbc.Alert("Sessão expirada.", color="danger")
    triggered = ctx.triggered_id
    if not (triggered and isinstance(triggered, dict)):
        return no_update
    d_id = triggered["index"]
    code, data = _api("post", f"/manutencao/dispensers/{d_id}/limpar", token=token)
    if code == 200:
        return dbc.Alert(
            f"✅ Dispenser {d_id}: comando de limpeza enviado. Aguardando confirmação do equipamento.",
            color="success", dismissable=True,
        )
    return dbc.Alert(f"Erro: {data.get('detail','?')}", color="danger", dismissable=True)


# ── Alterar status da OS ───────────────────────────────────────────────────────

@callback(
    Output("msg-alterar-status", "children"),
    Input({"type":"btn-alterar-status","index":ALL}, "n_clicks"),
    State({"type":"sel-status-os","index":ALL}, "value"),
    State({"type":"sel-status-os","index":ALL}, "id"),
    State("jwt-token", "data"),
    prevent_initial_call=True,
)
def _alterar_status_os(n_clicks_list, status_values, ids, token):
    if not any(n for n in (n_clicks_list or []) if n):
        return no_update
    if not token:
        return dbc.Alert("Sessão expirada.", color="danger")
    triggered = ctx.triggered_id
    if not (triggered and isinstance(triggered, dict)):
        return no_update
    os_id = triggered["index"]

    # Encontra o valor selecionado para esta OS
    novo_status = None
    for i, id_ in enumerate(ids or []):
        if id_["index"] == os_id:
            novo_status = status_values[i]
            break

    if not novo_status:
        return dbc.Alert("Não foi possível determinar o status.", color="warning")

    code, data = _api("put", f"/ordens/{os_id}/status", token=token,
                      json_body={"status": novo_status})
    if code == 200:
        return dbc.Alert(
            f"✅ OS {os_id} → {novo_status}", color="success", dismissable=True
        )
    return dbc.Alert(f"Erro: {data.get('detail','?')}", color="danger", dismissable=True)


# ── Modal de detalhe da OS ─────────────────────────────────────────────────────

@callback(
    Output("os-modal",       "is_open"),
    Output("os-modal-title", "children"),
    Output("os-modal-body",  "children"),
    Input({"type":"btn-os-detalhe","index":ALL}, "n_clicks"),
    Input("os-modal-close",  "n_clicks"),
    State("jwt-token", "data"),
    prevent_initial_call=True,
)
def _toggle_os_modal(n_clicks_list, close_click, token):
    triggered = ctx.triggered_id

    if triggered == "os-modal-close":
        return False, "", ""

    if not any(n for n in (n_clicks_list or []) if n):
        return no_update, no_update, no_update

    if not (triggered and isinstance(triggered, dict)):
        return no_update, no_update, no_update

    os_id = triggered["index"]
    _, data = _api("get", f"/os/{os_id}", token=token)

    if not isinstance(data, dict):
        return True, os_id, dbc.Alert("OS não encontrada.", color="danger")

    # Exibe payload JSON formatado + itens
    try:
        payload = json.loads(data.get("payload_json", "{}"))
    except Exception:
        payload = data.get("payload_json", {})

    itens = data.get("itens", [])
    itens_tabela = dbc.Table([
        html.Thead(html.Tr([
            html.Th("Dispenser"), html.Th("Medicamento"), html.Th("Qtd Alvo"),
            html.Th("Qtd Real"), html.Th("Status"),
        ])),
        html.Tbody([html.Tr([
            html.Td(f"D{i.get('dispenser_id')}"),
            html.Td(i.get("medicamento","")),
            html.Td(i.get("quantidade_alvo",0)),
            html.Td(i.get("quantidade_real",0)),
            html.Td(dbc.Badge(i.get("status","?"),
                              color=_cor_status_os(i.get("status","")))),
        ]) for i in itens]),
    ], bordered=True, striped=True, hover=True, size="sm", dark=True)

    corpo = html.Div([
        dbc.Badge(data.get("status","?"), color=_cor_status_os(data.get("status","")),
                  className="mb-2"),
        html.P([html.Strong("Categoria: "), data.get("categoria","—")], className="small"),
        html.P([html.Strong("Criado em: "), str(data.get("criado_em",""))[:19]], className="small"),
        html.Hr(),
        html.H6("Itens da OS:"),
        itens_tabela,
        html.Hr(),
        html.H6("Payload JSON (SAP):"),
        html.Pre(
            json.dumps(payload, indent=2, ensure_ascii=False),
            style={"background":"#1a1a2e","padding":"12px","borderRadius":"6px",
                   "fontSize":"12px","maxHeight":"300px","overflowY":"auto"},
        ),
    ])

    return True, f"OS: {os_id}", corpo


# ── Gerenciar usuários ─────────────────────────────────────────────────────────

@callback(
    Output("msg-usuario", "children"),
    Input("btn-criar-usuario", "n_clicks"),
    State("nou-username", "value"),
    State("nou-nome",     "value"),
    State("nou-senha",    "value"),
    State("nou-role",     "value"),
    State("jwt-token",    "data"),
    prevent_initial_call=True,
)
def _criar_usuario(_, username, nome, senha, role, token):
    if not token:
        return dbc.Alert("Sessão expirada.", color="danger")
    if not all([username, nome, senha]):
        return dbc.Alert("Preencha todos os campos.", color="warning")
    code, data = _api("post", "/manutencao/usuarios", token=token,
                      json_body={"username":username,"senha":senha,
                                 "nome_completo":nome,"role":role or "manutencao"})
    if code == 200:
        return dbc.Alert(f"Usuário '{username}' criado com sucesso!", color="success")
    return dbc.Alert(f"Erro: {data.get('detail','?')}", color="danger")


@callback(
    Output("active-tab", "data", allow_duplicate=True),
    Input({"type":"btn-salvar-role","index":ALL}, "n_clicks"),
    State({"type":"sel-role","index":ALL}, "value"),
    State({"type":"sel-role","index":ALL}, "id"),
    State("jwt-token", "data"),
    prevent_initial_call=True,
)
def _salvar_role(n_clicks_list, role_values, ids, token):
    if not any(n for n in (n_clicks_list or []) if n):
        return no_update
    triggered = ctx.triggered_id
    if not (triggered and isinstance(triggered, dict)):
        return no_update
    username = triggered["index"]
    novo_role = None
    for i, id_ in enumerate(ids or []):
        if id_["index"] == username:
            novo_role = role_values[i]
            break
    if novo_role and token:
        _api("put", f"/manutencao/usuarios/{username}", token=token,
             json_body={"role": novo_role})
    return "usuarios"


@callback(
    Output("active-tab", "data", allow_duplicate=True),
    Input({"type":"btn-toggle-ativo","index":ALL}, "n_clicks"),
    State({"type":"btn-toggle-ativo","index":ALL}, "children"),
    State("jwt-token", "data"),
    prevent_initial_call=True,
)
def _toggle_ativo(n_clicks_list, labels, token):
    if not any(n for n in (n_clicks_list or []) if n):
        return no_update
    triggered = ctx.triggered_id
    if not (triggered and isinstance(triggered, dict)):
        return no_update
    username = triggered["index"]
    idx = next((i for i, id_ in enumerate(
        [{"index": u} for u in [username]]
    ) if id_["index"] == username), None)
    # Determina label clicado
    triggered_idx = next(
        (i for i, nc in enumerate(n_clicks_list or []) if nc), None
    )
    if triggered_idx is not None and token:
        label = (labels or [])[triggered_idx] if labels else "Desativar"
        if label == "Desativar":
            _api("put", f"/manutencao/usuarios/{username}/desativar", token=token)
        else:
            _api("put", f"/manutencao/usuarios/{username}/ativar", token=token)
    return "usuarios"


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8051, debug=False)
