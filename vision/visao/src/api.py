"""API HTTP e dashboard web, sem dependencia externa.

Usa apenas a biblioteca padrao de proposito: numa esteira corporativa, cada
dependencia nova e uma conversa com seguranca da informacao. Quando o projeto
passar de uma bancada para varias, o caminho natural e trocar este arquivo por
FastAPI + Postgres — os contratos de endpoint abaixo foram desenhados para essa
migracao ser mecanica, nao uma reescrita.

Endpoints:
    GET /                        dashboard
    GET /api/estado              situacao atual de cada dispenser
    GET /api/eventos?limite=&tipo=&dispenser=&criticos=1
    GET /api/resumo?horas=24
    GET /api/integridade         verificacao da trilha de auditoria
    GET /api/saude               liveness/readiness
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from eventos import RegistroDeEventos

RAIZ = Path(__file__).resolve().parent.parent


class _Handler(BaseHTTPRequestHandler):
    registro: RegistroDeEventos = None  # type: ignore[assignment]
    silencioso: bool = True

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        params = parse_qs(url.query)
        rota = url.path.rstrip("/") or "/"

        try:
            if rota == "/":
                return self._html(PAGINA)
            if rota == "/api/estado":
                return self._json({"estado": self.registro.estado()})
            if rota == "/api/eventos":
                return self._json({"eventos": self.registro.listar(
                    limite=int(params.get("limite", ["100"])[0]),
                    tipo=params.get("tipo", [None])[0],
                    dispenser=(int(params["dispenser"][0])
                               if "dispenser" in params else None),
                    apenas_criticos=params.get("criticos", ["0"])[0] in ("1", "true"),
                )})
            if rota == "/api/resumo":
                return self._json(self.registro.resumo(
                    horas=int(params.get("horas", ["24"])[0])))
            if rota == "/api/integridade":
                return self._json(self.registro.verificar_integridade())
            if rota == "/api/saude":
                return self._json({"ok": True, "estacao": self.registro.estacao})
        except Exception as exc:  # nunca derruba o servidor por causa de um GET
            return self._json({"erro": str(exc)}, codigo=500)

        self._json({"erro": "rota nao encontrada"}, codigo=404)

    # ------------------------------------------------------------------ #
    def _json(self, dados, codigo: int = 200) -> None:
        corpo = json.dumps(dados, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(corpo)

    def _html(self, texto: str) -> None:
        corpo = texto.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, formato, *args):  # silencia o log padrao
        if not self.silencioso:
            super().log_message(formato, *args)


# --------------------------------------------------------------------------- #
class ServidorPainel:
    """Sobe a API numa thread, para conviver com o loop de video."""

    def __init__(self, registro: RegistroDeEventos, porta: int = 8000,
                 endereco: str = "0.0.0.0") -> None:
        _Handler.registro = registro
        self.porta = porta
        self._servidor = ThreadingHTTPServer((endereco, porta), _Handler)
        self._thread = threading.Thread(target=self._servidor.serve_forever, daemon=True)

    def iniciar(self) -> str:
        self._thread.start()
        return f"http://localhost:{self.porta}"

    def parar(self) -> None:
        try:
            self._servidor.shutdown()
            self._servidor.server_close()
        except Exception:
            pass


PAGINA = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Conferencia de dispensers</title>
<style>
 :root{--bg:#0f1115;--card:#171a21;--linha:#252a34;--txt:#e6e9ef;--fraco:#9aa3b2;
       --ok:#3ecf8e;--erro:#f0616d;--alerta:#f2b33d;--info:#5aa9f7}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--txt);
   font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
 header{padding:20px 28px;border-bottom:1px solid var(--linha);display:flex;
   align-items:baseline;gap:16px;flex-wrap:wrap}
 h1{font-size:19px;margin:0;font-weight:650}
 .sub{color:var(--fraco);font-size:13px}
 main{padding:24px 28px;max-width:1200px;margin:0 auto}
 .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:26px}
 .kpi{background:var(--card);border:1px solid var(--linha);border-radius:10px;padding:14px 16px}
 .kpi .v{font-size:26px;font-weight:650;letter-spacing:-.5px}
 .kpi .r{color:var(--fraco);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
 h2{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--fraco);
    margin:26px 0 12px;font-weight:600}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}
 .disp{background:var(--card);border:1px solid var(--linha);border-radius:10px;padding:16px;
   border-left:4px solid var(--linha)}
 .disp.ok{border-left-color:var(--ok)} .disp.erro{border-left-color:var(--erro)}
 .disp.alerta{border-left-color:var(--alerta)} .disp.vazio{border-left-color:var(--fraco)}
 .disp h3{margin:0 0 4px;font-size:15px} .disp .med{color:var(--fraco);font-size:13px;
   margin-bottom:10px;min-height:20px}
 .tag{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600;
   letter-spacing:.03em}
 .tag.ok{background:rgba(62,207,142,.15);color:var(--ok)}
 .tag.erro{background:rgba(240,97,109,.15);color:var(--erro)}
 .tag.alerta{background:rgba(242,179,61,.15);color:var(--alerta)}
 .tag.vazio{background:rgba(154,163,178,.15);color:var(--fraco)}
 .barra{height:7px;background:#0b0d11;border-radius:99px;overflow:hidden;margin-top:12px}
 .barra i{display:block;height:100%;background:var(--info)}
 .barra i.baixo{background:var(--alerta)} .barra i.critico{background:var(--erro)}
 .est{display:flex;justify-content:space-between;font-size:12px;color:var(--fraco);margin-top:6px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th{text-align:left;color:var(--fraco);font-weight:600;font-size:11px;text-transform:uppercase;
    letter-spacing:.05em;padding:8px 10px;border-bottom:1px solid var(--linha)}
 td{padding:9px 10px;border-bottom:1px solid var(--linha);vertical-align:top}
 tr:hover td{background:#1b1f27}
 .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--fraco)}
 .selo{padding:6px 12px;border-radius:8px;font-size:12px;font-weight:600}
 .selo.ok{background:rgba(62,207,142,.12);color:var(--ok)}
 .selo.erro{background:rgba(240,97,109,.12);color:var(--erro)}
 footer{color:var(--fraco);font-size:12px;padding:20px 28px;text-align:center}
</style></head><body>
<header>
  <h1>Conferencia de medicamentos nos dispensers</h1>
  <span class="sub" id="atualizado">carregando...</span>
  <span style="flex:1"></span>
  <span id="selo-integridade" class="selo">verificando trilha...</span>
</header>
<main>
  <div class="kpis" id="kpis"></div>
  <h2>Dispensers</h2>
  <div class="grid" id="dispensers"></div>
  <h2>Ocorrencias recentes</h2>
  <table><thead><tr><th>Quando</th><th>Disp.</th><th>Veredito</th>
    <th>Medicamento</th><th>Detalhe</th><th>Conf.</th></tr></thead>
    <tbody id="eventos"></tbody></table>
</main>
<footer>Atualiza sozinho a cada 3 segundos &middot; trilha de auditoria encadeada por hash</footer>
<script>
const CLASSE = {OK:'ok', ERRO_POSICAO:'erro', DIVERGENCIA:'erro',
                NAO_CADASTRADO:'erro', VAZIO:'vazio', INDETERMINADO:'alerta'};
const hora = s => s ? new Date(s).toLocaleTimeString('pt-BR') : '-';

async function pegar(u){ const r = await fetch(u); return r.json(); }

function pintarKpis(resumo, estado){
  const repor = estado.filter(e => e.precisa_repor).length;
  const erro  = estado.filter(e => ['ERRO_POSICAO','DIVERGENCIA','NAO_CADASTRADO']
                .includes(e.veredito)).length;
  const kpis = [
    ['dispensers com erro', erro, erro ? 'var(--erro)' : 'var(--ok)'],
    ['precisam repor', repor, repor ? 'var(--alerta)' : 'var(--txt)'],
    ['ocorrencias 24h', resumo.criticos ?? 0, 'var(--txt)'],
    ['eventos 24h', resumo.eventos ?? 0, 'var(--txt)'],
  ];
  document.getElementById('kpis').innerHTML = kpis.map(([r,v,c]) =>
    `<div class="kpi"><div class="v" style="color:${c}">${v}</div><div class="r">${r}</div></div>`
  ).join('');
}

function pintarDispensers(estado){
  document.getElementById('dispensers').innerHTML = estado.map(d => {
    const cls = CLASSE[d.veredito] || 'vazio';
    const pct = Math.round((d.fracao ?? 0) * 100);
    const nivelCls = d.caixas === null || d.caixas === undefined ? ''
                   : (d.caixas === 0 ? 'critico' : (d.precisa_repor ? 'baixo' : ''));
    const porCodigo = (d.unidades ?? null) === null ? null : `${d.unidades} lida(s)`;
    const caixas = (d.caixas ?? null) === null
      ? (porCodigo || '-') : `${d.caixas} caixa(s)`;
    return `<div class="disp ${cls}">
      <h3>Dispenser ${d.dispenser} <span class="tag ${cls}">${d.veredito || 'sem dado'}</span></h3>
      <div class="med">${d.medicamento || '&mdash;'}</div>
      <div class="barra"><i class="${nivelCls}" style="width:${pct}%"></i></div>
      <div class="est"><span>${caixas}</span><span>${
        porCodigo && d.caixas != null ? porCodigo : pct + '% cheio'}</span></div>
      <div class="est"><span class="mono">${d.estacao}</span><span>${hora(d.momento)}</span></div>
    </div>`;
  }).join('') || '<p class="sub">Nenhuma estacao reportou ainda.</p>';
}

function pintarEventos(eventos){
  document.getElementById('eventos').innerHTML = eventos.map(e => {
    const cls = CLASSE[e.veredito] || 'vazio';
    return `<tr>
      <td class="mono">${hora(e.momento)}</td>
      <td>${e.dispenser ?? '-'}</td>
      <td><span class="tag ${cls}">${e.veredito || e.tipo}</span></td>
      <td>${e.medicamento || '-'}</td>
      <td>${e.detalhe || ''}</td>
      <td class="mono">${e.confianca != null ? (e.confianca*100).toFixed(0)+'%' : '-'}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" class="sub">Sem eventos ainda.</td></tr>';
}

async function atualizar(){
  try{
    const [estado, eventos, resumo, integridade] = await Promise.all([
      pegar('/api/estado'), pegar('/api/eventos?limite=25&criticos=1'),
      pegar('/api/resumo'), pegar('/api/integridade')]);
    pintarKpis(resumo, estado.estado);
    pintarDispensers(estado.estado);
    pintarEventos(eventos.eventos);
    const selo = document.getElementById('selo-integridade');
    selo.textContent = integridade.integra
      ? `trilha integra (${integridade.total} eventos)`
      : `TRILHA VIOLADA no evento ${integridade.id_problema}`;
    selo.className = 'selo ' + (integridade.integra ? 'ok' : 'erro');
    document.getElementById('atualizado').textContent =
      'atualizado ' + new Date().toLocaleTimeString('pt-BR');
  }catch(e){
    document.getElementById('atualizado').textContent = 'sem conexao com a estacao';
  }
}
atualizar(); setInterval(atualizar, 3000);
</script></body></html>
"""


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Sobe o painel de supervisao.")
    p.add_argument("--porta", type=int, default=8000)
    p.add_argument("--banco", type=str, default=None)
    args = p.parse_args()

    registro = RegistroDeEventos(args.banco)
    servidor = ServidorPainel(registro, args.porta)
    print(f"Painel em {servidor.iniciar()}  (Ctrl+C encerra)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nencerrando")
    finally:
        servidor.parar()
        registro.fechar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
