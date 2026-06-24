# ═══════════════════════════════════════════════════════════════════════════════
# APSEN — Makefile de operações
# Uso: make <target>
# ═══════════════════════════════════════════════════════════════════════════════

.PHONY: up down restart build rebuild logs \
        log-backend log-dashboard log-ihm log-sap log-cnc log-dispenser \
        log-mqtt log-mysql ps status

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

# ── Subir / Parar ─────────────────────────────────────────────────────────────

up:
	@printf "$(GREEN)$(BOLD)▶ Subindo todos os serviços APSEN...$(RESET)\n"
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

# ── Status ────────────────────────────────────────────────────────────────────

ps:
	@printf "$(BOLD)━━━━ Containers APSEN ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose ps

status: ps
	@printf "\n$(BOLD)━━━━ Tópicos MQTT (últimas mensagens) ━━━━━━━━━━━━━━━━━━$(RESET)\n"
	@printf "  Use: $(CYAN)mosquitto_sub -h localhost -t 'apsen/#' -v$(RESET)\n"

# ── Logs individuais ──────────────────────────────────────────────────────────

LOG_LINES ?= 100

logs:
	@printf "$(BOLD)━━━━ Todos os logs (Ctrl+C para sair) ━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES)

log-backend:
	@printf "$(GREEN)$(BOLD)━━━━ [BACKEND] FastAPI :8000 ━━━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) backend

log-dashboard:
	@printf "$(BLUE)$(BOLD)━━━━ [DASHBOARD] Plotly Dash :8050 ━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) dashboard

log-ihm:
	@printf "$(MAGENTA)$(BOLD)━━━━ [IHM] Interface de Manutenção :8051 ━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) ihm_web

log-sap:
	@printf "$(YELLOW)$(BOLD)━━━━ [SAP-SIM] Simulador de Ordens de Saída ━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) sap_simulator

log-cnc:
	@printf "$(CYAN)$(BOLD)━━━━ [CNC-SIM] Simulador CNC ━━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) cnc_simulator

log-dispenser:
	@printf "$(WHITE)$(BOLD)━━━━ [DISP-SIM] Simulador de Dispensers ━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) dispenser_simulator

log-mqtt:
	@printf "$(RED)$(BOLD)━━━━ [MQTT] Eclipse Mosquitto :1883 ━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) mosquitto

log-mysql:
	@printf "$(GREEN)$(BOLD)━━━━ [MYSQL] MySQL 8 :3306 ━━━━━━━━━━━━━━━━━━━━━━━━━━━$(RESET)\n"
	docker compose logs -f --tail=$(LOG_LINES) mysql

# ── Atalhos de desenvolvimento ────────────────────────────────────────────────

mqtt-listen:
	@printf "$(BOLD)Escutando todos os tópicos APSEN (Ctrl+C para sair)...$(RESET)\n"
	mosquitto_sub -h localhost -t "apsen/#" -v

shell-backend:
	docker exec -it apsen-backend bash

shell-mysql:
	docker exec -it apsen-mysql mysql -u apsen -papsen_pass_2024 apsen_db

# ── Help ──────────────────────────────────────────────────────────────────────

help:
	@printf "$(BOLD)APSEN — Comandos disponíveis$(RESET)\n\n"
	@printf "  $(GREEN)make up$(RESET)              — sobe todos os serviços\n"
	@printf "  $(RED)make down$(RESET)            — para todos os serviços\n"
	@printf "  $(YELLOW)make restart$(RESET)         — reinicia tudo\n"
	@printf "  $(CYAN)make rebuild$(RESET)         — rebuild completo sem cache\n"
	@printf "  $(BOLD)make ps$(RESET)              — lista containers e status\n"
	@printf "\n$(BOLD)Logs por serviço:$(RESET)\n"
	@printf "  $(BOLD)make log-backend$(RESET)     — FastAPI :8000\n"
	@printf "  $(BOLD)make log-dashboard$(RESET)   — Dash :8050\n"
	@printf "  $(BOLD)make log-ihm$(RESET)         — IHM :8051\n"
	@printf "  $(BOLD)make log-sap$(RESET)         — SAP Simulator\n"
	@printf "  $(BOLD)make log-cnc$(RESET)         — CNC Simulator\n"
	@printf "  $(BOLD)make log-dispenser$(RESET)   — Dispenser Simulator\n"
	@printf "  $(BOLD)make log-mqtt$(RESET)        — Mosquitto MQTT\n"
	@printf "  $(BOLD)make log-mysql$(RESET)       — MySQL\n"
	@printf "\n$(BOLD)Extras:$(RESET)\n"
	@printf "  $(BOLD)make mqtt-listen$(RESET)     — escuta apsen/# em tempo real\n"
	@printf "  $(BOLD)make shell-backend$(RESET)   — bash dentro do container backend\n"
	@printf "  $(BOLD)make shell-mysql$(RESET)     — mysql CLI\n"
	@printf "  $(BOLD)make logs LOG_LINES=200$(RESET) — todos os logs com N linhas\n"
