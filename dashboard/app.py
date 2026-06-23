"""
APSEN - Dashboard Executivo (porta 8050) - Responsivo
"""
import json, os, threading, time
import dash
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import requests, websocket
from dash import Input, Output, State, callback, dcc, html

BACKEND_HTTP = os.getenv("BACKEND_URL", "http://backend:8000")
BACKEND_WS   = os.getenv("BACKEND_WS",  "ws://backend:8000/ws")

_lock   = threading.Lock()
_estado = {"contagem": 0, "meta": 1000, "lote_id": "—",
           "status": "idle", "velocidade": 0, "alarme": None}
_token_holder = {"token": ""}

def _ws_thread(token_holder):
    def on_message(ws, msg):
        with _lock:
            _estado.update(json.loads(msg))
    def on_close(ws, *a):
        time.sleep(5)
        if token_holder.get("token"):
            _ws_thread(token_holder)
    ws = websocket.WebSocketApp(
        f"{BACKEND_WS}?token={token_holder.get('token','')}",
        on_message=on_message, on_close=on_close,
        on_error=lambda ws, e: None,
    )
    ws.run_forever()

def _start_ws(token):
    _token_holder["token"] = token
    threading.Thread(target=_ws_thread, args=(_token_holder,), daemon=True).start()

def api(method, path, token=None, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = getattr(requests, method)(f"{BACKEND_HTTP}{path}", headers=headers, timeout=5, **kwargs)
        return r.json() if r.ok else None
    except Exception:
        return None

app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP],
                suppress_callback_exceptions=True, title="APSEN – Dashboard", update_title=None)
server = app.server

AZUL, AZUL2 = "#003087", "#0057b8"
VERDE, VERM, AMAR, CINZA = "#28a745", "#dc3545", "#ffc107", "#6c757d"
STATUS_COR   = {"idle": CINZA, "running": VERDE, "paused": AMAR, "alarm": VERM}
STATUS_LABEL = {"idle": "AGUARDANDO", "running": "EM PRODUCAO", "paused": "PAUSADO", "alarm": "ALARME"}

# ── Login ──────────────────────────────────────────────────────────────────────
def pagina_login(erro=""):
    return html.Div(style={
        "minHeight": "100vh",
        "background": f"linear-gradient(135deg, {AZUL} 0%, #001a4d 100%)",
        "display": "flex", "alignItems": "center", "justifyContent": "center", "padding": "16px",
    }, children=[
        html.Div(style={
            "background": "#fff", "borderRadius": "16px",
            "padding": "clamp(24px,5vw,48px) clamp(20px,5vw,40px)",
            "width": "100%", "maxWidth": "400px",
            "boxShadow": "0 20px 60px rgba(0,0,0,.3)",
        }, children=[
            html.Div([
                html.H1("APSEN", style={"color": AZUL, "fontWeight": "900",
                                         "letterSpacing": "4px",
                                         "fontSize": "clamp(28px,6vw,36px)", "margin": "0"}),
                html.P("Dashboard Executivo", style={"color": CINZA, "fontSize": "14px", "margin": "4px 0 32px"}),
            ], className="text-center"),
            html.Label("Usuario", style={"fontWeight": "600", "fontSize": "13px", "color": "#555"}),
            dbc.Input(id="login-user", type="text", size="lg", className="mb-3"),
            html.Label("Senha", style={"fontWeight": "600", "fontSize": "13px", "color": "#555"}),
            dbc.Input(id="login-senha", type="password", size="lg", className="mb-4"),
            dbc.Button("ENTRAR", id="btn-login", size="lg", className="w-100",
                       style={"background": AZUL, "border": "none", "fontWeight": "700", "letterSpacing": "2px"}),
            html.Div(erro, style={"color": VERM, "textAlign": "center", "marginTop": "12px", "fontSize": "13px"}),
        ]),
    ])

def kpi(titulo, valor, sub="", cor=AZUL):
    return html.Div(style={
        "background": "#fff", "borderRadius": "12px",
        "padding": "clamp(12px,2vw,24px) clamp(12px,2vw,28px)",
        "height": "100%", "boxShadow": "0 2px 12px rgba(0,0,0,.06)",
        "borderLeft": f"4px solid {cor}",
    }, children=[
        html.Div(titulo, style={"fontSize": "11px", "color": "#999",
                                 "textTransform": "uppercase", "letterSpacing": "1px", "fontWeight": "600"}),
        html.Div(valor, style={"fontSize": "clamp(20px,4vw,38px)", "fontWeight": "900",
                                "color": "#1a1a2e", "lineHeight": "1.1", "margin": "4px 0"}),
        html.Div(sub, style={"fontSize": "11px", "color": "#aaa"}),
    ])

