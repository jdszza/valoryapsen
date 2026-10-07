"""Testes dos modulos de produto: reconhecimento, fusao, estoque e auditoria.

Rode:  python tests/test_v2.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
sys.path.insert(0, str(RAIZ / "tests"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from configuracao import Catalogo, MapaZonas, Zona  # noqa: E402
from contagem import ContadorDeEstoque  # noqa: E402
from detector import DetectorDispensers, Estado, Ocorrencia  # noqa: E402
from embalagens import catalogo_sintetico, face_embalagem, variacao  # noqa: E402
from eventos import Evento, RegistroDeEventos  # noqa: E402
from fusao import MotorDeFusao, Veredito  # noqa: E402
from leitor_qr import LeituraQR  # noqa: E402
from reconhecimento import ReconhecedorVisual  # noqa: E402

FALHAS: list[str] = []

# Receita de cadastro recomendada em COMO_TESTAR.md: algumas inclinacoes, uma
# reducao de escala e duas fotos giradas. As giradas importam — sem elas o
# reconhecimento so cobre bem os quatro angulos retos.
CADASTRO = [dict(), dict(graus=20), dict(graus=-20), dict(graus=40),
            dict(graus=-40), dict(escala=0.5)]
GIROS_CADASTRO = (45, 90)


def checar(condicao: bool, descricao: str) -> None:
    print(f"  [{'PASS' if condicao else 'FALHA'}] {descricao}")
    if not condicao:
        FALHAS.append(descricao)


def _girar(img, graus):
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), graus, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    M[0, 2] += nw / 2 - w / 2.0
    M[1, 2] += nh / 2 - h / 2.0
    return cv2.warpAffine(img, M, (nw, nh), borderValue=(110, 110, 110))


def _reconhecedor(catalogo):
    faces = catalogo_sintetico(catalogo.medicamentos)
    rec = ReconhecedorVisual()
    for sku, face in faces.items():
        med = catalogo.buscar(sku)
        imagens = [variacao(face, semente=i, **kw) for i, kw in enumerate(CADASTRO)]
        imagens += [_girar(face, g) for g in GIROS_CADASTRO]
        rec.cadastrar(sku, med.nome, imagens)
    return rec, faces


def _ocorrencia(catalogo, sku, dispenser, zonas, tipo="qr"):
    """Monta uma leitura de codigo ja atribuida a uma zona."""
    zona = zonas.por_dispenser(dispenser)
    cx, cy = zona.centro
    poligono = np.float32([[cx - 40, cy - 40], [cx + 40, cy - 40],
                           [cx + 40, cy + 40], [cx - 40, cy + 40]])
    leitura = LeituraQR(conteudo=sku, poligono=poligono, tipo=tipo)
    med = catalogo.buscar(sku)
    return Ocorrencia(
        estado=Estado.OK if med and med.dispenser == dispenser else (
            Estado.QR_DESCONHECIDO if med is None else Estado.ERRO_POSICAO),
        conteudo=sku, leitura=leitura, zona=zona, medicamento=med,
        esperado_na_zona=catalogo.esperado_em(dispenser), confirmada=True,
    )


# --------------------------------------------------------------------------- #
def teste_reconhecimento(catalogo):
    print("\n1) Reconhecimento da embalagem sem nenhum codigo")
    cv2.setRNGSeed(12345)
    rec, faces = _reconhecedor(catalogo)
    checar(len(rec.referencias) == 4, f"4 embalagens cadastradas ({len(rec.referencias)})")

    acertos = trocas = 0
    for sku, face in faces.items():
        for kw in ({}, dict(graus=30), dict(brilho=0.5)):
            res = rec.identificar(variacao(face, semente=5, **kw))
            if res.nivel != "desconhecido":
                acertos += res.sku == sku
                trocas += res.sku != sku
    # 12 variacoes = 4 medicamentos x {frontal, girado 30 graus, luz fraca}.
    # O piso e baixo (6) por dois motivos: o giro de 30 graus e o ponto fraco
    # conhecido do canal visual (ver ROBUSTEZ.md), e o RANSAC do OpenCV e
    # estocastico, entao o numero exato oscila entre 7 e 8 de uma execucao para
    # outra. O que NAO pode ceder sao as verificacoes seguintes: nada de trocar
    # um medicamento por outro, nem aceitar embalagem estranha.
    checar(acertos >= 6, f"identificou em {acertos}/12 variacoes")
    checar(trocas == 0, "nunca trocou um medicamento por outro")

    # o invariante que realmente importa: girar a caixa nao pode virar erro
    girados = trocas_giradas = 0
    for sku, face in faces.items():
        for graus in (90, 180, 270):
            res = rec.identificar(_girar(face, graus))
            girados += res.nivel != "desconhecido" and res.sku == sku
            trocas_giradas += res.nivel != "desconhecido" and res.sku != sku
    checar(girados >= 9, f"caixa girada 90/180/270 ainda e identificada ({girados}/12)")
    checar(trocas_giradas == 0, "caixa girada nunca vira outro medicamento")


def teste_rejeicao_desconhecido(catalogo):
    print("\n2) Embalagem nao cadastrada precisa ser rejeitada")
    rec, _ = _reconhecedor(catalogo)
    aceitos = 0
    total = 0
    for i in range(8):
        intruso = face_embalagem(f"Produto {i} 10mg", f"XX-{i:03d}", indice=i + 4)
        for kw in ({}, dict(graus=25), dict(escala=0.5)):
            total += 1
            aceitos += rec.identificar(variacao(intruso, semente=i, **kw)).aceito
    checar(aceitos == 0, f"nenhuma embalagem estranha foi aceita (0 de {total})")

    liso = np.full((300, 400, 3), 200, np.uint8)
    checar(not rec.identificar(liso).aceito, "superficie lisa nao vira reconhecimento")


def teste_fusao_concordancia(catalogo, zonas):
    print("\n3) Fusao: codigo e embalagem concordando")
    rec, faces = _reconhecedor(catalogo)
    motor = MotorDeFusao(catalogo)

    med = catalogo.medicamentos[0]
    ident = rec.identificar(faces[med.qr])
    oc = _ocorrencia(catalogo, med.qr, med.dispenser, zonas)
    r = motor.avaliar(zonas.por_dispenser(med.dispenser), oc, ident)
    checar(r.veredito is Veredito.OK, f"veredito OK ({r.veredito.value})")
    checar(r.fonte == "codigo+visual", f"usou as duas evidencias ({r.fonte})")
    checar(r.confianca >= 0.9, f"confianca alta ({r.confianca:.2f})")

    # mesmo medicamento, mas no dispenser errado
    errado = 3 if med.dispenser != 3 else 2
    oc2 = _ocorrencia(catalogo, med.qr, errado, zonas)
    r2 = motor.avaliar(zonas.por_dispenser(errado), oc2, ident)
    checar(r2.veredito is Veredito.ERRO_POSICAO,
           f"erro de posicao detectado ({r2.veredito.value})")
    print(f"      -> {r2.mensagem()}")


def teste_fusao_divergencia(catalogo, zonas):
    print("\n4) Fusao: o caso que so ela enxerga — etiqueta trocada")
    rec, faces = _reconhecedor(catalogo)
    motor = MotorDeFusao(catalogo)

    a, b = catalogo.medicamentos[0], catalogo.medicamentos[1]
    # caixa do medicamento B fisicamente, mas com o codigo do A colado nela
    ident = rec.identificar(faces[b.qr])
    oc = _ocorrencia(catalogo, a.qr, a.dispenser, zonas)

    r = motor.avaliar(zonas.por_dispenser(a.dispenser), oc, ident)
    checar(r.veredito is Veredito.DIVERGENCIA,
           f"divergencia detectada ({r.veredito.value})")
    checar(r.critico, "classificada como critica")
    checar(a.nome in r.detalhe and b.nome in r.detalhe,
           "a mensagem nomeia os dois lados do conflito")
    print(f"      -> {r.mensagem()}")

    # e o ponto principal: so com codigo, isso passaria batido
    r_sem_visual = motor.avaliar(zonas.por_dispenser(a.dispenser), oc, None)
    checar(r_sem_visual.veredito is Veredito.OK,
           "sem a evidencia visual o mesmo caso seria dado como OK")


def teste_fusao_sem_codigo(catalogo, zonas):
    print("\n5) Fusao: codigo ilegivel, so a embalagem")
    rec, faces = _reconhecedor(catalogo)
    motor = MotorDeFusao(catalogo)
    med = catalogo.medicamentos[2]
    ident = rec.identificar(faces[med.qr])

    errado = 1 if med.dispenser != 1 else 2
    r = motor.avaliar(zonas.por_dispenser(errado), None, ident)
    checar(r.veredito is Veredito.ERRO_POSICAO,
           "erro de posicao detectado so pela aparencia")
    checar(r.fonte == "visual", f"fonte marcada como visual ({r.fonte})")
    checar(r.confianca < 0.5, f"confianca menor que com codigo ({r.confianca:.2f})")

    vazio = motor.avaliar(zonas.por_dispenser(1), None, None)
    checar(vazio.veredito is Veredito.VAZIO, "zona sem nada vira VAZIO")


def teste_politica_exigir_visual(catalogo, zonas):
    print("\n6) Politica opcional: exigir as duas evidencias")
    motor = MotorDeFusao(catalogo, exigir_visual=True)
    med = catalogo.medicamentos[0]
    oc = _ocorrencia(catalogo, med.qr, med.dispenser, zonas)
    r = motor.avaliar(zonas.por_dispenser(med.dispenser), oc, None)
    checar(r.veredito is Veredito.INDETERMINADO,
           "so codigo nao basta quando a politica exige as duas")


def teste_contagem():
    print("\n7) Contagem de estoque por altura da pilha")
    from test_v2_helpers import cena_pilha  # noqa: PLC0415

    contador = ContadorDeEstoque()
    vazio, zona = cena_pilha(0)
    contador.registrar_vazio(vazio, zona)
    cheio, _ = cena_pilha(6)
    altura = contador.aprender_altura(cheio, zona, 6)
    checar(abs(altura - 90) < 8, f"altura da caixa aprendida: {altura:.0f} px (real 90)")

    erros = []
    for n in range(0, 7):
        frame, _ = cena_pilha(n, semente=n)
        contador._ultimo.clear()
        nivel = contador.medir(frame, zona)
        erros.append(abs((nivel.caixas or 0) - n))
    checar(max(erros) <= 1, f"erro maximo de {max(erros)} caixa em 7 niveis")
    checar(sum(erros) == 0, f"contagem exata nos 7 niveis (soma dos erros {sum(erros)})")

    frame, _ = cena_pilha(1, semente=1)
    contador._ultimo.clear()
    checar(contador.medir(frame, zona).precisa_repor, "aviso de reposicao com 1 caixa")
    frame, _ = cena_pilha(6, semente=1)
    contador._ultimo.clear()
    checar(not contador.medir(frame, zona).precisa_repor, "sem aviso com a pilha cheia")

    sem_ref = ContadorDeEstoque()
    sem_ref.aprender_altura(cheio, zona, 6)
    frame, _ = cena_pilha(3, semente=3)
    checar(sem_ref.medir(frame, zona).confianca <= 0.35,
           "sem foto da prateleira vazia, a medida sai marcada como pouco confiavel")


def teste_auditoria():
    print("\n8) Trilha de auditoria encadeada por hash")
    import sqlite3

    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "ev.db"
        registro = RegistroDeEventos(caminho, estacao="teste")
        for i in range(6):
            registro.registrar(Evento(tipo="conferencia", dispenser=(i % 4) + 1,
                                      veredito="OK", detalhe=f"evento {i}"))
        checar(registro.verificar_integridade()["integra"], "cadeia integra apos gravar")
        checar(registro.resumo()["eventos"] == 6, "resumo conta os 6 eventos")
        registro.fechar()

        con = sqlite3.connect(caminho)
        con.execute("UPDATE eventos SET veredito='ERRO_POSICAO' WHERE id=3")
        con.commit()
        con.close()

        registro = RegistroDeEventos(caminho)
        veredito = registro.verificar_integridade()
        checar(not veredito["integra"], "alteracao no banco e detectada")
        checar(veredito["id_problema"] == 3,
               f"aponta exatamente o evento alterado (id {veredito['id_problema']})")
        registro.fechar()

        caminho2 = Path(pasta) / "ev2.db"
        registro = RegistroDeEventos(caminho2, estacao="teste")
        for i in range(4):
            registro.registrar(Evento(tipo="conferencia", dispenser=1, detalhe=f"e{i}"))
        registro.fechar()
        con = sqlite3.connect(caminho2)
        con.execute("DELETE FROM eventos WHERE id=2")
        con.commit()
        con.close()
        registro = RegistroDeEventos(caminho2)
        checar(not registro.verificar_integridade()["integra"],
               "remocao de evento tambem e detectada")
        registro.fechar()


def teste_api():
    print("\n9) API do painel")
    import json
    import urllib.request

    from api import ServidorPainel

    with tempfile.TemporaryDirectory() as pasta:
        registro = RegistroDeEventos(Path(pasta) / "api.db", estacao="bancada-teste")
        registro.registrar(Evento(tipo="conferencia", dispenser=2,
                                  veredito="DIVERGENCIA", confianca=0.95,
                                  detalhe="codigo e embalagem discordam"))
        registro.atualizar_estado(2, veredito="DIVERGENCIA", medicamento="Teste",
                                  confianca=0.95, caixas=3, fracao=0.5)
        servidor = ServidorPainel(registro, porta=8099)
        servidor.iniciar()
        try:
            def pegar(rota):
                with urllib.request.urlopen(f"http://localhost:8099{rota}", timeout=5) as r:
                    return json.loads(r.read())

            checar(pegar("/api/saude")["ok"], "endpoint de saude responde")
            estado = pegar("/api/estado")["estado"]
            checar(len(estado) == 1 and estado[0]["dispenser"] == 2,
                   "estado atual exposto na API")
            criticos = pegar("/api/eventos?criticos=1")["eventos"]
            checar(len(criticos) == 1, "filtro de eventos criticos funciona")
            checar(pegar("/api/integridade")["integra"], "integridade exposta na API")
            with urllib.request.urlopen("http://localhost:8099/", timeout=5) as r:
                checar(b"Conferencia" in r.read(), "dashboard e servido")
        finally:
            servidor.parar()
            registro.fechar()


def teste_fluxo_completo(catalogo, zonas):
    print("\n10) Fluxo ponta a ponta: visao -> fusao -> evento -> API")
    rec, faces = _reconhecedor(catalogo)
    motor = MotorDeFusao(catalogo)
    from eventos import eventos_de_fusao

    with tempfile.TemporaryDirectory() as pasta:
        registro = RegistroDeEventos(Path(pasta) / "fluxo.db", estacao="bancada-01")

        a, b = catalogo.medicamentos[0], catalogo.medicamentos[2]
        ident_b = rec.identificar(faces[b.qr])
        oc_a = _ocorrencia(catalogo, a.qr, a.dispenser, zonas)
        resultados = [
            motor.avaliar(zonas.por_dispenser(a.dispenser), oc_a, ident_b),
            motor.avaliar(zonas.por_dispenser(b.dispenser), None, None),
        ]
        for evento in eventos_de_fusao(resultados):
            registro.registrar(evento)

        criticos = registro.listar(apenas_criticos=True)
        checar(len(criticos) == 1, f"1 evento critico gravado ({len(criticos)})")
        checar(criticos[0]["veredito"] == "DIVERGENCIA",
               "o evento critico e a divergencia")
        checar(registro.verificar_integridade()["integra"], "trilha continua integra")
        checar(registro.resumo()["criticos"] == 1, "resumo reflete o critico")
        registro.fechar()


def teste_camera_cega(catalogo):
    print("\n11) Camera tapada precisa virar alarme, nao 'tudo vazio'")
    import argparse

    from cena_sintetica import montar_cena, zonas_padrao
    from estacao import Estacao
    import eventos as modulo_eventos

    with tempfile.TemporaryDirectory() as pasta:
        original = modulo_eventos.ARQ_BANCO
        modulo_eventos.ARQ_BANCO = Path(pasta) / "cego.db"
        try:
            args = argparse.Namespace(sem_visual=True, sem_estoque=True,
                                      exigir_visual=False, estacao="teste")
            estacao = Estacao(args)
            mapa = zonas_padrao()
            bom = montar_cena(catalogo, mapa, etiqueta_completa=True)

            _, vereditos = estacao.processar(bom)
            checar(len(vereditos) == 4, f"imagem boa gera 4 vereditos ({len(vereditos)})")

            tapada = np.full_like(bom, 20)
            for _ in range(20):
                _, vereditos = estacao.processar(tapada)
            checar(estacao._camera_cega, "camera tapada e detectada")
            checar(len(vereditos) == 0,
                   "nenhum veredito e publicado com a camera cega "
                   "(nao vira 'dispenser vazio')")
            falhas = [e for e in estacao.registro.listar(limite=30) if e["tipo"] == "falha"]
            checar(len(falhas) == 1, "gravou um evento de falha explicito")

            for _ in range(2):
                _, vereditos = estacao.processar(bom)
            checar(not estacao._camera_cega and len(vereditos) == 4,
                   "volta a operar sozinha quando a imagem retorna")
            estacao.registro.fechar()
        finally:
            modulo_eventos.ARQ_BANCO = original


def _pilha(catalogo, medicamentos, largura=230, altura_cena=1500):
    """Cena de um dispenser com varias caixas empilhadas."""
    from gerar_qrcodes import gerar_etiqueta

    im = np.full((altura_cena, 360, 3), 100, np.uint8)
    y = altura_cena - 60
    for med in medicamentos:
        arte = cv2.cvtColor(
            np.array(gerar_etiqueta(med.qr, med.nome, med.dispenser, 30,
                                    med.aruco).convert("RGB")),
            cv2.COLOR_RGB2BGR)
        arte = cv2.resize(arte, (largura, int(arte.shape[0] * largura / arte.shape[1])))
        y -= arte.shape[0] + 10
        if y < 30:
            break
        im[y:y + arte.shape[0], 40:40 + arte.shape[1]] = arte
    zonas = MapaZonas(zonas=[Zona(1, 10, 20, 340, altura_cena - 40)],
                      resolucao=(360, altura_cena))
    return im, zonas


def _contar(catalogo, medicamentos):
    from leitor_qr import LeitorCodigos

    im, zonas = _pilha(catalogo, medicamentos)
    det = DetectorDispensers(catalogo=catalogo, zonas=zonas,
                             leitor=LeitorCodigos(orcamento_ms=5000),
                             frames_para_confirmar=1, memoria_segundos=0.0)
    res = det.processar_frame(im)
    por_zona = {}
    for oc in res.ocorrencias:
        por_zona.setdefault(oc.dispenser_detectado, []).append(oc)
    veredito = MotorDeFusao(catalogo).avaliar(zonas.zonas[0], por_zona.get(1, []), None)
    return res, veredito, im, zonas


def teste_contagem_de_unidades(catalogo):
    print("\n12) Varias caixas do MESMO medicamento no dispenser")
    med = catalogo.medicamentos[0]

    for quantidade in (1, 2, 3, 5):
        res, veredito, _, _ = _contar(catalogo, [med] * quantidade)
        contagem = res.contagem_por_dispenser().get(1, {})
        checar(contagem.get(med.nome) == quantidade,
               f"{quantidade} caixa(s) -> contou {contagem.get(med.nome)}")
        checar(veredito.veredito is Veredito.OK and veredito.quantidade_certa == quantidade,
               f"veredito OK com {veredito.quantidade_certa} unidades certas")

    # a etiqueta tem 3 codigos: contar codigos daria 3x mais
    res, _, _, _ = _contar(catalogo, [med] * 3)
    codigos = sum(len(o.codigos) for o in res.ocorrencias)
    checar(codigos > 3 and len(res.ocorrencias) == 3,
           f"{codigos} codigos agrupados em 3 unidades (nao {codigos} unidades)")


def teste_contagem_com_qr_tapado(catalogo):
    print("\n13) Uma das caixas com o QR tapado ainda e contada")
    from leitor_qr import LeitorCodigos

    med = catalogo.medicamentos[0]
    im, zonas = _pilha(catalogo, [med] * 3)

    # tapa o QR da caixa do meio, deixando os ArUco visiveis
    altura_etiqueta = 197
    topo = im.shape[0] - 60 - 2 * (altura_etiqueta + 10)
    im[topo + 8:topo + altura_etiqueta - 60, 44:44 + 150] = 250

    leituras = LeitorCodigos(orcamento_ms=5000).ler(im, zonas=zonas)
    qrs = sum(1 for l in leituras if l.tipo == "qr")
    checar(qrs == 2, f"so 2 QR legiveis ({qrs})")

    det = DetectorDispensers(catalogo=catalogo, zonas=zonas,
                             leitor=LeitorCodigos(orcamento_ms=5000),
                             frames_para_confirmar=1, memoria_segundos=0.0)
    res = det.processar_frame(im)
    contagem = res.contagem_por_dispenser().get(1, {})
    checar(contagem.get(med.nome) == 3,
           f"as 3 caixas sao contadas mesmo assim ({contagem.get(med.nome)}) — "
           "o ArUco cobre a que perdeu o QR")


def teste_contagem_misturada(catalogo):
    print("\n14) Caixas certas e erradas no mesmo dispenser")
    certo, errado = catalogo.medicamentos[0], catalogo.medicamentos[2]

    res, veredito, _, _ = _contar(catalogo, [certo, certo, errado, certo])
    contagem = res.contagem_por_dispenser().get(1, {})
    checar(contagem.get(certo.nome) == 3, f"3 corretas ({contagem.get(certo.nome)})")
    checar(contagem.get(errado.nome) == 1, f"1 errada ({contagem.get(errado.nome)})")
    checar(veredito.veredito is Veredito.ERRO_POSICAO,
           "uma unidade errada no meio de tres certas mantem o dispenser em erro")
    checar(veredito.quantidade_certa == 3 and veredito.quantidade_errada == 1,
           f"veredito separa {veredito.quantidade_certa} certas de "
           f"{veredito.quantidade_errada} errada(s)")
    print(f"      -> {veredito.mensagem()}")

    res, veredito, _, _ = _contar(catalogo, [certo, errado, certo, errado])
    checar(veredito.quantidade_certa == 2 and veredito.quantidade_errada == 2,
           f"2 e 2 ({veredito.quantidade_certa} e {veredito.quantidade_errada})")
    checar("2x" in veredito.mensagem(), "a mensagem informa a quantidade errada")


def teste_custo_leitura(catalogo):
    print("\n15) Custo por frame do leitor (o FPS nao pode cair)")
    import time as _t

    from cena_sintetica import montar_cena, zonas_padrao
    from leitor_qr import LeitorCodigos

    mapa = zonas_padrao()
    frame = montar_cena(catalogo, mapa, etiqueta_completa=True)

    livre = LeitorCodigos(ler_por_zona=False)
    livre.ler(frame, agora=0.0)
    inicio = _t.perf_counter()
    for i in range(20):
        livre.ler(frame, agora=i / 30.0)
    ms_livre = (_t.perf_counter() - inicio) / 20 * 1000
    print(f"      modo livre (testar.py) ... {ms_livre:5.1f} ms  "
          f"(~{1000 / ms_livre:.0f} FPS)")
    checar(ms_livre < 55, f"modo livre cabe em 20 FPS ({ms_livre:.0f} ms)")

    por_zona = LeitorCodigos()
    por_zona.ler(frame, zonas=mapa, agora=0.0)
    inicio = _t.perf_counter()
    for i in range(20):
        por_zona.ler(frame, zonas=mapa, agora=i / 30.0)
    ms_zona = (_t.perf_counter() - inicio) / 20 * 1000
    print(f"      modo por zona (estacao) .. {ms_zona:5.1f} ms  "
          f"(~{1000 / ms_zona:.0f} FPS)")
    checar(ms_zona < 33, f"modo por zona cabe em 30 FPS ({ms_zona:.0f} ms)")


def teste_interface(catalogo, zonas):
    """A tela nao pode custar mais que a leitura, nem comer acento."""
    print("\n16) Interface de operacao (custo e texto)")
    import time as _t

    from fusao import MotorDeFusao
    from interface import (
        Medidas,
        PainelApsen,
        desenhar_zonas_no_video,
        encaixar,
        limpar,
        reduzir_frame,
    )

    # 1) o Hershey do OpenCV so desenha ASCII: acento e seta viram '?' na tela
    checar(limpar("Dipirona 500mg → fora de lugar") == "Dipirona 500mg -> fora de lugar",
           "acento e seta viram ASCII antes de ir para a tela")
    checar("?" not in limpar("Ibuprofeno esta na posicao errada — conferir"),
           "nenhum caractere sobra como '?'")

    # 2) texto comprido tem que ser cortado por LARGURA, nao por contagem de letras
    curto = encaixar("Amoxicilina 500mg comprimido revestido", 120, 0.42, 1)
    checar(curto.endswith("...") and len(curto) < 38, f"texto comprido e cortado ({curto!r})")
    checar(encaixar("Dipirona", 400, 0.42, 1) == "Dipirona",
           "texto que cabe nao e mexido")

    # 3) reducao de resolucao: a alavanca de FPS
    grande = np.zeros((1080, 1920, 3), np.uint8)
    checar(reduzir_frame(grande, 960).shape[:2] == (540, 960), "1080p reduz para 960x540")
    checar(reduzir_frame(grande, 0).shape == grande.shape, "largura 0 nao reduz")

    # 4) custo de desenhar a tela inteira
    fus = MotorDeFusao(catalogo)
    vereditos = fus.avaliar_frame(zonas, {}, {})
    video = np.zeros((540, 960, 3), np.uint8)
    painel = PainelApsen(1280, 720)
    painel.compor(video, vereditos, catalogo, Medidas())
    inicio = _t.perf_counter()
    for _ in range(30):
        img = desenhar_zonas_no_video(video.copy(), vereditos, zonas)
        painel.compor(img, vereditos, catalogo, Medidas())
    ms = (_t.perf_counter() - inicio) / 30 * 1000
    print(f"      desenhar a tela .......... {ms:5.1f} ms  (~{1000 / ms:.0f} FPS so de UI)")
    checar(ms < 10, f"a interface cabe no orcamento de 30 FPS ({ms:.1f} ms)")


# --------------------------------------------------------------------------- #
def _garantir_zonas():
    """O teste da camera cega instancia a Estacao, que exige zonas calibradas."""
    from cena_sintetica import zonas_padrao
    from configuracao import ARQ_ZONAS

    if not Path(ARQ_ZONAS).exists():
        zonas_padrao().salvar(ARQ_ZONAS)
        return True
    return False


def main() -> int:
    # O RANSAC do OpenCV e estocastico: sem semente fixa, o numero de
    # identificacoes oscila entre execucoes e o teste vira loteria perto do
    # limiar. Fixando aqui, uma falha significa regressao de verdade.
    cv2.setRNGSeed(12345)
    np.random.seed(12345)

    criou_zonas = _garantir_zonas()
    catalogo = Catalogo.carregar()
    zonas = MapaZonas(
        zonas=[Zona(i + 1, 40 + i * 300, 100, 260, 500) for i in range(4)],
        resolucao=(1280, 720),
    )

    teste_reconhecimento(catalogo)
    teste_rejeicao_desconhecido(catalogo)
    teste_fusao_concordancia(catalogo, zonas)
    teste_fusao_divergencia(catalogo, zonas)
    teste_fusao_sem_codigo(catalogo, zonas)
    teste_politica_exigir_visual(catalogo, zonas)
    teste_contagem()
    teste_auditoria()
    teste_api()
    teste_fluxo_completo(catalogo, zonas)
    teste_camera_cega(catalogo)
    teste_contagem_de_unidades(catalogo)
    teste_contagem_com_qr_tapado(catalogo)
    teste_contagem_misturada(catalogo)
    teste_custo_leitura(catalogo)
    teste_interface(catalogo, zonas)

    if criou_zonas:
        from configuracao import ARQ_ZONAS
        Path(ARQ_ZONAS).unlink(missing_ok=True)

    print("\n" + "=" * 62)
    if FALHAS:
        print(f"{len(FALHAS)} verificacao(oes) falharam:")
        for f in FALHAS:
            print(f"  - {f}")
        return 1
    print("Todos os testes passaram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
