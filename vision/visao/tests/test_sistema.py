"""Testes automatizados, sem camera. Rode:  python tests/test_sistema.py"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
sys.path.insert(0, str(RAIZ / "tests"))

import cv2  # noqa: E402

from cena_sintetica import montar_cena, zonas_padrao  # noqa: E402
from degradacao import gerar_cenario  # noqa: E402
from configuracao import Catalogo, MapaZonas, Zona  # noqa: E402
from detector import DetectorDispensers, Estado  # noqa: E402
from leitor_qr import LeitorCodigos, backend_disponivel  # noqa: E402

FALHAS: list[str] = []


def checar(condicao: bool, descricao: str) -> None:
    marca = "PASS" if condicao else "FALHA"
    print(f"  [{marca}] {descricao}")
    if not condicao:
        FALHAS.append(descricao)


def _detector(catalogo, mapa, **kw) -> DetectorDispensers:
    padrao = dict(frames_para_confirmar=1, cooldown_segundos=0.0, memoria_segundos=0.0)
    leitor = kw.pop("leitor", None) or LeitorCodigos()
    padrao.update(kw)
    return DetectorDispensers(catalogo=catalogo, zonas=mapa, leitor=leitor, **padrao)


# --------------------------------------------------------------------------- #
def teste_leitura_basica(catalogo, mapa):
    print("\n1) Leitura dos 4 QR codes na cena")
    frame = montar_cena(catalogo, mapa)
    leituras = LeitorCodigos(usar_aruco=False).ler(frame)
    checar(len(leituras) == 4, f"4 QR codes lidos (lidos: {len(leituras)})")
    conteudos = {l.conteudo for l in leituras}
    esperados = {m.qr for m in catalogo.medicamentos}
    checar(conteudos == esperados, f"conteudos corretos: {sorted(conteudos)}")


def teste_cena_correta(catalogo, mapa):
    print("\n2) Cena correta -> nenhum erro")
    det = _detector(catalogo, mapa)
    res = det.processar_frame(montar_cena(catalogo, mapa))
    checar(len(res.ocorrencias) == 4, f"4 ocorrencias ({len(res.ocorrencias)})")
    checar(all(o.estado is Estado.OK for o in res.ocorrencias), "todas OK")
    checar(not res.ha_erro, "nenhum alerta disparado")


def teste_troca_1_e_3(catalogo, mapa):
    print("\n3) Medicamento do dispenser 1 colocado no 3 (e vice-versa)")
    conteudo = {m.dispenser: m.qr for m in catalogo.por_qr.values()}
    conteudo[1], conteudo[3] = conteudo[3], conteudo[1]

    det = _detector(catalogo, mapa)
    res = det.processar_frame(montar_cena(catalogo, mapa, conteudo))

    erros = [o for o in res.ocorrencias if o.estado is Estado.ERRO_POSICAO]
    checar(len(erros) == 2, f"2 erros de posicao detectados ({len(erros)})")

    pares = {(o.dispenser_detectado, o.dispenser_esperado) for o in erros}
    checar(pares == {(1, 3), (3, 1)}, f"pares detectado->esperado corretos: {sorted(pares)}")

    ok = [o for o in res.ocorrencias if o.estado is Estado.OK]
    checar({o.dispenser_detectado for o in ok} == {2, 4}, "dispensers 2 e 4 seguem OK")
    checar(res.ha_erro and len(res.alertas_novos) == 2, "2 alertas novos emitidos")
    for o in erros:
        print(f"      -> {o.mensagem()}")


def teste_qr_desconhecido(catalogo, mapa):
    print("\n4) QR nao cadastrado")
    conteudo = {m.dispenser: m.qr for m in catalogo.por_qr.values()}
    conteudo[2] = "MED-999-NAO-CADASTRADO"
    det = _detector(catalogo, mapa)
    res = det.processar_frame(montar_cena(catalogo, mapa, conteudo))
    desconhecidos = [o for o in res.ocorrencias if o.estado is Estado.QR_DESCONHECIDO]
    checar(len(desconhecidos) == 1, "1 QR desconhecido detectado")
    checar(desconhecidos and desconhecidos[0].dispenser_detectado == 2,
           "localizado no dispenser 2")


def teste_debounce_e_cooldown(catalogo, mapa):
    print("\n5) Estabilidade: debounce de 5 frames e cooldown")
    conteudo = {m.dispenser: m.qr for m in catalogo.por_qr.values()}
    conteudo[1], conteudo[3] = conteudo[3], conteudo[1]
    frame = montar_cena(catalogo, mapa, conteudo)

    relogio = [0.0]
    det = DetectorDispensers(
        catalogo=catalogo, zonas=mapa, leitor=LeitorCodigos(),
        frames_para_confirmar=5, cooldown_segundos=10.0, memoria_segundos=0.0,
        relogio=lambda: relogio[0],
    )

    disparos = []
    for i in range(1, 9):
        res = det.processar_frame(frame)
        disparos.append(len(res.alertas_novos))
        relogio[0] += 0.05  # ~20 FPS

    checar(sum(disparos[:4]) == 0, "nenhum alerta nas 4 primeiras leituras (debounce)")
    checar(disparos[4] == 2, f"2 alertas exatamente no 5o frame (foi {disparos[4]})")
    checar(sum(disparos[5:]) == 0, "sem repeticao durante o cooldown")

    relogio[0] += 11.0  # passa o cooldown
    res = det.processar_frame(frame)
    checar(len(res.alertas_novos) == 2, "alerta reemitido depois do cooldown")


def teste_fora_de_zona(catalogo, mapa):
    print("\n6) QR visivel fora de qualquer dispenser")
    det = _detector(catalogo, mapa, alertar_fora_de_zona=True)
    # desloca todos os QRs para bem longe das zonas (canto superior)
    frame = montar_cena(catalogo, mapa, deslocamento=(0, -260))
    res = det.processar_frame(frame)
    fora = [o for o in res.ocorrencias if o.estado is Estado.FORA_DE_ZONA]
    checar(len(fora) >= 1, f"pelo menos 1 QR fora de zona ({len(fora)})")


def teste_reescala_resolucao(catalogo, mapa):
    print("\n7) Zonas se adaptam quando a resolucao da camera muda")
    frame = montar_cena(catalogo, mapa)
    metade = cv2.resize(frame, None, fx=0.5, fy=0.5)
    det = _detector(catalogo, mapa)
    res = det.processar_frame(metade)
    checar(res.zonas is not None and res.zonas.resolucao == (metade.shape[1], metade.shape[0]),
           "zonas reescaladas para a nova resolucao")
    checar(all(o.estado is Estado.OK for o in res.ocorrencias) and res.ocorrencias,
           "cena continua correta em metade da resolucao")


def teste_validacao_config(catalogo, mapa):
    print("\n8) Validacao das configuracoes")
    checar(mapa.validar(catalogo) == [], "zonas da cena passam na validacao")

    ruim = MapaZonas(zonas=[Zona(1, 0, 0, 200, 200), Zona(2, 100, 100, 200, 200)],
                     resolucao=(1280, 720))
    avisos = ruim.validar(catalogo)
    checar(any("sobrepoem" in a for a in avisos), "sobreposicao de zonas e detectada")
    checar(any("Sem zona calibrada" in a for a in avisos), "dispensers sem zona sao apontados")

    import json, tempfile  # noqa: E401
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"medicamentos": [
            {"qr": "A", "nome": "A", "dispenser": 1},
            {"qr": "B", "nome": "B", "dispenser": 1},
        ]}, f)
        caminho = f.name
    try:
        Catalogo.carregar(caminho)
        checar(False, "catalogo com 2 medicamentos no mesmo dispenser deve falhar")
    except ValueError:
        checar(True, "catalogo com 2 medicamentos no mesmo dispenser e rejeitado")


def teste_aruco(catalogo, mapa):
    print("\n9) Leitura pelo marcador ArUco (etiqueta hibrida)")
    frame = montar_cena(catalogo, mapa, etiqueta_completa=True)

    # varredura sem zonas: le QR e ArUco de tudo que estiver no frame
    leituras = LeitorCodigos(usar_aruco=True).ler(frame)
    arucos = [l for l in leituras if l.tipo == "aruco"]
    # cada etiqueta traz 2 marcadores (cantos opostos) -> 8 leituras, 4 ids
    checar(len(arucos) == 8, f"8 marcadores ArUco lidos, 2 por etiqueta ({len(arucos)})")
    ids = {l.conteudo for l in arucos}
    esperados = {f"ARUCO:{m.aruco}" for m in catalogo.medicamentos}
    checar(ids == esperados, f"ids ArUco corretos: {sorted(ids)}")
    checar(all(l.lado >= 16 for l in arucos), "nenhum falso positivo minusculo")

    # Na busca por zona o ArUco e somado ao QR de proposito: sem isso, uma
    # caixa com o QR tapado ficaria invisivel enquanto a vizinha e lida, e a
    # contagem de unidades sairia menor que a realidade.
    from leitor_qr import agrupar_em_unidades

    por_zona = LeitorCodigos(usar_aruco=True).ler(frame, zonas=mapa)
    tipos = {l.tipo for l in por_zona}
    checar(tipos == {"qr", "aruco"}, f"le QR e ArUco na mesma passada ({tipos})")
    checar(len(agrupar_em_unidades(por_zona)) == 4,
           "os 12 codigos viram 4 unidades fisicas (uma por etiqueta)")

    det = _detector(catalogo, mapa)
    res = det.processar_frame(frame)
    checar(len(res.ocorrencias) == 4,
           f"os 3 codigos de cada etiqueta viram 1 ocorrencia ({len(res.ocorrencias)})")
    checar(all(o.estado is Estado.OK for o in res.ocorrencias), "todas OK")


def teste_so_aruco_visivel(catalogo, mapa):
    print("\n10) So o ArUco legivel (QR apagado por reflexo) ainda pega o erro")
    conteudo = {m.dispenser: m.qr for m in catalogo.medicamentos}
    conteudo[1], conteudo[3] = conteudo[3], conteudo[1]
    frame = montar_cena(catalogo, mapa, conteudo, etiqueta_completa=True)

    # apaga a area do QR de cada etiqueta, deixando so os marcadores ArUco
    for zona in mapa.zonas:
        cx, cy = zona.centro
        x1, y1 = int(cx) - 78, int(cy) - 78
        frame[max(0, y1) : y1 + 150, max(0, x1) : x1 + 118] = 245

    det = _detector(catalogo, mapa)
    res = det.processar_frame(frame)
    erros = [o for o in res.ocorrencias if o.estado is Estado.ERRO_POSICAO]
    checar(len(erros) == 2, f"2 erros detectados so pelo ArUco ({len(erros)})")
    checar(all(o.leitura.tipo == "aruco" for o in erros),
           "os erros vieram mesmo do marcador ArUco")
    pares = {(o.dispenser_detectado, o.dispenser_esperado) for o in erros}
    checar(pares == {(1, 3), (3, 1)}, f"pares corretos: {sorted(pares)}")


def teste_memoria_temporal(catalogo, mapa):
    print("\n11) Memoria temporal: leitura intermitente nao apaga o estado")
    conteudo = {m.dispenser: m.qr for m in catalogo.medicamentos}
    conteudo[1], conteudo[3] = conteudo[3], conteudo[1]
    bom = montar_cena(catalogo, mapa, conteudo, etiqueta_completa=True)
    vazio = montar_cena(catalogo, mapa, {}, etiqueta_completa=True)  # nada legivel

    relogio = [0.0]
    det = DetectorDispensers(
        catalogo=catalogo, zonas=mapa, leitor=LeitorCodigos(),
        frames_para_confirmar=1, cooldown_segundos=1e9, memoria_segundos=2.0,
        relogio=lambda: relogio[0],
    )
    res = det.processar_frame(bom)
    checar(len(res.erros_confirmados) == 2, "erro detectado no frame legivel")

    relogio[0] += 0.5
    res = det.processar_frame(vazio)
    checar(len(res.erros_confirmados) == 2,
           "erro permanece 0,5 s depois, mesmo sem conseguir ler")
    checar(all(o.lembrada for o in res.erros_confirmados), "marcado como vindo da memoria")
    checar(not res.alertas_novos, "memoria nao dispara alerta novo (nao duplica o log)")

    relogio[0] += 3.0
    res = det.processar_frame(vazio)
    checar(not res.ocorrencias, "depois de 2 s sem leitura a memoria expira")


def teste_cena_degradada(catalogo, mapa):
    print("\n12) Camera ruim: angulo + desfoque + luz baixa")
    import numpy as np

    conteudo = {m.dispenser: m.qr for m in catalogo.medicamentos}
    conteudo[2], conteudo[4] = conteudo[4], conteudo[2]
    # etiquetas menores (codigo com ~64 px) + desfoque + luz baixa + ruido
    frame = montar_cena(catalogo, mapa, conteudo, etiqueta_completa=True, lado_qr=64)

    np.random.seed(2)
    ruim = cv2.GaussianBlur(frame, (0, 0), 1.5)
    ruim = np.clip(ruim.astype(np.float32) * 0.40 + 20, 0, 255).astype(np.uint8)
    ruim = np.clip(ruim.astype(np.float32) + np.random.normal(0, 9, ruim.shape),
                   0, 255).astype(np.uint8)

    leitor_simples = LeitorCodigos(cascata=False, ler_por_zona=False, usar_aruco=False)
    # orcamento largo de proposito: aqui se mede CAPACIDADE de deteccao, nao o
    # escalonamento por tempo. Com o teto padrao de 250 ms o resultado passa a
    # depender de quanta CPU a maquina tem livre no momento, e o teste vira
    # loteria — foi o que aconteceu ao rodar logo depois de outra bateria.
    leitor_completo = LeitorCodigos(orcamento_ms=5000)

    n_simples = len(leitor_simples.ler(ruim))
    n_completo = len(leitor_completo.ler(ruim, zonas=mapa))
    print(f"      codigos brutos -> leitor simples: {n_simples} | completo: {n_completo}")
    checar(n_completo > n_simples,
           f"cascata + ArUco leem mais que o leitor simples ({n_completo} vs {n_simples})")

    completo = _detector(catalogo, mapa, leitor=LeitorCodigos(orcamento_ms=5000))
    res = completo.processar_frame(ruim)
    erros = [o for o in res.ocorrencias if o.estado is Estado.ERRO_POSICAO]
    print(f"      dispensers reconhecidos: {len(res.ocorrencias)}/4")
    checar(len(res.ocorrencias) == 4, "os 4 dispensers sao reconhecidos na cena ruim")
    checar(len(erros) == 2, f"a troca 2<->4 e detectada mesmo com a imagem ruim ({len(erros)})")


def teste_latencia_apos_troca(catalogo, mapa):
    print("\n13) Latencia: quanto tempo entre trocar a caixa e o alerta")
    correto = {m.dispenser: m.qr for m in catalogo.medicamentos}
    trocado = dict(correto)
    trocado[1], trocado[3] = trocado[3], trocado[1]

    frame_ok = montar_cena(catalogo, mapa, correto, etiqueta_completa=True)
    frame_erro = montar_cena(catalogo, mapa, trocado, etiqueta_completa=True)

    relogio = [0.0]
    det = DetectorDispensers(
        catalogo=catalogo, zonas=mapa, leitor=LeitorCodigos(),
        frames_para_confirmar=3, cooldown_segundos=10.0, memoria_segundos=2.0,
        relogio=lambda: relogio[0],
    )
    for _ in range(30):                       # 1 s de cena correta
        relogio[0] += 1 / 30.0
        det.processar_frame(frame_ok)

    frames_ate_alerta = None
    for i in range(1, 61):                    # ate 2 s depois da troca
        relogio[0] += 1 / 30.0
        res = det.processar_frame(frame_erro)
        if res.alertas_novos:
            frames_ate_alerta = i
            break

    ok = frames_ate_alerta is not None
    checar(ok, "o alerta dispara depois da troca")
    if ok:
        ms = frames_ate_alerta / 30.0 * 1000
        print(f"      alerta em {frames_ate_alerta} frames (~{ms:.0f} ms a 30 FPS)")
        checar(frames_ate_alerta <= 6,
               f"o cache de zona nao atrasa o alerta ({frames_ate_alerta} frames)")


def teste_custo_por_frame(catalogo, mapa):
    print("\n14) Custo por frame (o processamento pesado nao pode matar o FPS)")
    import time as _t

    import numpy as np

    frame = montar_cena(catalogo, mapa, etiqueta_completa=True)
    np.random.seed(4)
    frames = [
        np.clip(frame.astype(np.int16) + np.random.normal(0, 1.2, frame.shape), 0, 255)
        .astype(np.uint8)
        for _ in range(20)
    ]

    relogio = [0.0]
    det = DetectorDispensers(
        catalogo=catalogo, zonas=mapa, leitor=LeitorCodigos(), relogio=lambda: relogio[0]
    )
    det.processar_frame(frames[0])
    inicio = _t.perf_counter()
    for f in frames:
        relogio[0] += 1 / 30.0
        det.processar_frame(f)
    ms = (_t.perf_counter() - inicio) / len(frames) * 1000

    print(f"      {ms:.1f} ms/frame (~{1000 / ms:.0f} FPS possiveis)")
    checar(ms < 33.0, f"cabe em 30 FPS com folga ({ms:.1f} ms < 33 ms)")


# --------------------------------------------------------------------------- #
def main() -> int:
    print(f"Backend de leitura de QR: {backend_disponivel()}")
    catalogo = Catalogo.carregar()
    mapa = zonas_padrao(4)

    for teste in (
        teste_leitura_basica,
        teste_cena_correta,
        teste_troca_1_e_3,
        teste_qr_desconhecido,
        teste_debounce_e_cooldown,
        teste_fora_de_zona,
        teste_reescala_resolucao,
        teste_validacao_config,
        teste_aruco,
        teste_so_aruco_visivel,
        teste_memoria_temporal,
        teste_cena_degradada,
        teste_latencia_apos_troca,
        teste_custo_por_frame,
    ):
        teste(catalogo, mapa)

    print("\n" + "=" * 60)
    if FALHAS:
        print(f"{len(FALHAS)} verificacao(oes) falharam:")
        for f in FALHAS:
            print(f"  - {f}")
        return 1
    print("Todos os testes passaram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
