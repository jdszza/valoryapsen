"""Publicacao do nivel medido para o backend Apsen (Flask).

A estacao mede caixas; o backend guarda unidades e decide o que fazer com a
mudanca (baixa FEFO, atribuicao a ordem, desvio). Este modulo e so o cano entre
os dois — de proposito burro quanto a regra de negocio, porque regra duplicada
nas duas pontas diverge na primeira alteracao.

Duas coisas ele NAO delega, porque sao propriedades do lado que enxerga:

1. HISTERESE. A contagem oscila: media movel, erro de +-1 caixa, mao do
   operador atravessando a zona. Publicar cada oscilacao encheria o estoque de
   "saiu 1, voltou 1" e cada par desses viraria dois desvios. Entao um valor so
   e publicado depois de se manter estavel por alguns segundos.

2. FILA COM REENVIO. A estacao continua gravando em eventos.db mesmo com o
   backend fora do ar — e justamente quando a rede cai que nao se pode parar de
   registrar. O envio vai para uma fila em memoria e e retentado; o loop de
   video nunca bloqueia esperando HTTP.

So biblioteca padrao, pela mesma razao do api.py: dependencia nova em esteira
corporativa e uma conversa com seguranca da informacao.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class Leitura:
    dispenser: int
    sku: str | None
    medicamento: str | None
    caixas: int | None
    confianca: float
    veredito: str
    detalhe: str
    momento: str
    unidades_vistas: int | None = None

    def para_dict(self) -> dict:
        return {
            "dispenser": self.dispenser,
            "sku": self.sku,
            "medicamento": self.medicamento,
            "caixas": self.caixas,
            "confianca": round(self.confianca, 3),
            "veredito": self.veredito,
            "detalhe": self.detalhe,
            "momento": self.momento,
            "unidades_vistas": self.unidades_vistas,
        }


class PublicadorEstoque:
    """Envia o nivel estavel de cada dispenser para o backend, com reenvio."""

    def __init__(
        self,
        url: str,
        estacao: str = "",
        ativo: bool = True,
        timeout_segundos: float = 5.0,
        estabilidade_segundos: float = 3.0,
        intervalo_reenvio: float = 10.0,
        fila_maxima: int = 500,
        verboso: bool = True,
    ) -> None:
        self.url = url.rstrip("/")
        self.estacao = estacao
        self.ativo = bool(ativo and url)
        self.timeout = float(timeout_segundos)
        self.estabilidade = float(estabilidade_segundos)
        self.intervalo_reenvio = float(intervalo_reenvio)
        self.verboso = verboso

        self._fila: queue.Queue[Leitura] = queue.Queue(maxsize=fila_maxima)
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None

        # candidato = valor visto agora mas ainda nao maduro
        self._candidato: dict[int, tuple[tuple, float]] = {}
        # publicado = ultimo valor que ja saiu daqui
        self._publicado: dict[int, tuple] = {}

        self.enviados = 0
        self.falhas = 0
        self.ultimo_erro = ""

        if self.ativo:
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ #
    @classmethod
    def a_partir_dos_parametros(cls, parametros: dict, estacao: str = "") -> "PublicadorEstoque":
        cfg = parametros.get("backend", {}) or {}
        return cls(
            url=cfg.get("url", ""),
            estacao=estacao,
            ativo=bool(cfg.get("ativo", False)),
            timeout_segundos=float(cfg.get("timeout_segundos", 5.0)),
            estabilidade_segundos=float(cfg.get("estabilidade_segundos", 3.0)),
            intervalo_reenvio=float(cfg.get("intervalo_reenvio", 10.0)),
            fila_maxima=int(cfg.get("fila_maxima", 500)),
        )

    # ------------------------------------------------------------------ #
    def atualizar(
        self,
        dispenser: int,
        caixas: int | None,
        confianca: float,
        veredito: str,
        sku: str | None = None,
        medicamento: str | None = None,
        detalhe: str = "",
        unidades_vistas: int | None = None,
    ) -> None:
        """Recebe a leitura de um frame. Chame sempre; a filtragem e aqui.

        `confianca` deve ser a da CONTAGEM (NivelEstoque.confianca), nao a do
        veredito de identidade: e ela que diz se o numero de caixas merece
        virar estoque. A identidade viaja separada, no campo `veredito`, e o
        backend usa as duas para coisas diferentes.

        `unidades_vistas` e a segunda via de contagem: quantas etiquetas do
        medicamento certo aparecem na zona. Nao depende de calibracao de pilha
        e o backend usa como reserva quando `caixas` vem nulo.
        """
        if not self.ativo:
            return

        chave = (caixas, unidades_vistas, veredito, sku)
        agora = time.monotonic()

        anterior = self._candidato.get(dispenser)
        if anterior is None or anterior[0] != chave:
            self._candidato[dispenser] = (chave, agora)
            return

        if agora - anterior[1] < self.estabilidade:
            return                                  # ainda amadurecendo
        if self._publicado.get(dispenser) == chave:
            return                                  # ja publicado, nada mudou

        self._publicado[dispenser] = chave
        self._enfileirar(Leitura(
            dispenser=dispenser, sku=sku, medicamento=medicamento, caixas=caixas,
            confianca=float(confianca), veredito=veredito, detalhe=detalhe,
            momento=time.strftime("%Y-%m-%d %H:%M:%S"),
            unidades_vistas=unidades_vistas,
        ))

    # ------------------------------------------------------------------ #
    def _enfileirar(self, leitura: Leitura) -> None:
        try:
            self._fila.put_nowait(leitura)
        except queue.Full:
            # Fila cheia significa backend fora do ar ha muito tempo. Descartar
            # a leitura MAIS ANTIGA e o certo: o backend so precisa do nivel
            # atual de cada dispenser, e o historico completo ja esta em
            # eventos.db. Perder o valor novo, esse sim, deixaria o estoque
            # parado num numero velho quando a rede voltasse.
            try:
                self._fila.get_nowait()
                self._fila.put_nowait(leitura)
            except (queue.Empty, queue.Full):
                pass
            self.ultimo_erro = "fila cheia; leitura antiga descartada"

    # ------------------------------------------------------------------ #
    def _worker(self) -> None:
        pendentes: list[Leitura] = []
        while not self._parar.is_set():
            try:
                pendentes.append(self._fila.get(timeout=1.0))
            except queue.Empty:
                if not pendentes:
                    continue

            # drena o resto da fila: uma OS grande mexe em varios dispensers de
            # uma vez e um POST unico e melhor que N
            while len(pendentes) < 50:
                try:
                    pendentes.append(self._fila.get_nowait())
                except queue.Empty:
                    break

            if self._enviar(pendentes):
                self.enviados += len(pendentes)
                pendentes = []
            else:
                self.falhas += 1
                # nao devolve para a fila: segurar aqui preserva a ordem e evita
                # corrida com leituras novas entrando pelo outro lado
                self._parar.wait(self.intervalo_reenvio)

    def _enviar(self, leituras: list[Leitura]) -> bool:
        corpo = json.dumps({
            "estacao": self.estacao,
            "leituras": [l.para_dict() for l in leituras],
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self.url}/api/visao/estoque", data=corpo,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resposta = json.loads(resp.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                json.JSONDecodeError) as exc:
            self.ultimo_erro = str(exc)
            if self.verboso:
                print(f"  [backend] envio falhou ({exc}); {len(leituras)} leitura(s) na fila")
            return False

        if self.verboso:
            for r in resposta.get("resultados", []):
                acao = r.get("acao", "?")
                if acao in ("saida", "reposicao", "bloqueado", "erro"):
                    print(f"  [backend] D{r.get('dispenser')} {acao}: {r.get('detalhe', '')}")
        self.ultimo_erro = ""
        return True

    # ------------------------------------------------------------------ #
    def fechar(self) -> None:
        if not self.ativo:
            return
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
def buscar_catalogo(url: str, timeout: float = 5.0) -> list[dict] | None:
    """Le do backend qual medicamento e esperado em cada dispenser.

    O backend e a fonte de verdade desse mapeamento. Manter uma copia local
    autoritativa criaria duas respostas para "o que deveria estar no dispenser
    1", e a divergencia entre elas nao aparece como erro: aparece como veredito
    OK em cima do medicamento errado, com o estoque sendo debitado da linha
    errada. E o pior modo de falhar deste sistema, porque parece sucesso.

    Devolve None quando o backend nao responde — quem chama decide se cai para
    o arquivo local ou se para. A estacao precisa continuar funcionando com a
    rede caida, entao a decisao nao pode estar aqui.
    """
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/api/visao/catalogo",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            dados = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError):
        return None

    for item in dados.get("incompletos", []):
        print(f"  [backend] dispenser {item.get('dispenser')} "
              f"({item.get('nome')}) ignorado: {item.get('motivo')}")
    return dados.get("medicamentos") or None
