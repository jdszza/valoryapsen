#!/usr/bin/env python3
"""
Simulador Serial — Display Apsen
Envia comandos JSON pelo cabo USB e exibe respostas do display.

Faz DOIS papeis, e eles sao diferentes:

1. Empurra comandos de debug (`nova_ordem`, `dispenser`, `status`) no ritmo de
   INTERVALO — o uso de sempre, para encher a tela sem backend.
2. RESPONDE aos pedidos que o firmware faz por conta propria (`ping`,
   `get_ordens`, `get_dispensers`, `get_catalogo`, `get_operadores`,
   `validar_operador`, `set_status`, `sync_dispensers`, `get_trava`,
   `liberar_trava`). Sem isso o display fica OFFLINE e
   `fetch_ordens_api` nunca roda — que e justamente o caminho onde o os_id
   longo do computador central e o campo `origem` chegam.

A trava do Triple Check entra nos DOIS papeis: o ciclo de comandos empurra um
push `trava` (ativa, depois liberada) para exercitar a tela sem placa, e o
papel de backend responde `get_trava` e confere nome + PIN em `liberar_trava`
— com o PIN do SUPERVISOR, que e outra pessoa que nao o operador logado.

Por isso ORDENS_CENTRAL abaixo carrega os_id no formato real do central
({template_id}-{AAAAMMDDTHHMMSS}-{6 hex}) e cobre os dois valores de `origem`:
e este arquivo que exercita o protocolo sem hardware.

Instalar dependência (uma vez):
    pip install pyserial

Uso:
    python simulador_serial.py           # lista portas e usa COM_PORTA abaixo
    python simulador_serial.py COM4      # porta como argumento
    python simulador_serial.py --listar  # só lista as portas disponíveis
"""

import serial
import serial.tools.list_ports
import threading
import time
import json
import sys

# ============================================================
# CONFIGURAÇÃO — altere a porta conforme o seu sistema
#   Windows : "COM3", "COM4", "COM5" ...
#   Linux   : "/dev/ttyUSB0", "/dev/ttyACM0"
#   Mac     : "/dev/cu.usbserial-*"
# ============================================================
COM_PORTA  = "COM3"
BAUD_RATE  = 115200
INTERVALO  = 5.0   # segundos entre cada comando