def pagina_dash(auth):
    token = auth.get("token")
    nome  = auth.get("nome", "")
    role  = auth.get("role", "")
    return html.Div(style={"background": "#f0f4f8", "minHeight": "100vh",
                            "fontFamily": "'Inter', sans-serif"}, children=[
        # Header
        html.Div(style={
            "background": f"linear-gradient(90deg, {AZUL} 0%, {AZUL2} 100%)",
            "color": "#fff", "padding": "0 16px", "height": "56px",
            "display": "flex", "alignItems": "center", "justifyContent": "space-between",
            "boxShadow": "0 2px 8px rgba(0,0,0,.2)",
        }, children=[
            html.Div([
                html.Span("APSEN", style={"fontSize": "clamp(16px,4vw,22px)",
                                           "fontWeight": "900", "letterSpacing": "3px"}),
                html.Span(" Dashboard Executivo", className="dash-header-sub",
                          style={"fontSize": "13px", "opacity": ".7", "marginLeft": "8px"}),
            ]),
            html.Div([
                html.Span(f"{nome} - {role.upper()}", className="d-none d-sm-inline",
                          style={"fontSize": "13px", "opacity": ".8", "marginRight": "12px"}),
                dbc.Button("Sair", id="btn-logout", size="sm",
                           style={"background": "rgba(255,255,255,.15)",
                                  "border": "1px solid rgba(255,255,255,.3)", "color": "#fff"}),
            ], style={"display": "flex", "alignItems": "center"}),
        ]),
        # Conteudo
        html.Div(className="dash-content", children=[
            dbc.Row(id="kpis", className="g-3 mb-4"),
            html.Div(id="alarme-banner"),
            dbc.Row([
                dbc.Col(html.Div(style={
                    "background": "#fff", "borderRadius": "12px", "padding": "20px",
                    "boxShadow": "0 2px 8px rgba(0,0,0,.06)", "height": "100%",
                }, children=[
                    html.H3("Producao ao Longo do Tempo",
                            style={"margin": "0 0 12px", "fontSize": "15px", "color": AZUL, "fontWeight": "700"}),
                    dcc.Graph(id="graph-prod", style={"height": "320px"},
                              className="dash-graph", config={"displayModeBar": False}),
                ]), width=12, lg=9, className="mb-4"),
                dbc.Col(html.Div([
                    html.Div(style={
                        "background": "#fff", "borderRadius": "12px", "padding": "16px",
                        "boxShadow": "0 2px 8px rgba(0,0,0,.06)", "marginBottom": "16px",
                    }, children=[
                        html.H3("Progresso do Lote",
                                style={"margin": "0 0 8px", "fontSize": "15px", "color": AZUL, "fontWeight": "700"}),
                        dcc.Graph(id="gauge-prog", style={"height": "200px"}, config={"displayModeBar": False}),
                    ]),
                    html.Div(id="painel-os", style={
                        "background": "#fff", "borderRadius": "12px", "padding": "16px",
                        "boxShadow": "0 2px 8px rgba(0,0,0,.06)",
                    }),
                ]), width=12, lg=3, className="mb-4"),
            ], className="g-3"),
        ]),
        dcc.Interval(id="iv-dash", interval=3000),
    ])

# ── Layout ──────────────────────────────────────────────────────────────────────
app.layout = html.Div([
    dcc.Location(id="url"),
    dcc.Store(id="auth-store", storage_type="session"),
    html.Div(id="app-root"),
])

@callback(Output("app-root", "children"),
          Input("auth-store", "data"), Input("url", "pathname"))
def root(auth, _):
    if not auth or not auth.get("token"): return pagina_login()
    return pagina_dash(auth)

@callback(Output("auth-store", "data"),
          Output("app-root", "children", allow_duplicate=True),
          Input("btn-login", "n_clicks"),
          State("login-user", "value"), State("login-senha", "value"),
          prevent_initial_call=True)
def fazer_login(n, username, senha):
    if not username or not senha:
        return dash.no_update, pagina_login("Preencha usuario e senha.")
    resp = api("post", "/auth/login", json={"username": username, "senha": senha})
    if resp and "token" in resp:
        _start_ws(resp["token"])
        return resp, dash.no_update
    return dash.no_update, pagina_login("Usuario ou senha incorretos.")

@callback(Output("auth-store", "data", allow_duplicate=True),
          Input("btn-logout", "n_clicks"), prevent_initial_call=True)
def logout(_):
    return None

@callback(Output("kpis", "children"), Output("alarme-banner", "children"),
          Output("graph-prod", "figure"), Output("gauge-prog", "figure"),
          Output("painel-os", "children"),
          Input("iv-dash", "n_intervals"), State("auth-store", "data"))
