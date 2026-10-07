"""Registro de eventos com trilha de auditoria encadeada.

Num sistema que aponta erro de medicamento, o log deixa de ser conveniencia e
vira prova. Se alguem contesta um alerta seis meses depois — ou pior, se alguem
apaga um alerta inconveniente — o registro precisa aguentar a pergunta
"como voce sabe que isso nao foi alterado?".

A resposta aqui e encadeamento por hash, a mesma ideia de um livro-razao: cada
evento carrega o hash do anterior. Alterar ou remover qualquer evento quebra a
cadeia de todos os seguintes, e `verificar_integridade()` aponta exatamente
onde. Nao impede a adulteracao — impede a adulteracao SILENCIOSA, que e o que
importa numa auditoria.

Isso conversa com o principio ALCOA+ de integridade de dados (atribuivel,
legivel, contemporaneo, original, exato) que a industria farmaceutica ja aplica
a registros eletronicos. Nao substitui uma validacao formal de sistema
computadorizado, mas e a base tecnica sobre a qual essa validacao se apoia.

O armazenamento e SQLite por padrao (zero infraestrutura, um arquivo) com o
mesmo esquema pensado para Postgres/Supabase quando virar multiplas estacoes.
"""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

RAIZ = Path(__file__).resolve().parent.parent
ARQ_BANCO = RAIZ / "dados" / "eventos.db"

GENESE = "0" * 64

ESQUEMA = """
CREATE TABLE IF NOT EXISTS eventos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    estacao      TEXT    NOT NULL,
    momento      TEXT    NOT NULL,
    tipo         TEXT    NOT NULL,
    dispenser    INTEGER,
    sku          TEXT,
    medicamento  TEXT,
    veredito     TEXT,
    confianca    REAL,
    detalhe      TEXT,
    dados        TEXT    NOT NULL DEFAULT '{}',
    hash_anterior TEXT   NOT NULL,
    hash         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eventos_momento   ON eventos(momento);
CREATE INDEX IF NOT EXISTS idx_eventos_tipo      ON eventos(tipo);
CREATE INDEX IF NOT EXISTS idx_eventos_dispenser ON eventos(dispenser);
CREATE INDEX IF NOT EXISTS idx_eventos_estacao   ON eventos(estacao);

CREATE TABLE IF NOT EXISTS estado_atual (
    estacao     TEXT NOT NULL,
    dispenser   INTEGER NOT NULL,
    momento     TEXT NOT NULL,
    veredito    TEXT,
    sku         TEXT,
    medicamento TEXT,
    confianca   REAL,
    caixas      INTEGER,
    fracao      REAL,
    unidades    INTEGER,
    precisa_repor INTEGER DEFAULT 0,
    PRIMARY KEY (estacao, dispenser)
);
"""


# --------------------------------------------------------------------------- #
@dataclass
class Evento:
    tipo: str
    dispenser: int | None = None
    sku: str | None = None
    medicamento: str | None = None
    veredito: str | None = None
    confianca: float | None = None
    detalhe: str = ""
    dados: dict[str, Any] = field(default_factory=dict)
    momento: str = ""
    estacao: str = ""
    id: int | None = None
    hash_anterior: str = ""
    hash: str = ""

    def para_dict(self) -> dict:
        return {
            "id": self.id,
            "estacao": self.estacao,
            "momento": self.momento,
            "tipo": self.tipo,
            "dispenser": self.dispenser,
            "sku": self.sku,
            "medicamento": self.medicamento,
            "veredito": self.veredito,
            "confianca": self.confianca,
            "detalhe": self.detalhe,
            "dados": self.dados,
            "hash_anterior": self.hash_anterior,
            "hash": self.hash,
        }