# ============================================================
# Comandos simulados — edite à vontade
# ============================================================
COMANDOS = [
    # --- Ordens de expedição LOCAIS (nascem no painel, o operador aciona) ---
    {"cmd": "nova_ordem", "id": "OS-001", "origem": "local",
     "itens": "Paracetamol|10;Ibuprofeno|5",
     "destino": "UTI A", "lote": "LOT-2024-01"},

    {"cmd": "nova_ordem", "id": "OS-002", "origem": "local",
     "itens": "Amoxicilina|8;Dipirona|15",
     "destino": "Enfermaria B", "lote": "LOT-2024-02"},

    # Sem o campo `origem`: o firmware tem que assumir "local". E o contrato do
    # backend anterior a integracao, e nao e por falta do campo que uma ordem
    # deve virar so-leitura.
    {"cmd": "nova_ordem", "id": "OS-003",
     "itens": "Omeprazol|20;Metformina|6",
     "destino": "Ambulatorio", "lote": "LOT-2024-03"},

    # --- Ordens ESPELHADAS do computador central (so-leitura no painel) ---
    # os_id no formato real: {template_id}-{AAAAMMDDTHHMMSS}-{6 hex}. Este tem
    # 36 caracteres e nao cabia nos 16 bytes de antes.
    {"cmd": "nova_ordem", "id": "OS-INFECTO-01-20260909T143012-A1B2C3",
     "origem": "central", "status": "Pendente",
     "itens": "Amoxicilina|8;Dipirona|15;Omeprazol|4",
     "destino": "Kit Infectologia - leito 12"},

    # Mesmo template, disparo seguinte: os 13 primeiros caracteres sao iguais.
    # Truncado em 16 bytes, os dois viravam o MESMO id — e o set_status do
    # display ia para a ordem errada.
    {"cmd": "nova_ordem", "id": "OS-INFECTO-01-20260909T151145-9F0E7D",
     "origem": "central", "status": "Em Processo",
     "itens": "Amoxicilina|6;Paracetamol|10",
     "destino": "Kit Infectologia - leito 30"},

    # --- Atualização de dispensers (slot 1–8) ---
    {"cmd": "dispenser", "slot": 1, "nome": "Paracetamol",
     "quantidade": 45, "capacidade": 60, "minimo": 10},

    {"cmd": "dispenser", "slot": 2, "nome": "Ibuprofeno",
     "quantidade": 28, "capacidade": 60, "minimo": 8},

    {"cmd": "dispenser", "slot": 3, "nome": "Amoxicilina",
     "quantidade": 55, "capacidade": 60, "minimo": 12},

    {"cmd": "dispenser", "slot": 4, "nome": "Dipirona",
     "quantidade": 8, "capacidade": 60, "minimo": 10},

    {"cmd": "dispenser", "slot": 5, "nome": "Omeprazol",
     "quantidade": 40, "capacidade": 60, "minimo": 5},

    {"cmd": "dispenser", "slot": 6, "nome": "Metformina",
     "quantidade": 3, "capacidade": 60, "minimo": 10},

    {"cmd": "dispenser", "slot": 7, "nome": "Atenolol",
     "quantidade": 50, "capacidade": 60, "minimo": 8},

    {"cmd": "dispenser", "slot": 8, "nome": "Losartana",
     "quantidade": 60, "capacidade": 60, "minimo": 10},

    # --- Estados terminais empurrados pelo central (push, nao cmd) ---
    # "Erro" e "Cancelado" sao palavras NOVAS no vocabulario do painel: chegam
    # de OS que a celula abortou. Sem o ramo que as trata, a ordem ficaria
    # parada em "Aguardando"/"Separando" para sempre, e o operador esperaria
    # uma execucao que ja acabou. Nao vem por get_ordens — a fila do display so
    # traz Pendente/Em Processo —, so por push.
    {"push": "ordem_status",
     "numero_os": "OS-INFECTO-01-20260909T151145-9F0E7D", "status": "Erro"},

    {"push": "ordem_status",
     "numero_os": "OS-INFECTO-01-20260909T143012-A1B2C3", "status": "Cancelado"},

    # --- Trava do Triple Check (push, so na transicao) ---
    # O motivo vem no formato real do orquestrador e passa de 200 caracteres
    # de proposito: o firmware guarda em MAX_MOTIVO_LEN (256) e, quando nao
    # couber, `copy_trunc` termina em "..." — truncar de proposito e deixar
    # rastro, nunca cortar em silencio.
    {"push": "trava", "ativa": True,
     "os_id": "OS-INFECTO-01-20260909T151145-9F0E7D", "slot_id": 3,
     "motivo": ("Triple Check FALHOU (2/3 fontes divergentes, limiar=1) — D3: "
                "dispenser: dispensou 9 de 10 esperados; "
                "balanca: desvio=10.0%")},

    {"push": "trava", "ativa": False, "os_id": "", "slot_id": None, "motivo": ""},

    # --- Consulta de status ---
    {"cmd": "status"},
]

# ============================================================
# Respostas aos pedidos que o FIRMWARE faz (o papel de backend)
# ============================================================
# O display pergunta e espera `{"resp": "<tag>", ...}` de volta, com timeout.
# Sem resposta ao "ping" ele fica OFFLINE e nem chega a pedir as ordens — que
# e o caminho onde o os_id longo e o campo `origem` chegam de verdade.
#
# `origem` cobre os dois valores de proposito: a ordem local mantem os botoes
# Iniciar/Retomar, a espelhada nao tem botao nenhum. E quem esta em
# "Em Processo" precisa aparecer na lista: e a que a celula executa agora.
ORDENS_CENTRAL = [
    {"numero_os": "OS-INFECTO-01-20260909T143012-A1B2C3",
     "destino": "Kit Infectologia - leito 12",
     "status": "Pendente", "origem": "central",
     "itens_lista": [{"med": "Amoxicilina", "qtd": 8},
                     {"med": "Dipirona", "qtd": 15},
                     {"med": "Omeprazol", "qtd": 4}]},

    # Mesmo template, disparo seguinte: os 13 primeiros caracteres coincidem.
    # Truncados em 16 bytes, os dois ids viravam o mesmo — e o push de status
    # casava com a linha errada no strcmp do firmware.
    {"numero_os": "OS-INFECTO-01-20260909T151145-9F0E7D",
     "destino": "Kit Infectologia - leito 30",
     "status": "Em Processo", "origem": "central",
     "itens_lista": [{"med": "Amoxicilina", "qtd": 6},
                     {"med": "Paracetamol", "qtd": 10}]},

    # Ordem nascida no painel: `origem` local, botoes liberados.
    {"numero_os": "OS-LOCAL-77",
     "destino": "Ambulatorio",
     "status": "Pendente", "origem": "local",
     "itens_lista": [{"med": "Omeprazol", "qtd": 20},
                     {"med": "Metformina", "qtd": 6}]},

    # Sem o campo `origem`: o firmware tem que assumir "local".
    {"numero_os": "OS-LEGADO-08",
     "destino": "Pronto Socorro",
     "status": "Pendente",
     "itens_lista": [{"med": "Paracetamol", "qtd": 3}]},
]

