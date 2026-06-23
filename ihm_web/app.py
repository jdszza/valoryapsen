"""
APSEN - IHM Web App (porta 8051)
Interface funcional e interativa para operadores e manutenção.
Roles: admin > manutencao > operador
"""
import os
import requests
import dash
from dash import Dash, Input, Output, State, callback, dcc, html, ctx
import dash_bootstrap_components as dbc

BACKEND = os.getenv("BACKEND_URL", "http://backend:8000")

app = Dash(
    __name__,
    external_stylesheets=[dbc.themes.CYBORG],
    suppress_callback_exceptions=True,
    title="APSEN – IHM",
    update_title=None,
)
server = app.server

def api(method, path, token=None, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = getattr(requests, method)(
            f"{BACKEND}{path}", headers=headers, timeout=5, **kwargs
        )
        return r.json() if r.ok else None
    except Exception:
        return None

VERDE   = "#00ff88"
AMAR    = "#ffc107"
VERM    = "#ff4444"
AZUL    = "#0d6efd"
CINZA   = "#6c757d"

STATUS_COR = {
    "aberto": AZUL, "em_andamento": VERDE, "concluido": CINZA,
    "cancelado": VERM, "idle": CINZA, "running": VERDE,
    "paused": AMAR, "alarm": VERM,
}

def badge(texto, cor):
    return html.Span(
        texto.upper(),
        style={"background": cor,
               "color": "#000" if cor == AMAR else "#fff",
               "borderRadius": "4px", "padding": "2px 10px",
               "fontSize": "11px", "fontWeight": "700", "letterSpacing": "1px"}
    )

def card(titulo, conteudo, cor=AZUL):
    return dbc.Card([
        dbc.CardHeader(html.Strong(titulo, style={"color": cor})),
        dbc.CardBody(conteudo),
    ], style={"border": f"1px solid {cor}33", "marginBottom": "16px"})

# ── Páginas ────────────────────────────────────────────────────────────────────
def pagina_login(erro=""):
    return dbc.Container([
        html.Div(style={"height": "80px"}),
        dbc.Row(dbc.Col(dbc.Card([
            dbc.CardBody([
                html.Div([
                    html.H2("APSEN", style={"color": VERDE, "fontWeight": "800",
                                             "letterSpacing": "3px"}),
                    html.P("Sistema de Contagem — IHM",
                           style={"color": "#aaa", "marginBottom": "32px"}),
                ], className="text-center"),
                dbc.Input(id="login-user",  placeholder="Usuário",
                          type="text",     className="mb-3", size="lg"),
                dbc.Input(id="login-senha", placeholder="Senha",
                          type="password", className="mb-3", size="lg"),
                dbc.Button("ENTRAR", id="btn-login", color="success",
                           size="lg", className="w-100"),
                html.Div(erro, id="login-erro",
                         style={"color": VERM, "marginTop": "12px",
                                "textAlign": "center", "fontSize": "14px"}),
            ])
        ], style={"border": f"1px solid {VERDE}44", "maxWidth": "400px",
                  "margin": "auto"})),
        justify="center"),
    ])

def sidebar(role, nome):
    itens = [
        dbc.NavLink("Ordens de Serviço", href="/ordens",   active="exact",
                    style={"color": "#ccc"}),
        dbc.NavLink("Operação",          href="/operacao", active="exact",
                    style={"color": "#ccc"}),
    ]
    if role in ("manutencao", "admin"):
        itens.append(dbc.NavLink("Manutenção", href="/manutencao",
                                 active="exact", style={"color": AMAR}))
    if role == "admin":
        itens.append(dbc.NavLink("Usuários", href="/usuarios",
                                 active="exact", style={"color": VERM}))
    return html.Div([
        html.Div([
            html.H4("APSEN", style={"color": VERDE, "fontWeight": "800",
                                     "letterSpacing": "2px", "margin": "0"}),
            html.Small("IHM", style={"color": "#888"}),
        ], style={"padding": "20px 16px 8px"}),
        html.Hr(style={"borderColor": "#333"}),
        dbc.Nav(itens, vertical=True, pills=True, style={"padding": "0 8px"}),
        html.Div(style={"flexGrow": "1"}),
        html.Hr(style={"borderColor": "#333"}),
        html.Div([
            html.Small(nome, style={"color": "#aaa", "display": "block"}),
            html.Small(role.upper(), style={"color": VERDE, "fontWeight": "700"}),
            dbc.Button("Sair", id="btn-logout", color="danger",
                       outline=True, size="sm", className="mt-2 w-100"),
        ], style={"padding": "12px 16px"}),
    ], style={
        "width": "200px", "minHeight": "100vh", "background": "#111",
        "display": "flex", "flexDirection": "column",
        "position": "fixed", "left": 0, "top": 0, "bottom": 0,
        "borderRight": "1px solid #222",
    })

def pagina_ordens(token, role):
    ordens = api("get", "/ordens", token=token) or []
    can_create = role in ("operador", "manutencao", "admin")
    linhas = []
    for o in ordens:
        cor = STATUS_COR.get(o.get("status", ""), CINZA)
        linhas.append(html.Tr([
            html.Td(html.Strong(o["os_id"], style={"color": VERDE})),
            html.Td(o["produto"]),
            html.Td(o["lote_id"]),
            html.Td(f"{o['meta']:,}"),
            html.Td(badge(o["status"], cor)),
            html.Td(o.get("responsavel", "—")),
        ], style={"borderBottom": "1px solid #222"}))

    return html.Div([
        html.Div([
            html.H4("Ordens de Serviço", style={"color": VERDE, "margin": 0}),
            dbc.Button("+ Nova OS", id="btn-nova-os", color="success",
                       size="sm", disabled=not can_create),
        ], style={"display": "flex", "justifyContent": "space-between",
                  "alignItems": "center", "marginBottom": "20px"}),

        dbc.Modal([
            dbc.ModalHeader("Nova Ordem de Serviço"),
            dbc.ModalBody([
                dbc.Input(id="nos-produto", placeholder="Produto",    className="mb-2"),
                dbc.Input(id="nos-lote",    placeholder="ID do Lote", className="mb-2"),
                dbc.Input(id="nos-meta",    placeholder="Meta (unidades)",
                          type="number",   className="mb-2"),
                dbc.Input(id="nos-resp",    placeholder="Responsável", className="mb-2"),
            ]),
            dbc.ModalFooter([
                dbc.Button("Cancelar", id="btn-nos-cancel",  color="secondary"),
                dbc.Button("Criar OS", id="btn-nos-confirm", color="success"),
            ]),
        ], id="modal-nova-os", is_open=False),
        html.Div(id="os-feedback",
                 style={"color": VERDE, "marginBottom": "8px", "fontSize": "13px"}),

        # id="ordens-lista" é atualizado pelo callback iv-ordens
        html.Div(id="ordens-lista", children=card("Lista de Ordens", dbc.Table([
            html.Thead(html.Tr([
                html.Th("OS"), html.Th("Produto"), html.Th("Lote"),
                html.Th("Meta"), html.Th("Status"), html.Th("Responsável"),
            ], style={"borderBottom": "1px solid #333"})),
            html.Tbody(linhas or [html.Tr(html.Td(
                "Nenhuma OS cadastrada.", colSpan=6,
                style={"color": "#666", "textAlign": "center", "padding": "24px"}
            ))]),
        ], dark=True, hover=True, responsive=True, style={"fontSize": "14px"}))),

        dcc.Interval(id="iv-ordens", interval=10000),
    ])

def _render_op_kpis(est: dict):
    """Renderiza os KPIs dinâmicos da página de operação."""
    status   = est.get("status", "idle")
    cor_s    = STATUS_COR.get(status, CINZA)
    contagem = est.get("contagem", 0)
    meta     = est.get("meta", 1)
    prog     = min(100, round(contagem / max(meta, 1) * 100, 1))
    return [
        dbc.Row([
            dbc.Col(card("Contagem", html.Div([
                html.Div(f"{contagem:,}", style={"fontSize": "56px", "fontWeight": "800",
                                                  "color": VERDE, "lineHeight": "1"}),
                html.Small("unidades", style={"color": "#888"}),
            ])), md=3),
            dbc.Col(card("Meta", html.Div([
                html.Div(f"{meta:,}", style={"fontSize": "56px", "fontWeight": "800",
                                              "color": "#fff", "lineHeight": "1"}),
                html.Small("unidades", style={"color": "#888"}),
            ])), md=3),
            dbc.Col(card("Progresso", html.Div([
                html.Div(f"{prog}%", style={"fontSize": "56px", "fontWeight": "800",
                                             "color": AMAR, "lineHeight": "1"}),
                dbc.Progress(value=prog, color="warning",
                             style={"marginTop": "8px", "height": "8px"}),
            ])), md=3),
            dbc.Col(card("Status", html.Div([
                badge(status, cor_s),
                html.Div(f"{est.get('velocidade', 0)} un/min",
                         style={"fontSize": "28px", "fontWeight": "700",
                                "color": "#fff", "marginTop": "12px"}),
                html.Small("velocidade", style={"color": "#888"}),
            ])), md=3),
        ], className="mb-3"),
        html.Div(
            [html.Strong("⚠ ALARME: "), est.get("alarme", "")],
            style={"background": "#4a0000", "border": f"1px solid {VERM}",
                   "borderRadius": "8px", "padding": "12px 20px",
                   "color": VERM, "marginBottom": "16px",
                   "display": "block" if est.get("alarme") else "none"},
        ),
    ]

def pagina_operacao(token, role):
    est = api("get", "/estado", token=token) or {}
    can = role in ("operador", "manutencao", "admin")

    return html.Div([
        html.H4("Operação em Tempo Real", style={"color": VERDE, "marginBottom": "20px"}),
        # id="op-live-section" é atualizado pelo callback iv-operacao
        html.Div(_render_op_kpis(est), id="op-live-section"),

        card("Controles", html.Div([
            dbc.Row([
                dbc.Col(dbc.Button("▶ INICIAR", id="btn-op-start", color="success",
                                   size="lg", className="w-100", disabled=not can), md=3),
                dbc.Col(dbc.Button("⏸ PAUSAR",  id="btn-op-pause", color="warning",
                                   size="lg", className="w-100", disabled=not can), md=3),
                dbc.Col(dbc.Button("⏹ PARAR",   id="btn-op-stop",  color="danger",
                                   size="lg", className="w-100", disabled=not can), md=3),
                dbc.Col(dbc.Button("↺ RESETAR", id="btn-op-reset", color="secondary",
                                   size="lg", className="w-100", disabled=not can), md=3),
            ]),
            html.Div(id="op-feedback", style={"marginTop": "12px", "fontSize": "13px"}),
        ]), cor=VERDE),

        dcc.Interval(id="iv-operacao", interval=2000),
    ])

def pagina_manutencao(token):
    alarmes = api("get", "/manutencao/alarmes?limite=20", token=token) or []
    log     = api("get", "/manutencao/log?limite=20",     token=token) or []
    diag    = api("get", "/manutencao/diagnostico",       token=token) or {}
    em      = diag.get("estado_maquina", {})

    diag_items = [
        ("Status",      em.get("status", "—")),
        ("Contagem",    em.get("contagem", "—")),
        ("Velocidade",  f"{em.get('velocidade', 0)} un/min"),
        ("Lote",        em.get("lote_id", "—")),
        ("MQTT",        "✔ Conectado" if diag.get("mqtt_conectado") else "✘ Desconectado"),
    ]

    linhas_al = [html.Tr([
        html.Td(a.get("tipo", "")),
        html.Td(a.get("descricao", "")),
        html.Td(html.Small(a.get("ts", ""), style={"color": "#888"})),
    ]) for a in alarmes] or [html.Tr(html.Td(
        "Nenhum alarme.", colSpan=3,
        style={"color": "#666", "textAlign": "center", "padding": "16px"}
    ))]

    linhas_log = [html.Tr([
        html.Td(badge(l.get("tipo", ""), AZUL)),
        html.Td(l.get("descricao", "")),
        html.Td(l.get("componente", "—")),
        html.Td(l.get("responsavel", "")),
        html.Td(html.Small(l.get("ts", ""), style={"color": "#888"})),
    ]) for l in log] or [html.Tr(html.Td(
        "Nenhum registro.", colSpan=5,
        style={"color": "#666", "textAlign": "center", "padding": "16px"}
    ))]

    return html.Div([
        html.H4("Painel de Manutenção", style={"color": AMAR, "marginBottom": "20px"}),
        dbc.Row([
            dbc.Col(card("Diagnóstico", html.Table([html.Tbody([
                html.Tr([
                    html.Td(k, style={"color": "#888", "paddingRight": "20px",
                                      "paddingBottom": "8px"}),
                    html.Td(html.Strong(v, style={"color": "#fff"})),
                ]) for k, v in diag_items
            ])]), cor=AMAR), md=5),
            dbc.Col(card("Controle Manual", html.Div([
                dbc.Select(id="sel-cmd-manut", options=[
                    {"label": "Teste Sensor",       "value": "teste_sensor"},
                    {"label": "Zerar Encoder",      "value": "reset_encoder"},
                    {"label": "Piscar LED Alarme",  "value": "piscar_alarme"},
                    {"label": "Reiniciar Contagem", "value": "reset_contagem"},
                ], className="mb-2"),
                dbc.Input(id="inp-componente", placeholder="Componente (opcional)",
                          className="mb-2"),
                dbc.Button("Enviar Comando", id="btn-cmd-manut",
                           color="warning", className="w-100"),
                html.Div(id="manut-cmd-fb",
                         style={"marginTop": "8px", "fontSize": "13px"}),
            ]), cor=AMAR), md=7),
        ], className="mb-3"),

        card("Registrar Intervenção", html.Div([
            dbc.Row([
                dbc.Col(dbc.Select(id="sel-tipo-manut", options=[
                    {"label": "Preventiva", "value": "preventiva"},
                    {"label": "Corretiva",  "value": "corretiva"},
                    {"label": "Inspeção",   "value": "inspecao"},
                    {"label": "Calibração", "value": "calibracao"},
                    {"label": "Limpeza",    "value": "limpeza"},
                ], placeholder="Tipo"), md=4),
                dbc.Col(dbc.Input(id="inp-comp-manut", placeholder="Componente"), md=4),
                dbc.Col(dbc.Button("Registrar", id="btn-reg-manut", color="warning"), md=4),
            ], className="mb-2"),
            dbc.Textarea(id="inp-desc-manut", placeholder="Descrição da intervenção...",
                         rows=3, style={"background": "#1a1a1a", "color": "#fff",
                                        "border": "1px solid #333"}),
            html.Div(id="manut-reg-fb",
                     style={"marginTop": "8px", "fontSize": "13px", "color": VERDE}),
        ]), cor=AMAR),

        card("Histórico de Alarmes", dbc.Table([
            html.Thead(html.Tr([html.Th("Tipo"), html.Th("Descrição"), html.Th("Data/Hora")])),
            html.Tbody(linhas_al),
        ], dark=True, hover=True, responsive=True, size="sm")),

        card("Log de Manutenções", dbc.Table([
            html.Thead(html.Tr([html.Th("Tipo"), html.Th("Descrição"),
                                html.Th("Componente"), html.Th("Responsável"),
                                html.Th("Data/Hora")])),
            html.Tbody(linhas_log),
        ], dark=True, hover=True, responsive=True, size="sm")),

        dcc.Interval(id="iv-manut", interval=15000),
    ])

def pagina_usuarios(token):
    usuarios = api("get", "/usuarios", token=token) or []
    cor_role = {"admin": VERM, "manutencao": AMAR, "operador": VERDE}
    linhas = [html.Tr([
        html.Td(u["username"]),
        html.Td(u["nome_completo"]),
        html.Td(badge(u["role"], cor_role.get(u["role"], CINZA))),
        html.Td(badge("ativo" if u["ativo"] else "inativo",
                      VERDE if u["ativo"] else CINZA)),
    ]) for u in usuarios]

    return html.Div([
        html.Div([
            html.H4("Gerenciar Usuários", style={"color": VERM, "margin": 0}),
            dbc.Button("+ Novo Usuário", id="btn-novo-user", color="danger", size="sm"),
        ], style={"display": "flex", "justifyContent": "space-between",
                  "alignItems": "center", "marginBottom": "20px"}),

        dbc.Modal([
            dbc.ModalHeader("Novo Usuário"),
            dbc.ModalBody([
                dbc.Input(id="nu-username", placeholder="Username",      className="mb-2"),
                dbc.Input(id="nu-nome",     placeholder="Nome Completo", className="mb-2"),
                dbc.Input(id="nu-senha",    placeholder="Senha",
                          type="password",  className="mb-2"),
                dbc.Select(id="nu-role", options=[
                    {"label": "Operador",   "value": "operador"},
                    {"label": "Manutenção", "value": "manutencao"},
                    {"label": "Admin",      "value": "admin"},
                ], placeholder="Papel", className="mb-2"),
            ]),
            dbc.ModalFooter([
                dbc.Button("Cancelar", id="btn-nu-cancel",  color="secondary"),
                dbc.Button("Criar",    id="btn-nu-confirm", color="danger"),
            ]),
        ], id="modal-novo-user", is_open=False),

        card("Usuários", dbc.Table([
            html.Thead(html.Tr([html.Th("Username"), html.Th("Nome"),
                                html.Th("Papel"), html.Th("Status")])),
            html.Tbody(linhas),
        ], dark=True, hover=True, responsive=True)),
        html.Div(id="user-fb", style={"color": VERDE, "marginTop": "8px"}),
    ])

def layout_principal(pathname, auth, role, nome):
    token = (auth or {}).get("token")
    if pathname in ("/", "/ordens"):
        return pagina_ordens(token, role)
    elif pathname == "/operacao":
        return pagina_operacao(token, role)
    elif pathname == "/manutencao":
        if role not in ("manutencao", "admin"):
            return html.Div("Acesso negado.", style={"color": VERM, "padding": "40px"})
        return pagina_manutencao(token)
    elif pathname == "/usuarios":
        if role != "admin":
            return html.Div("Acesso negado.", style={"color": VERM, "padding": "40px"})
        return pagina_usuarios(token)
    return html.Div("Página não encontrada.", style={"padding": "40px", "color": "#888"})

# ── Layout ─────────────────────────────────────────────────────────────────────
app.layout = html.Div([
    dcc.Location(id="url"),
    dcc.Store(id="auth-store", storage_type="session"),
    html.Div(id="app-root"),
])

@callback(Output("app-root", "children"),
          Input("url", "pathname"), Input("auth-store", "data"))
def router(pathname, auth):
    if not auth or not auth.get("token"):
        return pagina_login()
    return html.Div([
        sidebar(auth.get("role", ""), auth.get("nome", "")),
        html.Div(layout_principal(pathname, auth, auth.get("role", ""),
                                  auth.get("nome", "")),
                 style={"marginLeft": "210px", "padding": "28px 32px",
                        "minHeight": "100vh"}),
    ])

@callback(
    Output("auth-store", "data"),
    Output("login-erro", "children"),
    Input("btn-login", "n_clicks"),
    State("login-user", "value"), State("login-senha", "value"),
    prevent_initial_call=True,
)
def fazer_login(n, username, senha):
    if not username or not senha:
        return dash.no_update, "Preencha usuário e senha."
    resp = api("post", "/auth/login", json={"username": username, "senha": senha})
    if resp and "token" in resp:
        return resp, ""
    return dash.no_update, "Usuário ou senha incorretos."

@callback(
    Output("auth-store", "data", allow_duplicate=True),
    Output("url", "pathname"),
    Input("btn-logout", "n_clicks"),
    prevent_initial_call=True,
)
def logout(n):
    return None, "/"

@callback(
    Output("op-feedback", "children"),
    Input("btn-op-start", "n_clicks"), Input("btn-op-pause", "n_clicks"),
    Input("btn-op-stop",  "n_clicks"), Input("btn-op-reset", "n_clicks"),
    State("auth-store", "data"),
    prevent_initial_call=True,
)
def controles(ns, np, nst, nr, auth):
    token = (auth or {}).get("token")
    t = ctx.triggered_id
    if not t or not token:
        return dash.no_update
    mapa = {
        "btn-op-start": ("start", "▶ Máquina iniciada.",  VERDE),
        "btn-op-pause": ("pause", "⏸ Máquina pausada.",   AMAR),
        "btn-op-stop":  ("stop",  "⏹ Máquina parada.",    VERM),
    }
    if t == "btn-op-reset":
        api("post", "/cmd/reset", token=token)
        return html.Span("↺ Contagem resetada.", style={"color": CINZA})
    if t in mapa:
        cmd, msg, cor = mapa[t]
        api("post", f"/cmd/status?cmd={cmd}", token=token)
        return html.Span(msg, style={"color": cor})
    return dash.no_update

@callback(
    Output("manut-cmd-fb", "children"),
    Input("btn-cmd-manut", "n_clicks"),
    State("sel-cmd-manut", "value"), State("inp-componente", "value"),
    State("auth-store", "data"),
    prevent_initial_call=True,
)
def cmd_manut(n, cmd, comp, auth):
    token = (auth or {}).get("token")
    if not cmd or not token:
        return "Selecione um comando."
    resp = api("post", f"/manutencao/cmd?cmd={cmd}&componente={comp or ''}", token=token)
    if resp:
        return html.Span(f"✔ '{cmd}' enviado.", style={"color": VERDE})
    return html.Span("Falha ao enviar.", style={"color": VERM})

@callback(
    Output("manut-reg-fb", "children"),
    Input("btn-reg-manut", "n_clicks"),
    State("sel-tipo-manut", "value"), State("inp-desc-manut", "value"),
    State("inp-comp-manut", "value"), State("auth-store", "data"),
    prevent_initial_call=True,
)
def reg_manut(n, tipo, desc, comp, auth):
    token = (auth or {}).get("token")
    if not tipo or not desc:
        return "Preencha tipo e descrição."
    resp = api("post", "/manutencao/log", token=token,
               json={"tipo": tipo, "descricao": desc, "componente": comp or ""})
    return f"✔ Registrado ({tipo})." if resp else "Falha ao registrar."

@callback(
    Output("op-live-section", "children"),
    Input("iv-operacao", "n_intervals"),
    State("auth-store", "data"),
    prevent_initial_call=True,
)
def refresh_operacao(_, auth):
    """Atualiza KPIs de operação a cada 2 s via iv-operacao."""
    token = (auth or {}).get("token")
    if not token:
        return dash.no_update
    est = api("get", "/estado", token=token) or {}
    return _render_op_kpis(est)

@callback(
    Output("ordens-lista", "children"),
    Input("iv-ordens", "n_intervals"),
    State("auth-store", "data"),
    prevent_initial_call=True,
)
def refresh_ordens(_, auth):
    """Atualiza lista de OS a cada 10 s via iv-ordens."""
    token = (auth or {}).get("token")
    if not token:
        return dash.no_update
    ordens = api("get", "/ordens", token=token) or []
    linhas = []
    for o in ordens:
        cor = STATUS_COR.get(o.get("status", ""), CINZA)
        linhas.append(html.Tr([
            html.Td(html.Strong(o["os_id"], style={"color": VERDE})),
            html.Td(o["produto"]),
            html.Td(o["lote_id"]),
            html.Td(f"{o['meta']:,}"),
            html.Td(badge(o["status"], cor)),
            html.Td(o.get("responsavel", "—")),
        ], style={"borderBottom": "1px solid #222"}))
    return card("Lista de Ordens", dbc.Table([
        html.Thead(html.Tr([
            html.Th("OS"), html.Th("Produto"), html.Th("Lote"),
            html.Th("Meta"), html.Th("Status"), html.Th("Responsável"),
        ], style={"borderBottom": "1px solid #333"})),
        html.Tbody(linhas or [html.Tr(html.Td(
            "Nenhuma OS cadastrada.", colSpan=6,
            style={"color": "#666", "textAlign": "center", "padding": "24px"}
        ))]),
    ], dark=True, hover=True, responsive=True, style={"fontSize": "14px"}))

@callback(
    Output("modal-nova-os", "is_open"),
    Output("os-feedback", "children"),
    Input("btn-nova-os", "n_clicks"),   Input("btn-nos-cancel", "n_clicks"),
    Input("btn-nos-confirm", "n_clicks"),
    State("modal-nova-os", "is_open"),
    State("nos-produto", "value"), State("nos-lote", "value"),
    State("nos-meta", "value"),    State("nos-resp", "value"),
    State("auth-store", "data"),
    prevent_initial_call=True,
)
def modal_os(n_open, n_cancel, n_confirm, is_open, produto, lote, meta, resp, auth):
    t = ctx.triggered_id
    if t == "btn-nova-os":    return True, ""
    if t == "btn-nos-cancel": return False, ""
    if t == "btn-nos-confirm":
        token = (auth or {}).get("token")
        if produto and lote and meta and resp and token:
            r = api("post", "/ordens", token=token,
                    json={"produto": produto, "lote_id": lote,
                          "meta": int(meta), "responsavel": resp})
            if r:
                return False, f"✔ OS {r.get('os_id','')} criada."
        return False, "Preencha todos os campos."
    return is_open, ""

@callback(
    Output("modal-novo-user", "is_open"),
    Output("user-fb", "children"),
    Input("btn-novo-user", "n_clicks"),  Input("btn-nu-cancel", "n_clicks"),
    Input("btn-nu-confirm", "n_clicks"),
    State("modal-novo-user", "is_open"),
    State("nu-username", "value"), State("nu-nome", "value"),
    State("nu-senha", "value"),    State("nu-role", "value"),
    State("auth-store", "data"),
    prevent_initial_call=True,
)
def modal_user(n_open, n_cancel, n_confirm, is_open, username, nome, senha, role, auth):
    t = ctx.triggered_id
    if t == "btn-novo-user":   return True, ""
    if t == "btn-nu-cancel":   return False, ""
    if t == "btn-nu-confirm":
        token = (auth or {}).get("token")
        if all([username, nome, senha, role, token]):
            api("post", "/usuarios", token=token,
                json={"username": username, "nome_completo": nome,
                      "senha": senha, "role": role})
            return False, f"\u2714 Usuário '{username}' criado."
        return False, "Preencha todos os campos."
    return is_open, ""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8051, debug=False)
