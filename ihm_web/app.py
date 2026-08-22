"""
APSEN - IHM de Manutenção v2.1 (porta 8051)
=============================================
Requer autenticação JWT. Usuários padrão: admin / manut1
(Senhas iniciais em SEED_ADMIN_SENHA e SEED_MANUT_SENHA no .env — troque ambas
após o primeiro login, pela aba 👥 Usuários. Ver README.)

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
import re

import dash
import requests
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update
import dash_bootstrap_components as dbc

BACKEND_URL = os.getenv("BACKEND_URL", "http://central-computer:8000")
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


def _vazio(icone: str, texto: str) -> html.Div:
    """Estado vazio: ocupa o painel em vez de deixar uma linha de texto solta."""
    return html.Div(
        [html.Span(icone, className="ico"), html.Span(texto)],
        className="ap-empty",
    )


def _hora(ts, com_data: bool = True) -> str:
    """`2026-08-18T14:03:07` → `08-18 14:03` (ou só `14:03:07`)."""
    s = str(ts or "").replace("T", " ")
    if len(s) < 19:
        return s[:16]
    return f"{s[5:10]} {s[11:16]}" if com_data else s[11:19]


def _titulo(icone: str, texto: str) -> html.H5:
    """Cabeçalho de aba — o filete dourado sob ele vem do `h5::after` do tema."""
    return html.H5(f"{icone} {texto}", className="mb-3")


def _cor_disp(qty: int, cap: int = 100) -> str:
    pct = qty / cap * 100 if cap else 0
    if pct >= 60: return "success"
    if pct >= 20: return "warning"
    return "danger" if qty > 0 else "secondary"


# ── Sidebar links ──────────────────────────────────────────────────────────────

# (ícone, rótulo, id da aba). O ícone é separado do rótulo para ganhar coluna
# própria no CSS: com tudo numa string só, ícones de larguras diferentes
# desalinham o texto de cada item da sidebar.
_SIDEBAR_LINKS = [
    ("🌡", "Temperaturas",     "temp"),
    ("⚙",  "Desgaste / Uso",   "uso"),
    ("📋", "Log Manutenção",   "log"),
    ("🚨", "Alarmes",          "alarmes"),
    ("➕", "Nova Manutenção",  "nova"),
    ("📦", "Dispensers",       "dispensers"),
    ("📷", "Visão / Balança",  "visao"),
    ("⛔", "Triple Check",     "trava"),
    ("🗂", "Ordens (OS)",      "ordens"),
    ("👥", "Usuários",         "usuarios"),
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
        # Alvo do download de relatório: o arquivo chega por aqui, em memória,
        # sem o navegador precisar falar com o backend (ver _buscar_relatorio).
        dcc.Download(id="download-relatorio"),

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
            className="ap-login-wrap",
            children=[
                dbc.Card([
                    dbc.CardHeader([
                        html.Div("AP", className="ap-login-logo"),
                        html.H4("APSEN", className="ap-login-title"),
                        html.Span("Painel de Manutenção", className="ap-login-sub"),
                    ]),
                    dbc.CardBody([
                        dbc.Label("Usuário", html_for="inp-user"),
                        dbc.Input(
                            id="inp-user", placeholder="seu.usuario",
                            type="text", className="mb-3",
                            autoFocus=True,
                        ),
                        dbc.Label("Senha", html_for="inp-senha"),
                        dbc.Input(
                            id="inp-senha", placeholder="••••••••",
                            type="password", className="mb-3",
                            debounce=True,
                        ),
                        dbc.Button(
                            "Entrar", id="btn-login",
                            color="primary", className="w-100",
                        ),
                        html.Div(id="msg-login", className="mt-3 text-danger small text-center"),
                    ]),
                ], className="ap-login"),
            ],
        ),

        # ── Tela Principal ────────────────────────────────────────────────────
        html.Div(
            id="tela-principal",
            style={"display": "none"},
            children=[
                dbc.Navbar(
                    dbc.Container([
                        dbc.NavbarBrand(
                            [html.Span("AP", className="ap-brand-mark"), "APSEN Manutenção"],
                            className="fw-bold",
                        ),
                        html.Span(id="span-username", className="me-auto ms-3"),
                        dbc.Button("Sair", id="btn-logout",
                                   color="outline-secondary", size="sm"),
                    ], fluid=True),
                    dark=True, color="dark", className="mb-0",
                ),
                dbc.Row([
                    # Sidebar
                    dbc.Col(
                        dbc.Nav(
                            [
                                dbc.NavLink(
                                    [html.Span(icone, className="ico"), label],
                                    id=f"nav-{tab_id}", href="#", active=(tab_id == "temp"),
                                )
                                for icone, label, tab_id in _SIDEBAR_LINKS
                            ],
                            vertical=True, pills=True,
                        ),
                        md=2, className="ap-sidebar pt-2",
                    ),
                    # Conteúdo
                    dbc.Col(html.Div(id="conteudo-principal", className="ap-content"), md=10),
                ], className="g-0"),
            ],
        ),
    ],
)


# ── Login / Logout ─────────────────────────────────────────────────────────────

# As duas telas alternam por `style`, e o estilo inline vence o da folha — então
# cada uma precisa do SEU display, não de um "visível" genérico: a de login é
# `grid` (é o que centraliza o card em `.ap-login-wrap`) e a principal é `block`.
_OCULTO            = {"display": "none"}
_LOGIN_VISIVEL     = {"display": "grid"}
_PRINCIPAL_VISIVEL = {"display": "block"}


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
    if not username or not senha:
        return (None, None, None, "Preencha usuário e senha.",
                _LOGIN_VISIVEL, _OCULTO, "")

    code, data = _api("post", "/auth/login", json_body={"username": username, "senha": senha})
    if code == 200:
        token = data.get("token", "")
        nome  = data.get("nome", username)
        role  = data.get("role", "manutencao")
        chip = html.Span(
            [nome, html.Span(role, className="role")],
            className="ap-user-chip",
        )
        return token, nome, role, "", _OCULTO, _PRINCIPAL_VISIVEL, chip

    # 0 é o código que `_api` devolve quando a requisição nem saiu (central fora
    # do ar): dizer "credenciais inválidas" nesse caso manda o técnico conferir a
    # senha por um problema que não é dele.
    msg = ("Central indisponível — tente novamente em instantes."
           if code == 0 else "Credenciais inválidas.")
    return None, None, None, msg, _LOGIN_VISIVEL, _OCULTO, ""


@callback(
    Output("jwt-token",      "data",  allow_duplicate=True),
    Output("tela-login",     "style", allow_duplicate=True),
    Output("tela-principal", "style", allow_duplicate=True),
    Input("btn-logout", "n_clicks"),
    prevent_initial_call=True,
)
def _logout(_):
    return None, _LOGIN_VISIVEL, _OCULTO


# ── Navegação ──────────────────────────────────────────────────────────────────

@callback(
    Output("active-tab", "data"),
    [Input(f"nav-{t}", "n_clicks") for _, _l, t in _SIDEBAR_LINKS],
    prevent_initial_call=True,
)
def _nav(*_):
    triggered = ctx.triggered_id or "nav-temp"
    return triggered.replace("nav-", "")


@callback(
    [Output(f"nav-{t}", "active") for _, _l, t in _SIDEBAR_LINKS],
    Input("active-tab", "data"),
)
def _marcar_aba_ativa(tab):
    """Sem isto a sidebar não mostra onde o usuário está.

    `dbc.Nav(pills=True)` só estiliza o link que tem `active=True`; a propriedade
    nunca era escrita, então os dez itens ficavam com a mesma aparência em todas
    as telas — trocar de aba mudava o conteúdo e nada mais, e voltar exigia
    lembrar em qual item se clicou.
    """
    return [t == (tab or "temp") for _, _l, t in _SIDEBAR_LINKS]


# ── Conteúdo principal ────────────────────────────────────────────────────────

@callback(
    Output("conteudo-principal", "children"),
    Input("active-tab",  "data"),
    Input("poll",        "n_intervals"),
    # O token é Input, não State: era State, e como o login não muda `active-tab`
    # nem dispara o `poll`, o painel ficava em branco de 0 a POLL_MS (5s) depois
    # de entrar — tempo suficiente para o técnico achar que o login falhou.
    Input("jwt-token",   "data"),
    State("user-role",   "data"),
    prevent_initial_call=True,
)
def _render_conteudo(tab, _, token, role):
    if not token:
        return _vazio("🔒", "Faça login para continuar.")

    renderers = {
        "temp":       lambda: _render_temp(token),
        "uso":        lambda: _render_uso(token),
        "log":        lambda: _render_log(token),
        "alarmes":    lambda: _render_alarmes(token),
        "nova":       lambda: _render_nova_manut(),
        "dispensers": lambda: _render_dispensers(token),
        "visao":      lambda: _render_visao(token),
        "trava":      lambda: _render_trava(token, role),
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
                html.Div(f"{val}°C", className=f"ap-metric-value text-{cor}"),
                dbc.Progress(value=min(val/85*100,100), color=cor, style={"height":"8px"}),
                html.Small(_hora(leit.get("ts")), className="text-muted d-block text-end mt-1"),
            ]),
        ], className="ap-metric h-100"), md=3, sm=6, xs=12, className="mb-3"))

    return html.Div([
        _titulo("🌡", "Temperaturas dos Componentes"),
        dbc.Row(cards) if cards else _vazio("🌡", "Nenhuma leitura de temperatura."),
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
        ], className="ap-row mb-1"))

    return html.Div([
        _titulo("⚙", "Desgaste e Horas de Uso"),
        html.Div(rows) if rows else _vazio("⚙", "Nenhuma leitura de desgaste."),
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
    ], className="ap-thead")

    rows = [dbc.Row([
        dbc.Col(html.Small(_hora(l.get("ts")), className="ap-mono text-muted"), md=2),
        dbc.Col(html.Small(l.get("componente","?"), className="fw-bold"), md=3),
        dbc.Col(html.Small(l.get("tipo","?"), className="text-info"), md=2),
        dbc.Col(html.Small(l.get("descricao",""), className="text-light"), md=4),
        dbc.Col(html.Small(l.get("tecnico","?"), className="text-muted"), md=1),
    ], className="ap-row") for l in logs]

    return html.Div([
        _titulo("📋", "Log de Manutenções"),
        header,
        html.Div(rows) if rows else _vazio("📋", "Nenhuma manutenção registrada."),
    ])


# ── Alarmes ────────────────────────────────────────────────────────────────────

def _render_alarmes(token):
    _, alarmes = _api("get", "/manutencao/alarmes?resolvido=false&limite=50", token=token)
    if not isinstance(alarmes, list):
        return html.P("Erro ao carregar alarmes.", className="text-danger small")

    if not alarmes:
        return html.Div([_titulo("🚨", "Alarmes"),
                         dbc.Alert("Nenhum alarme ativo.", color="success")])

    items = [dbc.ListGroupItem([
        dbc.Row([
            dbc.Col([
                html.Strong(f"[{a.get('tipo','').upper()}] "),
                html.Span(a.get("descricao","")),
                html.Br(),
                html.Small(f"Fonte: {a.get('fonte','?')} | {_hora(a.get('ts'))}",
                           className="text-muted"),
            ], md=9),
            dbc.Col(dbc.Button("Resolver",
                               id={"type":"btn-resolver","index":a.get("id",0)},
                               color="warning", size="sm"),
                    md=3, className="d-flex align-items-center"),
        ])
    ], color="danger", className="mb-1") for a in alarmes]

    return html.Div([_titulo("🚨", "Alarmes Ativos"), dbc.ListGroup(items)])


# ── Nova Manutenção ────────────────────────────────────────────────────────────

def _render_nova_manut():
    return html.Div([
        _titulo("➕", "Registrar Manutenção"),
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


# ── Visão Computacional + Balança ──────────────────────────────────────────────

def _render_visao(token):
    _, estado = _api("get", "/estado", token=token)
    if not isinstance(estado, dict):
        return html.P("Erro ao carregar estado.", className="text-danger small")

    visao    = estado.get("visao", {})
    peso     = estado.get("peso", {})
    # Três câmeras: uma por fileira de dispensers e a da mesa (a da balança).
    cam_esq  = visao.get("camera_dispenser_esq", {})
    cam_dir  = visao.get("camera_dispenser_dir", {})
    cam_mesa = visao.get("camera_mesa", {})

    # A cobertura de cada câmera sai do nº de slots que o próprio /estado
    # trouxe — a IHM não precisa de mais uma env var em sincronia com o compose
    # só para escrever "D1–D4" no cabeçalho do card.
    n_slots     = len(estado.get("dispensers", {})) or 8
    por_fileira = max(1, n_slots // 2)

    def _cor_leit(tipo):
        if not tipo:
            return "secondary"
        if "ok" in tipo:    return "success"
        if "divergencia" in tipo or "falha" in tipo: return "danger"
        return "info"

    # Buscar histórico recente de visão
    _, hist_visao = _api("get", "/api/v1/visao/historico?limite=20", token=token)
    hist_rows = []
    if isinstance(hist_visao, dict):
        for leit in hist_visao.get("leituras", []):
            ts = str(leit.get("criado_em", ""))[:16].replace("T", " ")
            hist_rows.append(
                html.Tr([
                    html.Td(html.Small(ts, className="text-muted")),
                    html.Td(dbc.Badge(leit.get("camera", "?"), color="info", className="me-1")),
                    html.Td(html.Small(f"D{leit.get('slot_id','?')}")),
                    html.Td(dbc.Badge(leit.get("tipo","?"),
                                      color=_cor_leit(leit.get("tipo","")))),
                    html.Td(html.Small(leit.get("sku_lido") or leit.get("motivo") or "—",
                                       className="text-muted")),
                    html.Td(html.Small(
                        f"{leit.get('qtd_detectada','—')} / {leit.get('qtd_esperada','—')}"
                        if leit.get("qtd_esperada") else "—"
                    )),
                ])
            )

    hist_tabela = dbc.Table(
        [
            html.Thead(html.Tr([
                html.Th("Horário"), html.Th("Câmera"), html.Th("Slot"),
                html.Th("Tipo"), html.Th("SKU/Motivo"), html.Th("Qtd det/esp"),
            ])),
            html.Tbody(hist_rows if hist_rows else [html.Tr(html.Td("Sem leituras.", colSpan=6))]),
        ],
        bordered=True, hover=True, size="sm", responsive=True,
    )

    return html.Div([
        html.H5("📷 Visão Computacional", className="mb-3"),
        dbc.Row([
            dbc.Col(dbc.Card([
                dbc.CardHeader(f"Câmera Esquerda · D1–D{por_fileira} (SKU)"),
                dbc.CardBody([
                    html.P([
                        "Última leitura: ",
                        dbc.Badge(cam_esq.get("ultima_leitura") or "—",
                                  color=_cor_leit(cam_esq.get("ultima_leitura"))),
                    ], className="mb-1 small"),
                    html.Small(f"Slot: D{cam_esq.get('slot_id') or '?'}", className="text-muted me-2"),
                    html.Small(f"Confiança: {int((cam_esq.get('confianca') or 0)*100)}%",
                               className="text-muted"),
                ]),
            ]), md=4, className="mb-3"),
            dbc.Col(dbc.Card([
                dbc.CardHeader(f"Câmera Direita · D{por_fileira + 1}–D{n_slots} (SKU)"),
                dbc.CardBody([
                    html.P([
                        "Última leitura: ",
                        dbc.Badge(cam_dir.get("ultima_leitura") or "—",
                                  color=_cor_leit(cam_dir.get("ultima_leitura"))),
                    ], className="mb-1 small"),
                    html.Small(f"Slot: D{cam_dir.get('slot_id') or '?'}", className="text-muted me-2"),
                    html.Small(f"Confiança: {int((cam_dir.get('confianca') or 0)*100)}%",
                               className="text-muted"),
                ]),
            ]), md=4, className="mb-3"),
            dbc.Col(dbc.Card([
                dbc.CardHeader("Câmera da Mesa · balança (Contagem)"),
                dbc.CardBody([
                    html.P([
                        "Última leitura: ",
                        dbc.Badge(cam_mesa.get("ultima_leitura") or "—",
                                  color=_cor_leit(cam_mesa.get("ultima_leitura"))),
                    ], className="mb-1 small"),
                    html.Small(
                        f"Detectado: {cam_mesa.get('quantidade_detectada') or '—'} / "
                        f"Esperado: {cam_mesa.get('quantidade_esperada') or '—'}",
                        className="text-muted",
                    ),
                ]),
            ]), md=4, className="mb-3"),
        ]),

        html.H5("⚖️ Balança HX711", className="mb-2"),
        dbc.Card(dbc.CardBody([
            html.P([
                "Status: ",
                dbc.Badge(
                    peso.get("ultima_leitura") or "—",
                    color=("success" if peso.get("ultima_leitura") == "peso_ok"
                           else "danger" if peso.get("ultima_leitura") == "peso_divergencia"
                           else "warning" if peso.get("ultima_leitura") == "tara_ok"
                           else "secondary"),
                ),
                html.Small(f"  Slot D{peso.get('slot_id') or '?'}", className="text-muted ms-2"),
            ], className="mb-1 small"),
            html.Small(
                f"Medido: {peso.get('peso_medido_g') or '—'} g  |  "
                f"Esperado: {peso.get('peso_esperado_g') or '—'} g  |  "
                f"Desvio: {peso.get('desvio_pct') or '—'}%"
                if peso.get("ultima_leitura") in ("peso_ok", "peso_divergencia")
                else "Aguardando pesagem…",
                className="text-muted",
            ),
        ]), className="mb-3"),

        html.H5("📋 Histórico de Leituras de Visão", className="mb-2"),
        html.Div(hist_tabela, style={"maxHeight": "350px", "overflowY": "auto"}),
    ])


# ── Triple Check — Trava de Emergência ─────────────────────────────────────────

def _render_trava(token, role):
    _, estado = _api("get", "/estado", token=token)
    trava = (estado or {}).get("trava", {})
    ativa = trava.get("ativa", False)

    if ativa:
        status_card = dbc.Alert([
            html.H4("⛔ TRAVA ATIVA", className="alert-heading"),
            html.Hr(),
            html.P([html.Strong("OS: "), trava.get("os_id", "?")], className="mb-1 small"),
            html.P([html.Strong("Slot: "), f"D{trava.get('slot_id', '?')}"], className="mb-1 small"),
            html.P([html.Strong("Motivo: "), trava.get("motivo", "")], className="mb-2 small"),
        ], color="danger")
    else:
        status_card = dbc.Alert([
            html.H5("✅ Sistema liberado", className="alert-heading mb-0"),
            html.P("Nenhuma trava de Triple Check ativa no momento.",
                   className="mb-0 small mt-1"),
        ], color="success")

    # Botão de liberar só aparece para admin e se trava ativa
    btn_liberar = html.Div()
    if role == "admin" and ativa:
        btn_liberar = html.Div([
            html.Hr(),
            html.P(
                "⚠ Ao liberar, a OS retomará do ponto em que parou. "
                "Verifique fisicamente se o erro foi corrigido antes de prosseguir.",
                className="text-warning small",
            ),
            dbc.Button(
                "🔓 Liberar Trava e Retomar OS",
                id="btn-liberar-trava",
                color="warning", size="lg", className="w-100",
            ),
            html.Div(id="msg-liberar-trava", className="mt-2"),
        ])
    elif not ativa:
        btn_liberar = html.Div()
    else:
        btn_liberar = html.P(
            "Apenas administradores podem liberar a trava.",
            className="text-muted small mt-3",
        )

    return html.Div([
        html.H5("⛔ Triple Check — Trava de Emergência", className="mb-3"),
        html.P(
            "O Triple Check valida 3 fontes independentes após cada dispensa: "
            "contagem do dispenser, câmera de mesa e balança HX711. "
            "Basta UMA fonte divergir para a OS ser suspensa e aguardar intervenção. "
            "Fonte que não conseguiu medir (falha de leitura da câmera, sensor fora "
            "do ar, timeout) não conta como divergência: gera alarme, não trava.",
            className="text-muted small mb-3",
        ),
        status_card,
        btn_liberar,
    ])


@callback(
    Output("msg-liberar-trava", "children"),
    Input("btn-liberar-trava", "n_clicks"),
    State("jwt-token", "data"),
    prevent_initial_call=True,
)
def _cb_liberar_trava(n, token):
    if not n:
        return no_update
    status, resp = _api("post", "/api/v1/admin/liberar-trava", token=token)
    if status == 200:
        return dbc.Alert("✅ Trava liberada com sucesso. OS retomando…", color="success")
    return dbc.Alert(
        f"Erro ao liberar trava: {resp.get('detail', 'Falha desconhecida')}",
        color="danger",
    )


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

    rows_com_download = []
    for os_ in historico:
        os_id  = os_.get("os_id","?")
        sts    = os_.get("status","?")
        cor    = _cor_status_os(sts)
        cat    = os_.get("categoria","—")
        criado = str(os_.get("criado_em",""))[:16]

        rows_com_download.append(dbc.Row([
            dbc.Col(
                dbc.Button(os_id, id={"type":"btn-os-detalhe","index":os_id},
                           color="link", size="sm", className="p-0 text-info"),
                md=3,
            ),
            dbc.Col(html.Small(cat, className="text-muted"), md=2),
            dbc.Col(dbc.Badge(sts, color=cor, className="small"), md=2),
            dbc.Col(html.Small(criado, className="text-muted"), md=1),
            dbc.Col(
                dbc.ButtonGroup([
                    dbc.Button(
                        "CSV",
                        id={"type": "btn-relatorio", "index": os_id, "formato": "csv"},
                        color="outline-success", size="sm", n_clicks=0,
                    ),
                    dbc.Button(
                        "XLSX",
                        id={"type": "btn-relatorio", "index": os_id, "formato": "xlsx"},
                        color="outline-primary", size="sm", n_clicks=0,
                    ),
                ], size="sm"),
                md=1,
            ),
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
        dbc.Col(html.Small("Criado em", className="text-muted fw-bold"), md=1),
        dbc.Col(html.Small("Relatório", className="text-muted fw-bold"), md=1),
        dbc.Col(html.Small("Alterar Status", className="text-muted fw-bold"), md=3),
    ], className="mb-2")

    return html.Div([
        html.H5("🗂 Histórico de Ordens (OS)", className="mb-1"),
        html.P("Clique no ID da OS para ver o JSON. Botões CSV/XLSX para exportar relatório.",
               className="text-muted small mb-3"),
        header,
        html.Div(rows_com_download) if rows_com_download else html.P("Sem ordens registradas.", className="text-muted small"),
        html.Div(id="msg-relatorio", className="mt-2"),
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


# ── Download de relatório (server-side) ───────────────────────────────────────
#
# Os botões já foram âncoras para
# `{BACKEND_URL}/api/v1/relatorio/...?formato=csv&token={jwt}`. Duas coisas
# erradas em uma linha:
#
#   1. `BACKEND_URL` é `http://central-computer:8000` — nome DNS da rede Docker
#      `apsen-net`. Quem resolve esse nome é o container; o navegador do
#      operador, não. O link simplesmente não baixava nada.
#   2. o JWT ia na query string, ou seja, no histórico do navegador, no header
#      `Referer` e no log de acesso do central.
#
# Agora quem busca o arquivo é o processo da IHM, que está DENTRO da rede
# Docker (o hostname resolve) e manda o token no header `Authorization` (fora
# da URL). Os bytes voltam ao navegador pelo `dcc.Download`, pela conexão que
# o operador já tem aberta com a IHM.
#
# A alternativa era publicar uma `PUBLIC_BACKEND_URL` (ex.: localhost:8000) só
# para os links; ela exigiria expor o central ao navegador e manter mais uma
# variável em sincronia com o compose — e ainda deixaria o token na URL.

_TIPOS_RELATORIO = {
    "csv":  "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_RE_FILENAME = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.I)


def _nome_do_arquivo(resp, os_id: str, formato: str) -> str:
    """Nome vem do `Content-Disposition` do backend; o fallback repete o padrão."""
    achado = _RE_FILENAME.search(resp.headers.get("Content-Disposition", "") or "")
    return achado.group(1).strip() if achado else f"relatorio_{os_id}.{formato}"


def _erro_relatorio(resp, os_id: str) -> str:
    if resp.status_code == 401:
        return "Sessão expirada. Faça login novamente."
    if resp.status_code == 404:
        return f"OS {os_id} não encontrada."
    try:
        detalhe = (resp.json() or {}).get("detail", "")
    except Exception:
        detalhe = ""
    return f"Erro {resp.status_code} ao gerar o relatório{': ' + detalhe if detalhe else '.'}"


def _buscar_relatorio(os_id: str, formato: str, token: str) -> tuple[dict, str]:
    """Baixa o relatório da OS e devolve `(payload_do_dcc_download, erro)`.

    Chamada SERVER-SIDE: roda no container da IHM, onde `BACKEND_URL` resolve.
    O token vai no header — nunca em `params`, nunca na URL.
    """
    if formato not in _TIPOS_RELATORIO:
        return None, f"Formato de relatório inválido: {formato}"

    try:
        resp = requests.get(
            f"{BACKEND_URL}/api/v1/relatorio/os/{os_id}",
            params={"formato": formato},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except Exception as exc:
        return None, f"Falha ao contatar o servidor: {exc}"

    if resp.status_code != 200:
        return None, _erro_relatorio(resp, os_id)

    return dcc.send_bytes(
        resp.content,
        _nome_do_arquivo(resp, os_id, formato),
        type=_TIPOS_RELATORIO[formato],
    ), ""


def _baixar_relatorio(n_clicks_list, token):
    """Corpo do callback dos botões CSV/XLSX. Registrado logo abaixo."""
    if not any(n for n in (n_clicks_list or []) if n):
        return no_update, no_update
    if not token:
        return no_update, dbc.Alert("Sessão expirada.", color="danger")

    alvo = ctx.triggered_id
    if not (alvo and isinstance(alvo, dict)):
        return no_update, no_update

    dados, erro = _buscar_relatorio(alvo["index"], alvo["formato"], token)
    if erro:
        return no_update, dbc.Alert(erro, color="danger", dismissable=True)
    return dados, None


# Registro explícito, em vez de `@callback` em cima da função: o decorador
# devolve o wrapper do Dash, que só roda dentro de um callback context. Assim
# `_baixar_relatorio` continua sendo função Python comum, testável direto
# (tests/test_ihm_relatorio.py) sem subir servidor nem navegador.
_cb_baixar_relatorio = callback(
    Output("download-relatorio", "data"),
    Output("msg-relatorio", "children"),
    Input({"type": "btn-relatorio", "index": ALL, "formato": ALL}, "n_clicks"),
    State("jwt-token", "data"),
    prevent_initial_call=True,
)(_baixar_relatorio)


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
    State({"type":"btn-toggle-ativo","index":ALL}, "id"),
    State("jwt-token", "data"),
    prevent_initial_call=True,
)
def _toggle_ativo(n_clicks_list, labels, ids, token):
    """Ativa/desativa o usuário do botão CLICADO.

    O label ("Ativar"/"Desativar") é que decide a ação, e ele era lido pelo
    "primeiro botão com n_clicks". `n_clicks` é cumulativo por componente:
    depois de qualquer clique anterior, aquele botão continua com n_clicks > 0
    e era ele o encontrado — o `username` vinha certo do `ctx.triggered_id`,
    mas o VERBO saía do usuário errado. Clicar em "Ativar" de um técnico podia
    desativá-lo, porque outra linha da tabela dizia "Desativar".

    Agora a posição sai do casamento com o id disparado, como em
    `_salvar_role`. (O `idx` que existia aqui era uma versão inacabada desse
    mesmo casamento, sem efeito nenhum.)
    """
    if not any(n for n in (n_clicks_list or []) if n):
        return no_update
    triggered = ctx.triggered_id
    if not (triggered and isinstance(triggered, dict)) or not token:
        return no_update
    username = triggered["index"]

    posicao = next((i for i, id_ in enumerate(ids or [])
                    if id_.get("index") == username), None)
    if posicao is None:
        return no_update

    label = (labels or [None] * (posicao + 1))[posicao] or "Desativar"
    acao  = "desativar" if label == "Desativar" else "ativar"
    _api("put", f"/manutencao/usuarios/{username}/{acao}", token=token)
    return "usuarios"


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8051, debug=False)