def atualizar(_, auth):
    token = (auth or {}).get("token")
    with _lock:
        est = dict(_estado)

    contagem = est.get("contagem", 0)
    meta     = est.get("meta", 1)
    status   = est.get("status", "idle")
    vel      = est.get("velocidade", 0)
    lote_id  = est.get("lote_id", "—")
    alarme   = est.get("alarme")
    prog     = min(100, round(contagem / max(meta, 1) * 100, 1))

    kpis = [
        dbc.Col(kpi("Contagem Atual", f"{contagem:,}", f"Lote: {lote_id}", AZUL),  width=6, md=4, xl=2),
        dbc.Col(kpi("Meta",           f"{meta:,}",     "unidades",          CINZA), width=6, md=4, xl=2),
        dbc.Col(kpi("Progresso",      f"{prog}%",       f"{contagem:,} / {meta:,}",
                    VERDE if prog >= 80 else AMAR),                                 width=6, md=4, xl=2),
        dbc.Col(kpi("Velocidade",     f"{vel}",         "un/min",            AZUL2),width=6, md=4, xl=3),
        dbc.Col(kpi("Status",         STATUS_LABEL.get(status, status.upper()), "",
                    STATUS_COR.get(status, CINZA)),                                 width=12,md=4, xl=3),
    ]

    banner = html.Div([html.Strong("⚠ ALARME: "), alarme],
        style={"background": "#fff3cd", "border": f"1px solid {AMAR}", "borderRadius": "10px",
               "padding": "14px 20px", "fontWeight": "600", "color": "#856404",
               "marginBottom": "20px"}) if alarme else html.Div()

    hist = api("get", f"/historico?lote_id={lote_id}&limite=200", token=token) or []
    hist = list(reversed(hist))
    fig = go.Figure()
    if hist:
        fig.add_trace(go.Scatter(x=[r["ts"] for r in hist], y=[r["valor"] for r in hist],
            mode="lines", line=dict(color=AZUL, width=2.5),
            fill="tozeroy", fillcolor="rgba(0,48,135,0.08)", name="Contagem"))
        fig.add_hline(y=meta, line_dash="dash", line_color=VERM,
                      annotation_text=f"Meta: {meta:,}", annotation_position="top right")
    fig.update_layout(paper_bgcolor="white", plot_bgcolor="white",
        margin=dict(l=10, r=10, t=10, b=30),
        xaxis=dict(showgrid=False, tickangle=-30, tickfont=dict(size=10)),
        yaxis=dict(showgrid=True, gridcolor="#f0f0f0"),
        hovermode="x unified", showlegend=False)

    gauge = go.Figure(go.Indicator(
        mode="gauge+number+delta", value=prog,
        delta={"reference": 100, "relative": False, "valueformat": ".1f", "suffix": "%"},
        number={"suffix": "%", "font": {"size": 32, "color": AZUL}},
        gauge={"axis": {"range": [0, 100], "tickcolor": "#ccc"}, "bar": {"color": AZUL},
               "steps": [{"range": [0, 50], "color": "#f8f9fa"},
                          {"range": [50, 80], "color": "#e8f4fd"},
                          {"range": [80, 100], "color": "#d4edda"}],
               "threshold": {"line": {"color": VERDE, "width": 3}, "thickness": 0.75, "value": 100}}))
    gauge.update_layout(paper_bgcolor="white", margin=dict(l=20, r=20, t=20, b=20), height=200)

    ordens = api("get", "/ordens?status_os=em_andamento", token=token) or []
    os_items = []
    for o in ordens[:4]:
        s = o.get("status", "")
        cor = {"em_andamento": VERDE, "aberto": AZUL, "concluido": CINZA}.get(s, CINZA)
        os_items.append(html.Div([
            html.Div([
                html.Strong(o["os_id"], style={"fontSize": "13px", "color": AZUL}),
                html.Span(s.replace("_", " ").upper(),
                          style={"fontSize": "10px", "fontWeight": "700",
                                 "background": cor, "color": "#fff",
                                 "borderRadius": "4px", "padding": "1px 8px", "marginLeft": "8px"}),
            ]),
            html.Div(o["produto"], style={"fontSize": "12px", "color": "#888"}),
            html.Div(f"Meta: {o['meta']:,} un | Resp: {o.get('responsavel','—')}",
                     style={"fontSize": "11px", "color": "#aaa"}),
        ], style={"borderBottom": "1px solid #f0f0f0", "paddingBottom": "10px", "marginBottom": "10px"}))

    painel_os = html.Div([
        html.H3("Ordens em Andamento",
                style={"margin": "0 0 14px", "fontSize": "15px", "color": AZUL, "fontWeight": "700"}),
        *(os_items or [html.P("Nenhuma OS em andamento.", style={"color": "#aaa", "fontSize": "13px"})]),
    ])

    return kpis, banner, fig, gauge, painel_os

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8050, debug=False)
