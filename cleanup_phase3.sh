#!/usr/bin/env bash
# ============================================================
# APSEN — Limpeza Fase 3: remove código MQTT legado
# Arquivos/pastas que NÃO fazem parte da arquitetura atual
# (não aparecem no docker-compose.yml v3.1)
#
# Execute na raiz do repositório:
#   bash cleanup_phase3.sh
# ============================================================
set -e
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

echo "=== APSEN Fase 3 — Limpeza MQTT ==="

# Diretórios legados
LEGACY=(
    "backend"         # antigo broker bridge MQTT → MySQL (substituído por central-computer/)
    "sap_simulator"   # antigo gerador de OS via MQTT (substituído por order-generator/)
    "mosquitto"       # configuração do broker Mosquitto (serviço removido do docker-compose)
)

for dir in "${LEGACY[@]}"; do
    if [ -d "$REPO_ROOT/$dir" ]; then
        echo "  Removendo: $dir/"
        rm -rf "$REPO_ROOT/$dir"
    else
        echo "  Já removido: $dir/"
    fi
done

# __pycache__ em todos os diretórios
echo "  Limpando __pycache__..."
find "$REPO_ROOT" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo ""
echo "=== Verificando referências MQTT restantes nos arquivos ativos ==="
ACTIVE_DIRS="central-computer dispenser-adapter cnc-adapter dispenser_simulator cnc_simulator order-generator dashboard ihm_web"
for d in $ACTIVE_DIRS; do
    if [ -d "$REPO_ROOT/$d" ]; then
        matches=$(grep -ril "mqtt\|mosquitto\|paho" "$REPO_ROOT/$d" 2>/dev/null || true)
        if [ -n "$matches" ]; then
            echo "  ATENÇÃO — MQTT encontrado em: $matches"
        fi
    fi
done

echo ""
echo "=== Concluído ==="
echo "Próximo passo: docker compose down -v && docker compose build --no-cache && docker compose up -d"