def calcular_hash(evento: Evento) -> str:
    """Hash do conteudo do evento + hash do anterior.

    A serializacao e canonica (chaves ordenadas, sem espacos) de proposito: o
    mesmo evento tem que produzir o mesmo hash em qualquer maquina, senao a
    verificacao vira loteria.
    """
    corpo = json.dumps(
        {
            "estacao": evento.estacao,
            "momento": evento.momento,
            "tipo": evento.tipo,
            "dispenser": evento.dispenser,
            "sku": evento.sku,
            "medicamento": evento.medicamento,
            "veredito": evento.veredito,
            "confianca": evento.confianca,
            "detalhe": evento.detalhe,
            "dados": evento.dados,
            "hash_anterior": evento.hash_anterior,
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    )
    return hashlib.sha256(corpo.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
class RegistroDeEventos:
    """Banco de eventos append-only com cadeia de hash."""

    def __init__(
        self,
        caminho: Path | str | None = None,
        estacao: str | None = None,
    ) -> None:
        # resolvido em tempo de chamada (e nao no default do argumento) para que
        # testes e instalacoes possam redirecionar o banco sem reimportar
        self.caminho = Path(caminho) if caminho is not None else ARQ_BANCO
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self.estacao = estacao or socket.gethostname()
        self._trava = threading.Lock()

        self._conexao = sqlite3.connect(str(self.caminho), check_same_thread=False)
        self._conexao.row_factory = sqlite3.Row
        self._conexao.executescript(ESQUEMA)
        self._migrar()
        # WAL: leitura do dashboard nao trava a escrita da visao
        self._conexao.execute("PRAGMA journal_mode=WAL")
        self._conexao.commit()

    def _migrar(self) -> None:
        """Acrescenta colunas novas a bancos criados por versoes anteriores.

        CREATE TABLE IF NOT EXISTS nao altera tabela existente, entao sem isto
        um banco antigo quebraria ao gravar a contagem de unidades.
        """
        existentes = {
            linha["name"]
            for linha in self._conexao.execute("PRAGMA table_info(estado_atual)")
        }
        for coluna, tipo in (("unidades", "INTEGER"),):
            if coluna not in existentes:
                self._conexao.execute(
                    f"ALTER TABLE estado_atual ADD COLUMN {coluna} {tipo}")
        self._conexao.commit()

    # ------------------------------------------------------------------ #
    def registrar(self, evento: Evento) -> Evento:
        with self._trava:
            evento.estacao = evento.estacao or self.estacao
            evento.momento = evento.momento or datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            )
            evento.hash_anterior = self._ultimo_hash()
            evento.hash = calcular_hash(evento)

            cur = self._conexao.execute(
                """INSERT INTO eventos
                   (estacao, momento, tipo, dispenser, sku, medicamento, veredito,
                    confianca, detalhe, dados, hash_anterior, hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (evento.estacao, evento.momento, evento.tipo, evento.dispenser,
                 evento.sku, evento.medicamento, evento.veredito, evento.confianca,
                 evento.detalhe,
                 json.dumps(evento.dados, ensure_ascii=False, default=str),
                 evento.hash_anterior, evento.hash),
            )
            evento.id = int(cur.lastrowid)
            self._conexao.commit()
            return evento

    def _ultimo_hash(self) -> str:
        linha = self._conexao.execute(
            "SELECT hash FROM eventos ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return linha["hash"] if linha else GENESE

    # ------------------------------------------------------------------ #
    def atualizar_estado(
        self,
        dispenser: int,
        veredito: str | None = None,
        sku: str | None = None,
        medicamento: str | None = None,
        confianca: float | None = None,
        caixas: int | None = None,
        fracao: float | None = None,
        unidades: int | None = None,
        precisa_repor: bool = False,
    ) -> None:
        """Foto do estado atual — o dashboard le daqui, sem varrer o historico."""
        with self._trava:
            self._conexao.execute(
                """INSERT INTO estado_atual
                   (estacao, dispenser, momento, veredito, sku, medicamento,
                    confianca, caixas, fracao, unidades, precisa_repor)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(estacao, dispenser) DO UPDATE SET
                     momento=excluded.momento, veredito=excluded.veredito,
                     sku=excluded.sku, medicamento=excluded.medicamento,
                     confianca=excluded.confianca, caixas=excluded.caixas,
                     fracao=excluded.fracao, unidades=excluded.unidades,
                     precisa_repor=excluded.precisa_repor""",
                (self.estacao, dispenser,
                 datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 veredito, sku, medicamento, confianca, caixas, fracao, unidades,
                 1 if precisa_repor else 0),
            )
            self._conexao.commit()

    # ------------------------------------------------------------------ #
    def listar(
        self,
        limite: int = 100,
        tipo: str | None = None,
        dispenser: int | None = None,
        desde: str | None = None,
        apenas_criticos: bool = False,
    ) -> list[dict]:
        sql = "SELECT * FROM eventos WHERE 1=1"
        params: list[Any] = []
        if tipo:
            sql += " AND tipo = ?"
            params.append(tipo)
        if dispenser is not None:
            sql += " AND dispenser = ?"
            params.append(dispenser)
        if desde:
            sql += " AND momento >= ?"
            params.append(desde)
        if apenas_criticos:
            sql += " AND veredito IN ('ERRO_POSICAO','DIVERGENCIA','NAO_CADASTRADO')"
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limite))

        linhas = self._conexao.execute(sql, params).fetchall()
        return [self._linha_para_dict(l) for l in linhas]

    def estado(self) -> list[dict]:
        linhas = self._conexao.execute(
            "SELECT * FROM estado_atual ORDER BY estacao, dispenser"
        ).fetchall()
        return [dict(l) for l in linhas]

    def resumo(self, horas: int = 24) -> dict:
        limite = _iso_horas_atras(horas)
        total = self._conexao.execute(
            "SELECT COUNT(*) c FROM eventos WHERE momento >= ?", (limite,)
        ).fetchone()["c"]
        criticos = self._conexao.execute(
            """SELECT COUNT(*) c FROM eventos WHERE momento >= ?
               AND veredito IN ('ERRO_POSICAO','DIVERGENCIA','NAO_CADASTRADO')""",
            (limite,),
        ).fetchone()["c"]
        por_tipo = {
            l["tipo"]: l["c"] for l in self._conexao.execute(
                "SELECT tipo, COUNT(*) c FROM eventos WHERE momento >= ? GROUP BY tipo",
                (limite,),
            ).fetchall()
        }
        return {
            "janela_horas": horas,
            "eventos": total,
            "criticos": criticos,
            "por_tipo": por_tipo,
            "estacoes": [
                l["estacao"] for l in self._conexao.execute(
                    "SELECT DISTINCT estacao FROM eventos"
                ).fetchall()
            ],
        }

    @staticmethod
    def _linha_para_dict(linha: sqlite3.Row) -> dict:
        d = dict(linha)
        try:
            d["dados"] = json.loads(d.get("dados") or "{}")
        except json.JSONDecodeError:
            d["dados"] = {}
        return d

    # ------------------------------------------------------------------ #
    def verificar_integridade(self) -> dict:
        """Recalcula a cadeia inteira e aponta o primeiro ponto adulterado."""
        linhas = self._conexao.execute("SELECT * FROM eventos ORDER BY id").fetchall()
        anterior = GENESE
        for linha in linhas:
            evento = Evento(
                tipo=linha["tipo"], dispenser=linha["dispenser"], sku=linha["sku"],
                medicamento=linha["medicamento"], veredito=linha["veredito"],
                confianca=linha["confianca"], detalhe=linha["detalhe"],
                dados=json.loads(linha["dados"] or "{}"),
                momento=linha["momento"], estacao=linha["estacao"],
                hash_anterior=linha["hash_anterior"],
            )
            if linha["hash_anterior"] != anterior:
                return {
                    "integra": False, "total": len(linhas), "id_problema": linha["id"],
                    "motivo": "encadeamento quebrado: o evento anterior foi removido "
                              "ou reordenado",
                }
            if calcular_hash(evento) != linha["hash"]:
                return {
                    "integra": False, "total": len(linhas), "id_problema": linha["id"],
                    "motivo": "conteudo alterado depois de gravado",
                }
            anterior = linha["hash"]
        return {"integra": True, "total": len(linhas), "id_problema": None,
                "motivo": "cadeia integra"}

    # ------------------------------------------------------------------ #
    def exportar_csv(self, destino: Path | str, limite: int = 100000) -> Path:
        import csv

        destino = Path(destino)
        destino.parent.mkdir(parents=True, exist_ok=True)
        eventos = self.listar(limite=limite)
        campos = ["id", "estacao", "momento", "tipo", "dispenser", "sku",
                  "medicamento", "veredito", "confianca", "detalhe", "hash"]
        with destino.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=campos)
            w.writeheader()
            for e in reversed(eventos):
                w.writerow({k: e.get(k) for k in campos})
        return destino

    def fechar(self) -> None:
        try:
            self._conexao.close()
        except sqlite3.Error:
            pass


# --------------------------------------------------------------------------- #
def _iso_horas_atras(horas: int) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat(
        timespec="seconds"
    )


def eventos_de_fusao(resultados: Iterable, niveis: dict | None = None) -> list[Evento]:
    """Converte vereditos de fusao em eventos prontos para registrar."""
    niveis = niveis or {}
    saida: list[Evento] = []
    for r in resultados:
        nivel = niveis.get(r.dispenser)
        saida.append(
            Evento(
                tipo="conferencia",
                dispenser=r.dispenser,
                sku=r.medicamento.qr if r.medicamento else None,
                medicamento=r.medicamento.nome if r.medicamento else None,
                veredito=r.veredito.value,
                confianca=r.confianca,
                detalhe=r.mensagem(),
                dados={
                    "fonte": r.fonte,
                    "esperado": r.esperado.nome if r.esperado else None,
                    "estoque": nivel.para_dict() if nivel else None,
                    "unidades_certas": getattr(r, "quantidade_certa", None),
                    "unidades_erradas": getattr(r, "quantidade_errada", None),
                    "itens": getattr(r, "itens", None),
                },
            )
        )
    return saida