DISPENSERS_RESP = [
    {"slot": c["slot"], "nome": c["nome"], "quantidade": c["quantidade"],
     "capacidade": c["capacidade"], "minimo": c["minimo"],
     "lote": f"LOT-{c['slot']:03d}", "validade": "2027-01-31"}
    for c in COMANDOS if c.get("cmd") == "dispenser"
]

CATALOGO_RESP = sorted({d["nome"] for d in DISPENSERS_RESP})

# Sem `pin`: a lista de operadores NAO carrega credencial nenhuma desde que o
# backend passou a conferir o PIN ele mesmo (`cmd: validar_operador`). Este
# arquivo e quem exercita o protocolo sem placa — deixar o campo aqui faria o
# firmware continuar compilando contra um contrato que o backend nao serve mais.
OPERADORES_RESP = [
    {"nome": "Operador Teste", "perfil": "Operador"},
    # O supervisor e quem libera a trava: outra pessoa, outro PIN. O display
    # lista os perfis Supervisor/Admin no popup de liberacao.
    {"nome": "Supervisora Teste", "perfil": "Supervisor"},
]

# PINs que este simulador aceita. Existem so aqui, no lado que faz o papel do
# backend: o display nao guarda PIN nenhum.
PIN_VALIDO = "1234"
PIN_SUPERVISOR = "4321"

# Estado da trava que `get_trava` devolve. Comeca ativa para que a tela de trava
# apareca no primeiro ciclo sem depender do push; `liberar_trava` com o PIN do
# supervisor a desliga, e o push do ciclo de comandos a religa.
TRAVA_SIM = {
    "ativa": True,
    "os_id": "OS-INFECTO-01-20260909T151145-9F0E7D",
    "slot_id": 3,
    "motivo": ("Triple Check FALHOU (2/3 fontes divergentes, limiar=1) — D3: "
               "dispenser: dispensou 9 de 10 esperados; balanca: desvio=10.0%"),
}

# cmd de leitura -> (tag do "resp", dados)
_RESPOSTAS_GET = {
    "get_ordens": ("ordens", ORDENS_CENTRAL),
    "get_dispensers": ("dispensers", DISPENSERS_RESP),
    "get_catalogo": ("catalogo", CATALOGO_RESP),
    "get_operadores": ("operadores", OPERADORES_RESP),
}


def _origem_da_ordem(numero_os):
    for o in ORDENS_CENTRAL:
        if o["numero_os"] == numero_os:
            return o.get("origem", "local")
    return "local"


def responder(msg):
    """Resposta ao pedido do display, ou None se a linha nao pedia nada.

    Espelha `_handle_serial_message` do backend no que o firmware observa —
    inclusive a RECUSA de escrita sobre ordem espelhada, que e o que os botoes
    desabilitados existem para nao deixar o operador descobrir por acidente.
    """
    cmd = msg.get("cmd")
    if cmd == "ping":
        return {"resp": "pong", "epoch": int(time.time())}
    if cmd in _RESPOSTAS_GET:
        tag, data = _RESPOSTAS_GET[cmd]
        return {"resp": tag, "data": data}
    if cmd == "validar_operador":
        if msg.get("pin", "") == PIN_VALIDO:
            return {"resp": "operador", "ok": True,
                    "nome": OPERADORES_RESP[0]["nome"],
                    "perfil": OPERADORES_RESP[0]["perfil"]}
        return {"resp": "operador", "ok": False}
    if cmd == "set_status":
        if _origem_da_ordem(msg.get("numero_os", "")) == "central":
            return {"resp": "ok", "ok": False, "msg": "ordem do central"}
        return {"resp": "ok", "ok": True}
    if cmd in ("sync_dispensers", "set_dispenser_med"):
        return {"resp": "ok", "ok": True}
    if cmd == "get_trava":
        return {"resp": "trava", **TRAVA_SIM, "ts": int(time.time())}
    if cmd == "liberar_trava":
        # Nome + PIN do SUPERVISOR. A resposta nunca ecoa o PIN.
        if (msg.get("nome", "") == OPERADORES_RESP[1]["nome"]
                and msg.get("pin", "") == PIN_SUPERVISOR):
            TRAVA_SIM.update({"ativa": False, "os_id": "", "slot_id": None, "motivo": ""})
            return {"resp": "ok", "ok": True, "msg": "trava liberada"}
        return {"resp": "ok", "ok": False, "msg": "nome ou PIN incorreto"}
    return None

