# ═══════════════════════════════════════════════════════════════════════════════
# APSEN — Makefile de operações
# Uso: make <target>   |   make help
#
# Os nomes de serviço aqui são os do docker-compose.yml. Este arquivo já
# apontou para `backend`, `sap_simulator` e `mosquitto`, que não existem desde
# a migração de MQTT para REST/HTTP.
# ═══════════════════════════════════════════════════════════════════════════════

.PHONY: up down restart build rebuild logs ps status test test-deps lint \
        log-central log-dashboard log-ihm log-order-gen \
        log-cnc-sim log-dispenser-sim log-vision-sim log-weight-sim \
        log-cnc-adapter log-dispenser-adapter log-vision-adapter \
        log-weight-adapter log-mysql \
        shell-central shell-mysql help

# ── Cores para output do terminal ─────────────────────────────────────────────
RESET  := \033[0m
BOLD   := \033[1m
CYAN   := \033[36m
GREEN  := \033[32m
YELLOW := \033[33m
RED    := \033[31m
MAGENTA:= \033[35m
BLUE   := \033[34m
WHITE  := \033[37m

LOG_LINES ?= 100

# ── Subir / Parar ─────────────────────────────────────────────────────────────

up:
	@printf "$(GREEN)$(BOLD)▶ Subindo todos os serviços APSEN...$(RESET)\n"
	@test -f .env || (printf "$(RED)Falta o .env — rode: cp .env.example .env$(RESET)\n" && exit 1)
	docker compose up -d
	@printf "$(GREEN)✅ Serviços ativos. Dashboard: http://localhost:8050 | IHM: http://localhost:8051$(RESET)\n"

down:
	@printf "$(RED)$(BOLD)■ Parando todos os serviços...$(RESET)\n"
	docker compose down

restart:
	@printf "$(YELLOW)$(BOLD)↺ Reiniciando todos os serviços...$(RESET)\n"
	docker compose down && docker compose up -d

build:
	@printf "$(CYAN)$(BOLD)🔨 Build incremental...$(RESET)\n"
	docker compose build

rebuild:
	@printf "$(CYAN)$(BOLD)🔨 Rebuild completo (sem cache + remove volumes)...$(RESET)\n"
	docker compose down -v && docker compose build --no-cache && docker compose up -d

# ── Testes e verificação ──────────────────────────────────────────────────────

test-deps:
	@printf "$(CYAN)$(BOLD)📦 Instalando dependências de teste...$(RESET)\n"
	pip install -r tests/requirements-dev.txt

test:
	@printf "$(CYAN)$(BOLD)🧪 Rodando a suíte (sem Docker, sem MySQL)...$(RESET)\n"
	pytest tests/

lint:
	@printf "$(CYAN)$(BOLD)🔎 pyflakes...$(RESET)\n"
	python -m pyflakes central-computer cnc_simulator dispenser_simulator \
		vision-simulator weight-simulator cnc-adapter dispenser-adapter \
		vision-adapter weight-adapter dashboard ihm_web order-generator tests

config:
	@printf "$(CYAN)$(BOLD)🔎 Validando docker-compose.yml...$(RESET)\n"
	docker compose config -q && printf "$(GREEN)compose OK$(RESET)\n"

# ── Status ────────────────────────────────────────────────────────────────────

ps:
	@printf "$(BOLD)━━━━ Containers APSEN ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose ps

status: ps
	@printf "\n$(BOLD)━━━━ Saúde dos serviços ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose ps --format "table {{.Service}}\t{{.Status}}"
	@printf "\n$(BOLD)━━━━ Estado da planta ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	@printf "  Fila:   $(CYAN)curl -s localhost:8000/api/v1/fila$(RESET)\n"
	@printf "  Estado: $(CYAN)curl -s localhost:8000/estado$(RESET)\n"
	@printf "  Trava:  $(CYAN)curl -s localhost:8000/api/v1/trava$(RESET)\n"

# ── Logs individuais ──────────────────────────────────────────────────────────

logs:
	@printf "$(BOLD)━━━━ Todos os logs (Ctrl+C para sair) ━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES)

log-central:
	@printf "$(GREEN)$(BOLD)━━━━ [CENTRAL] FastAPI + orquestrador :8000 ━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) central-computer

log-dashboard:
	@printf "$(BLUE)$(BOLD)━━━━ [DASHBOARD] Plotly Dash :8050 ━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) dashboard

log-ihm:
	@printf "$(MAGENTA)$(BOLD)━━━━ [IHM] Interface de Manutenção :8051 ━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) ihm_web

log-order-gen:
	@printf "$(YELLOW)$(BOLD)━━━━ [ORDER-GEN] Gerador de OS ━━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) order-generator

log-cnc-sim:
	@printf "$(CYAN)$(BOLD)━━━━ [CNC-SIM] Simulador CNC :8200 ━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) cnc-simulator

log-dispenser-sim:
	@printf "$(WHITE)$(BOLD)━━━━ [DISP-SIM] Simulador de Dispensers :8201 ━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) dispenser-simulator

log-vision-sim:
	@printf "$(CYAN)$(BOLD)━━━━ [VISION-SIM] Câmeras :8202 ━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) vision-simulator

log-weight-sim:
	@printf "$(YELLOW)$(BOLD)━━━━ [WEIGHT-SIM] Balança HX711 :8203 ━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) weight-simulator

log-cnc-adapter:
	@printf "$(CYAN)$(BOLD)━━━━ [CNC-ADAPTER] :8101 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) cnc-adapter

log-dispenser-adapter:
	@printf "$(WHITE)$(BOLD)━━━━ [DISP-ADAPTER] :8100 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) dispenser-adapter

log-vision-adapter:
	@printf "$(CYAN)$(BOLD)━━━━ [VISION-ADAPTER] :8102 ━━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) vision-adapter

log-weight-adapter:
	@printf "$(YELLOW)$(BOLD)━━━━ [WEIGHT-ADAPTER] :8103 ━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) weight-adapter

log-mysql:
	@printf "$(GREEN)$(BOLD)━━━━ [MYSQL] MySQL 8 :3306 ━━━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) mysql

# ── Atalhos de desenvolvimento ────────────────────────────────────────────────

shell-central:
	docker exec -it apsen-central bash

shell-mysql:
	@# Senha vem do .env — não fica no arquivo versionado.
	docker exec -it apsen-mysql sh -c 'mysql -u "$$MYSQL_USER" -p"$$MYSQL_PASSWORD" "$$MYSQL_DATABASE"'

# ── Help ──────────────────────────────────────────────────────────────────────

help:
	@printf "$(BOLD)APSEN — Comandos disponíveis$(RESET)\n\n"
	@printf "  $(GREEN)make up$(RESET)              — sobe todos os serviços (exige .env)\n"
	@printf "  $(RED)make down$(RESET)            — para todos os serviços\n"
	@printf "  $(YELLOW)make restart$(RESET)         — reinicia tudo\n"
	@printf "  $(CYAN)make rebuild$(RESET)         — rebuild completo sem cache\n"
	@printf "  $(BOLD)make ps$(RESET) / $(BOLD)status$(RESET)     — containers, saúde e atalhos de estado\n"
	@printf "\n$(BOLD)Verificação:$(RESET)\n"
	@printf "  $(BOLD)make test$(RESET)            — pytest tests/ (sem Docker, sem MySQL)\n"
	@printf "  $(BOLD)make test-deps$(RESET)       — instala tests/requirements-dev.txt\n"
	@printf "  $(BOLD)make lint$(RESET)            — pyflakes em todo o repositório\n"
	@printf "  $(BOLD)make config$(RESET)          — valida o docker-compose.yml\n"
	@printf "\n$(BOLD)Logs por serviço:$(RESET)\n"
	@printf "  $(BOLD)make log-central$(RESET)           — FastAPI + orquestrador :8000\n"
	@printf "  $(BOLD)make log-dashboard$(RESET)         — Dash :8050\n"
	@printf "  $(BOLD)make log-ihm$(RESET)               — IHM :8051\n"
	@printf "  $(BOLD)make log-order-gen$(RESET)         — gerador de OS\n"
	@printf "  $(BOLD)make log-cnc-sim$(RESET)           — simulador CNC :8200\n"
	@printf "  $(BOLD)make log-dispenser-sim$(RESET)     — simulador dispensers :8201\n"
	@printf "  $(BOLD)make log-vision-sim$(RESET)        — simulador câmeras :8202\n"
	@printf "  $(BOLD)make log-weight-sim$(RESET)        — simulador balança :8203\n"
	@printf "  $(BOLD)make log-{cnc,dispenser,vision,weight}-adapter$(RESET) — adapters\n"
	@printf "  $(BOLD)make log-mysql$(RESET)             — MySQL\n"
	@printf "\n$(BOLD)Extras:$(RESET)\n"
	@printf "  $(BOLD)make shell-central$(RESET)   — bash dentro do container do central\n"
	@printf "  $(BOLD)make shell-mysql$(RESET)     — mysql CLI\n"
	@printf "  $(BOLD)make logs LOG_LINES=200$(RESET) — todos os logs com N linhas\n"
