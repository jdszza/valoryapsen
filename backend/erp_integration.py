"""
APSEN - Integração ERP / Gerador Automático de OS

Ao iniciar um novo lote (via MQTT apsen/lote ou POST /cmd/lote), este módulo:
  1. Cria automaticamente uma Ordem de Serviço no banco de dados
  2. (Opcional) Notifica um sistema ERP externo via POST JSON

Configuração via variáveis de ambiente no docker-compose.yml:
  ERP_ENABLED  = "true" | "false"          (padrão: false)
  ERP_URL      = URL base do ERP externo   (padrão: "")
  ERP_CENTRO   = Código do centro APSEN    (padrão: "APSEN-SP-01")
  ERP_TIMEOUT  = Timeout HTTP em segundos  (padrão: 3)

Exemplo de payload enviado ao ERP:
  POST {ERP_URL}/api/sap/registro
  {
    "documento": "ABERTURA_LOTE_PRODUCAO",
    "os_id": "OS-0042",
    "lote": "LOTE-001",
    "produto": "Comprimido 500mg",
    "meta": 5000,
    "centro": "APSEN-SP-01",
    "timestamp_evento": "2026-06-23T20:00:00+00:00"
  }
"""

import logging
import threading
from datetime import datetime, timezone

import requests

from config import settings
from database import criar_os, listar_os

logger = logging.getLogger(__name__)


# ── Payload ERP ────────────────────────────────────────────────────────────────

def _montar_payload_erp(lote_id: str, produto: str, meta: int, os_id: str) -> dict:
    """
    Monta o payload JSON enviado ao sistema ERP.
    Formato inspirado em transação de movimento de estoque (MIGO/SAP simplificado).
    """
    return {
        "documento": "ABERTURA_LOTE_PRODUCAO",
        "os_id": os_id,
        "lote": lote_id,
        "produto": produto,
        "meta": meta,
        "centro": settings.ERP_CENTRO,
        "timestamp_evento": datetime.now(timezone.utc).isoformat(),
    }


def notificar_erp(lote_id: str, produto: str, meta: int, os_id: str) -> dict:
    """
    Envia o payload ao endpoint ERP configurado.

    Retorna dict com status da operação:
      {"status": "desabilitado"}                     — ERP_ENABLED=false
      {"status": "ok", "protocolo_sap": "..."}       — sucesso
      {"status": "erro_comunicacao", "mensagem": ""} — falha HTTP
    """
    if not settings.ERP_ENABLED or not settings.ERP_URL:
        return {"status": "desabilitado"}

    payload = _montar_payload_erp(lote_id, produto, meta, os_id)
    try:
        resp = requests.post(
            f"{settings.ERP_URL}/api/sap/registro",
            json=payload,
            timeout=settings.ERP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        data["payload_enviado"] = payload
        logger.info(f"[ERP] OS {os_id} notificada com sucesso: {data.get('protocolo_sap', '')}")
        return {"status": "ok", **data}
    except requests.RequestException as exc:
        logger.warning(f"[ERP] ERP indisponível para OS {os_id}: {exc}")
        return {
            "status": "erro_comunicacao",
            "mensagem": str(exc),
            "payload_enviado": payload,
        }


# ── Geração automática de OS ───────────────────────────────────────────────────

def _ja_existe_os_para_lote(lote_id: str) -> bool:
    """Retorna True se já existe uma OS aberta ou em andamento para este lote_id."""
    ordens = listar_os()
    return any(
        o["lote_id"] == lote_id and o["status"] in ("aberto", "em_andamento")
        for o in ordens
    )


def processar_novo_lote(
    lote_id: str,
    produto: str,
    meta: int,
    criado_por: str = "sistema",
) -> dict:
    """
    Orquestra a abertura de um novo lote:
      1. Verifica se já existe OS para evitar duplicatas
      2. Cria a OS automaticamente no banco de dados
      3. Notifica o ERP (se ERP_ENABLED=true)

    Retorna um dict com o resultado de cada etapa.
    Projetado para rodar em thread de background — não usa async/await.
    """
    try:
        if _ja_existe_os_para_lote(lote_id):
            logger.info(f"[OS-AUTO] Lote {lote_id} já tem OS ativa — criação ignorada.")
            return {"status": "ja_existe", "lote_id": lote_id}

        result = criar_os(
            produto=produto,
            lote_id=lote_id,
            meta=meta,
            responsavel=criado_por,
            criado_por=criado_por,
        )
        os_id = result["os_id"]
        logger.info(f"[OS-AUTO] OS {os_id} criada automaticamente para lote {lote_id} "
                    f"(produto={produto}, meta={meta})")

        erp_result = notificar_erp(lote_id, produto, meta, os_id)
        return {
            "status": "criado",
            "os_id": os_id,
            "lote_id": lote_id,
            "erp": erp_result,
        }

    except Exception as exc:
        logger.error(f"[OS-AUTO] Erro ao processar lote {lote_id}: {exc}")
        return {"status": "erro", "mensagem": str(exc)}


def processar_novo_lote_bg(
    lote_id: str,
    produto: str,
    meta: int,
    criado_por: str = "sistema",
) -> None:
    """
    Variante não-bloqueante: dispara processar_novo_lote em uma daemon thread.
    Usar a partir de callbacks MQTT ou de endpoints síncronos do FastAPI.
    """
    threading.Thread(
        target=processar_novo_lote,
        args=(lote_id, produto, meta, criado_por),
        daemon=True,
        name=f"os-auto-{lote_id}",
    ).start()