# ============================================================
# Leitura em thread separada para não bloquear o envio
# ============================================================
def thread_leitura(ser: serial.Serial, parar: threading.Event):
    while not parar.is_set():
        try:
            linha = ser.readline().decode("utf-8", errors="ignore").strip()
            if not linha:
                continue
            if linha.startswith("{"):
                try:
                    obj = json.loads(linha)
                except json.JSONDecodeError:
                    print(f"  << (JSON inválido): {linha}")
                    continue
                print(f"  << {json.dumps(obj, ensure_ascii=False)}")
                # O display tambem PERGUNTA. Responder aqui, na propria thread
                # de leitura, e o que mantem o `serial_request` dele dentro do
                # timeout — ele fica em laco esperando a resposta chegar.
                resp = responder(obj)
                if resp is not None:
                    linha_resp = json.dumps(resp, ensure_ascii=False)
                    ser.write((linha_resp + "\n").encode("utf-8"))
                    print(f"  >> (resp) {linha_resp}")
            else:
                print(f"  [esp32] {linha}")
        except Exception:
            break


def listar_portas():
    portas = list(serial.tools.list_ports.comports())
    print("\nPortas seriais disponíveis:")
    if not portas:
        print("  (nenhuma encontrada)")
    for p in portas:
        print(f"  {p.device:12s}  {p.description}")
    print()


def main():
    # Tratar argumentos simples
    porta = COM_PORTA
    if len(sys.argv) > 1:
        if sys.argv[1] == "--listar":
            listar_portas()
            return
        porta = sys.argv[1]

    print("=" * 54)
    print("  Simulador Serial — Display Apsen")
    print(f"  Porta : {porta}   Baud : {BAUD_RATE}")
    print(f"  Intervalo entre comandos : {INTERVALO}s")
    print(f"  Total de comandos no ciclo : {len(COMANDOS)}")
    print("=" * 54)
    listar_portas()

    try:
        ser = serial.Serial(porta, BAUD_RATE, timeout=0.5)
    except serial.SerialException as e:
        print(f"ERRO: não foi possível abrir {porta}\n  {e}")
        print("  → Use 'python simulador_serial.py --listar' para ver as portas.")
        return

    print(f"Conectado em {porta}. Ctrl+C para parar.\n")
    time.sleep(1.5)  # aguarda o ESP32 reiniciar após abrir a porta

    parar = threading.Event()
    t = threading.Thread(target=thread_leitura, args=(ser, parar), daemon=True)
    t.start()

    try:
        ciclo = 0
        while True:
            for idx, cmd in enumerate(COMANDOS):
                linha = json.dumps(cmd, ensure_ascii=False)
                ts = time.strftime("%H:%M:%S")
                print(f"\n[{ts}] >> cmd {idx + 1}/{len(COMANDOS)}  (ciclo {ciclo + 1})")
                print(f"  >> {linha}")
                ser.write((linha + "\n").encode("utf-8"))
                # O push de trava tambem muda o que `get_trava` responde: os
                # dois papeis tem que contar a mesma historia, senao o display
                # desfaz o push no proximo poll.
                if cmd.get("push") == "trava":
                    TRAVA_SIM.update({k: cmd[k] for k in ("ativa", "os_id", "slot_id", "motivo")})
                time.sleep(INTERVALO)
            ciclo += 1
            print("\n--- Ciclo completo, reiniciando ---\n")

    except KeyboardInterrupt:
        print("\n\nEncerrado pelo usuário.")
    finally:
        parar.set()
        ser.close()


if __name__ == "__main__":
    main()
