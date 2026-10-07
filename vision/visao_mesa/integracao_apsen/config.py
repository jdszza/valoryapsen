"""Configuracao da camada de integracao, por variavel de ambiente ou .env.

Tudo que muda de uma bancada para outra — IP do adapter, porta, limites de
tempo — mora aqui e so aqui. Nada disso entra em config/mesa.json: aquele
arquivo e da VISAO (limiares, camera, escala) e e editado pelo calibrar.py; se
o endereco do PC central morasse junto, salvar uma calibragem reescreveria a
configuracao de rede sem ninguem perceber.

Precedencia: variavel de ambiente real > arquivo .env > padrao. O ambiente
ganha do arquivo de proposito, para dar para subir uma segunda instancia de
teste em outra porta sem editar (e depois esquecer de reverter) o .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
ARQ_ENV = RAIZ / ".env"


def carregar_env(caminho: Path = ARQ_ENV) -> None:
    """Le um .env simples (CHAVE=valor) sem depender de python-dotenv.

    Nao sobrescreve o que ja existe no ambiente — ver a precedencia acima.
    """
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave, valor = chave.strip(), valor.strip().strip('"').strip("'")
        os.environ.setdefault(chave, valor)


def _texto(nome: str, padrao: str) -> str:
    return os.environ.get(nome, padrao).strip()


def _inteiro(nome: str, padrao: int) -> int:
    try:
        return int(float(_texto(nome, str(padrao))))
    except ValueError:
        return padrao


def _decimal(nome: str, padrao: float) -> float:
    try:
        return float(_texto(nome, str(padrao)).replace(",", "."))
    except ValueError:
        return padrao


def _ligado(nome: str, padrao: bool = False) -> bool:
    return _texto(nome, "1" if padrao else "0").lower() in ("1", "true", "sim", "yes")


@dataclass
class ConfigIntegracao:
    # Onde fica o vision-adapter. O default e o IP de exemplo do documento de
    # integracao: e PLACEHOLDER, tem de ser conferido na bancada.
    adapter_url: str = "http://192.168.0.10:8102"
    host: str = "0.0.0.0"
    porta: int = 8202
    num_slots: int = 8

    # Abaixo disso a contagem nao vira numero: vira evento de falha. Nao vira
    # divergencia — divergencia trava a OS e chama supervisor, entao so se
    # manda quando a estacao tem certeza.
    confianca_minima: float = 0.60

    # As caixinhas ainda estao quicando quando o comando chega.
    t_assentamento_s: float = 0.5
    t_max_processamento_s: float = 20.0

    # Quantos frames entram no voto de maioria. Impar de proposito: com par,
    # 2 a 2 nao tem vencedor e a leitura inteira seria descartada por empate.
    frames_por_captura: int = 5

    # Reproduzir a injecao de falha do simulador fica DESLIGADO por padrao: numa
    # estacao real, um campo de demonstracao que chega por HTTP nao pode mudar
    # o que a camera afirma ter visto.
    aceitar_injecao: bool = False

    # Quantas OS ficam na memoria do acumulado, e por quanto tempo.
    max_os_memoria: int = 50
    validade_os_h: float = 2.0

    arquivo_log: str = ""          # vazio = so console
    imagem_fixa: str = ""          # bancada sem CNC: repete uma foto (ver README)

    cabecalhos: dict = field(default_factory=lambda: {"Content-Type": "application/json"})

    @classmethod
    def carregar(cls) -> "ConfigIntegracao":
        carregar_env()
        return cls(
            adapter_url=_texto("ADAPTER_URL", "http://192.168.0.10:8102").rstrip("/"),
            host=_texto("HOST", "0.0.0.0"),
            porta=_inteiro("PORTA", 8202),
            num_slots=_inteiro("NUM_SLOTS", 8),
            confianca_minima=_decimal("CONFIANCA_MINIMA", 0.60),
            t_assentamento_s=_decimal("T_ASSENTAMENTO_S", 0.5),
            t_max_processamento_s=_decimal("T_MAX_PROCESSAMENTO_S", 20.0),
            frames_por_captura=max(1, _inteiro("FRAMES_POR_CAPTURA", 5)),
            aceitar_injecao=_ligado("ACEITAR_INJECAO", False),
            max_os_memoria=_inteiro("MAX_OS_MEMORIA", 50),
            validade_os_h=_decimal("VALIDADE_OS_H", 2.0),
            arquivo_log=_texto("ARQUIVO_LOG", ""),
            imagem_fixa=_texto("IMAGEM_FIXA", ""),
        )

    @property
    def url_eventos(self) -> str:
        return f"{self.adapter_url}/eventos"

    def resumo(self) -> str:
        return (f"adapter={self.adapter_url}  escutando={self.host}:{self.porta}  "
                f"slots=1..{self.num_slots}  confianca_minima={self.confianca_minima:.2f}  "
                f"assentamento={self.t_assentamento_s}s  "
                f"teto_processamento={self.t_max_processamento_s}s  "
                f"frames={self.frames_por_captura}  "
                f"aceitar_injecao={'SIM' if self.aceitar_injecao else 'nao'}")
