"""Snapshots de `_estado`: quem lê fora do lock precisa de cópia PROFUNDA.

`get_estado()` e o handler do WebSocket faziam `dict(_estado)`. A cópia rasa
duplica só o primeiro nível — `dispensers`, `cnc`, `visao`, `peso` e `trava`
continuam sendo os dicionários vivos do central. A serialização (FastAPI ou
`json.dumps`) acontece DEPOIS de soltar o lock, então um evento de dispenser
chegando nesse intervalo altera a estrutura no meio da iteração:
`RuntimeError: dictionary changed size during iteration`.

`_broadcast_estado()` já usava `copy.deepcopy` sob o lock; estes testes cobram
o mesmo dos outros dois pontos.
"""
import asyncio
import json


def test_get_estado_devolve_snapshot_independente(carregar_central):
    """Mutação posterior do estado global não pode alterar o que já foi devolvido."""
    central = carregar_central()
    central.slot(1).update({"medicamento": "Dipirona", "quantidade": 10})

    snap = central.modulo.get_estado()

    central.slot(1).update({"medicamento": "Amoxicilina", "quantidade": 0})
    central.modulo._estado["cnc"]["status"] = "movendo"
    central.modulo._estado["trava"]["ativa"] = True

    assert snap["dispensers"]["1"]["medicamento"] == "Dipirona"
    assert snap["dispensers"]["1"]["quantidade"] == 10
    assert snap["cnc"]["status"] != "movendo"
    assert snap["trava"]["ativa"] is False


def test_snapshot_nao_compartilha_objetos_aninhados(carregar_central):
    """Identidade, não só valor: nenhum nível pode ser o objeto vivo."""
    central = carregar_central()

    snap = central.modulo.get_estado()

    for chave in ("dispensers", "cnc", "visao", "peso", "trava"):
        assert snap[chave] is not central.modulo._estado[chave], (
            f"snapshot['{chave}'] ainda é o objeto vivo do central"
        )
    assert snap["dispensers"]["1"] is not central.modulo._estado["dispensers"]["1"]


class Marcador:
    """Valor que o json não sabe serializar — obriga o `default` a rodar."""


def _serializar_como_fastapi(estrutura, ao_encontrar_marcador):
    """Serializa iterando em Python, que é como o FastAPI monta a resposta.

    O encoder em C do `json.dumps` percorre o dicionário sem checar mudança de
    tamanho: ele não levanta, mas pode devolver snapshot inconsistente. Já o
    caminho Python — `JSONEncoder.iterencode`, e também o `jsonable_encoder`
    que o FastAPI usa no retorno do endpoint — itera `dict.items()` e levanta
    `RuntimeError` na hora. É esse o caminho do `GET /estado`.
    """
    encoder = json.encoder.JSONEncoder(default=ao_encontrar_marcador)
    return "".join(encoder.iterencode(estrutura))


def test_serializacao_do_snapshot_sobrevive_a_evento_concorrente(carregar_central):
    """O erro real: dicionário mudando de tamanho durante a serialização.

    Reproduz a corrida sem thread nenhuma. O `default` só é chamado no meio da
    varredura de `dispensers`; mexer no estado global a partir dali equivale a
    um POST /eventos/dispenser chegando entre a soltura do lock e o fim da
    serialização.

    O teste também exercita a cópia rasa antiga, para provar que não está
    passando à toa: com `dict()` o mesmo cenário levanta RuntimeError.
    """
    import pytest

    central = carregar_central()
    estado = central.modulo._estado
    estado["dispensers"]["1"]["marcador"] = Marcador()

    def _evento_no_meio(_obj):
        estado["dispensers"][f"novo{len(estado['dispensers'])}"] = {"status": "idle"}
        return "—"

    # Como era: o snapshot raso compartilha o dicionário `dispensers`.
    with pytest.raises(RuntimeError, match="changed size during iteration"):
        _serializar_como_fastapi(dict(estado), _evento_no_meio)

    # Como é: deepcopy sob o lock, serialização sobre a cópia.
    snap = central.modulo.get_estado()
    _serializar_como_fastapi(snap, _evento_no_meio)      # não pode levantar


def test_websocket_envia_snapshot_profundo(carregar_central):
    """Mesmo contrato no `/ws`, que serializa fora do lock com json.dumps."""
    central = carregar_central()
    central.slot(2).update({"medicamento": "Dipirona"})

    class WSFake:
        def __init__(self):
            self.enviados = []

        async def accept(self):
            pass

        async def send_text(self, msg):
            # Enquanto a mensagem já está pronta, o estado global segue mudando.
            central.slot(2)["medicamento"] = "Outro"
            self.enviados.append(msg)

        async def receive_text(self):
            raise central.modulo.WebSocketDisconnect()

    ws = WSFake()
    asyncio.run(central.modulo.ws_endpoint(ws))

    (mensagem,) = ws.enviados
    assert json.loads(mensagem)["dispensers"]["2"]["medicamento"] == "Dipirona"
