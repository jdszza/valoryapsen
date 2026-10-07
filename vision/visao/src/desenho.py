"""Overlay visual: zonas, QR codes lidos, status por dispenser e banner de erro."""

from __future__ import annotations

import cv2
import numpy as np

from configuracao import Catalogo, MapaZonas, Zona
from detector import Estado, Ocorrencia, ResultadoFrame

FONTE = cv2.FONT_HERSHEY_SIMPLEX

VERDE = (80, 200, 120)
VERMELHO = (60, 60, 235)
AMARELO = (60, 190, 240)
CINZA = (170, 170, 170)
BRANCO = (255, 255, 255)
PRETO = (0, 0, 0)

COR_POR_ESTADO = {
    Estado.OK: VERDE,
    Estado.ERRO_POSICAO: VERMELHO,
    Estado.QR_DESCONHECIDO: VERMELHO,
    Estado.FORA_DE_ZONA: AMARELO,
}


def _texto_com_fundo(
    img: np.ndarray,
    texto: str,
    org: tuple[int, int],
    cor: tuple[int, int, int] = BRANCO,
    escala: float = 0.5,
    espessura: int = 1,
    fundo: tuple[int, int, int] = PRETO,
    alpha: float = 0.65,
) -> None:
    (w, h), base = cv2.getTextSize(texto, FONTE, escala, espessura)
    x, y = org
    x = max(0, min(x, img.shape[1] - w - 8))
    y = max(h + 6, min(y, img.shape[0] - 4))
    p1 = (x - 4, y - h - 5)
    p2 = (x + w + 4, y + base + 2)
    recorte = img[max(0, p1[1]) : p2[1], max(0, p1[0]) : p2[0]]
    if recorte.size:
        overlay = np.full_like(recorte, fundo, dtype=np.uint8)
        cv2.addWeighted(overlay, alpha, recorte, 1 - alpha, 0, recorte)
    cv2.putText(img, texto, (x, y), FONTE, escala, cor, espessura, cv2.LINE_AA)


def desenhar_zona(
    img: np.ndarray,
    zona: Zona,
    cor: tuple[int, int, int] = CINZA,
    espessura: int = 2,
    rotulo: str | None = None,
    selecionada: bool = False,
    alcas: bool = False,
) -> None:
    p1, p2 = (zona.x, zona.y), (zona.x2, zona.y2)
    cv2.rectangle(img, p1, p2, cor, espessura + (1 if selecionada else 0), cv2.LINE_AA)

    if selecionada:
        cv2.rectangle(img, (p1[0] - 3, p1[1] - 3), (p2[0] + 3, p2[1] + 3), BRANCO, 1, cv2.LINE_AA)

    if alcas:
        for hx, hy in ((zona.x, zona.y), (zona.x2, zona.y), (zona.x, zona.y2), (zona.x2, zona.y2)):
            cv2.rectangle(img, (hx - 5, hy - 5), (hx + 5, hy + 5), cor, -1)
            cv2.rectangle(img, (hx - 5, hy - 5), (hx + 5, hy + 5), BRANCO, 1)

    texto = rotulo if rotulo is not None else f"DISPENSER {zona.dispenser}"
    _texto_com_fundo(img, texto, (zona.x + 6, zona.y + 22), cor, 0.55, 2)


def desenhar_leitura(img: np.ndarray, oc: Ocorrencia) -> None:
    cor = COR_POR_ESTADO.get(oc.estado, CINZA)
    pts = oc.leitura.poligono.astype(np.int32).reshape(-1, 1, 2)
    # leitura vinda da memoria (o codigo nao foi decodificado neste frame) fica
    # com contorno tracejado/fino, para o operador saber que e estado retido
    cv2.polylines(img, [pts], True, cor, 1 if oc.lembrada else 3, cv2.LINE_AA)

    x, y, w, h = oc.leitura.caixa
    rotulo = oc.nome_medicamento if oc.medicamento else f"?? {oc.conteudo[:18]}"
    if oc.leitura.tipo == "aruco":
        rotulo += "  [ArUco]"
    if oc.lembrada:
        rotulo += f"  (memoria {oc.idade_segundos:.1f}s)"
    _texto_com_fundo(img, rotulo, (x, y - 8), cor, 0.5, 1)

    if oc.estado is Estado.ERRO_POSICAO:
        _texto_com_fundo(
            img,
            f"E do disp. {oc.dispenser_esperado} -> esta no {oc.dispenser_detectado}",
            (x, y + h + 20),
            VERMELHO,
            0.48,
            1,
        )
        # X sobre o codigo
        cv2.line(img, (x, y), (x + w, y + h), VERMELHO, 2, cv2.LINE_AA)
        cv2.line(img, (x + w, y), (x, y + h), VERMELHO, 2, cv2.LINE_AA)


def desenhar_banner(img: np.ndarray, texto: str, cor: tuple[int, int, int] = VERMELHO) -> None:
    altura = 46
    faixa = img[0:altura, :]
    overlay = np.full_like(faixa, cor, dtype=np.uint8)
    cv2.addWeighted(overlay, 0.85, faixa, 0.15, 0, faixa)
    cv2.putText(img, texto, (14, 31), FONTE, 0.72, BRANCO, 2, cv2.LINE_AA)


LARGURA_PAINEL = 300


def anexar_painel(
    img: np.ndarray,
    resultado: ResultadoFrame,
    catalogo: Catalogo,
    zonas: MapaZonas,
) -> np.ndarray:
    """Acrescenta uma faixa lateral com o status de cada dispenser.

    A faixa e adicionada AO LADO da imagem (nao por cima), para nao tapar
    nenhum dispenser da cena.
    """
    altura, largura_img = img.shape[:2]
    faixa = np.full((altura, LARGURA_PAINEL, 3), 24, dtype=np.uint8)
    img = np.hstack([img, faixa])
    x0 = largura_img

    y = 34
    cv2.putText(img, "STATUS DOS DISPENSERS", (x0 + 14, y), FONTE, 0.55, BRANCO, 2, cv2.LINE_AA)
    y += 12
    cv2.line(img, (x0 + 14, y), (img.shape[1] - 14, y), (90, 90, 90), 1)
    y += 26

    por_disp = resultado.por_dispenser()
    contagens = resultado.contagem_por_dispenser()
    for numero in sorted({z.dispenser for z in zonas.zonas}):
        esperado = catalogo.esperado_em(numero)
        ocs = por_disp.get(numero, [])
        erros = [o for o in ocs if o.e_erro]

        if erros:
            cor, situacao = VERMELHO, "ERRO"
        elif ocs:
            cor, situacao = VERDE, "OK"
        else:
            cor, situacao = CINZA, "vazio"

        cv2.circle(img, (x0 + 22, y - 5), 7, cor, -1)
        cv2.putText(img, f"Disp. {numero}", (x0 + 38, y), FONTE, 0.5, BRANCO, 1, cv2.LINE_AA)
        cv2.putText(img, situacao, (x0 + 210, y), FONTE, 0.5, cor, 2, cv2.LINE_AA)
        y += 19
        nome = (esperado.nome if esperado else "nao cadastrado")[:30]
        cv2.putText(img, f"esperado: {nome}", (x0 + 38, y), FONTE, 0.4, CINZA, 1, cv2.LINE_AA)
        y += 17

        # quantas unidades de cada medicamento ha nesta zona
        contagem = contagens.get(numero, {})
        for item, quantidade in sorted(contagem.items(), key=lambda x: -x[1])[:3]:
            certo = esperado is not None and item == esperado.nome
            cv2.putText(img, f"{quantidade}x {item[:24]}", (x0 + 38, y), FONTE, 0.4,
                        VERDE if certo else VERMELHO, 1, cv2.LINE_AA)
            y += 17
        y += 12

    return img


def renderizar(
    frame: np.ndarray,
    resultado: ResultadoFrame,
    catalogo: Catalogo,
    mostrar_painel: bool = True,
    fps: float | None = None,
) -> np.ndarray:
    img = frame.copy()
    zonas = resultado.zonas
    por_disp = resultado.por_dispenser()
    contagens = resultado.contagem_por_dispenser()

    if zonas is not None:
        for zona in zonas.zonas:
            tem_erro = any(o.e_erro for o in por_disp.get(zona.dispenser, []))
            tem_leitura = bool(por_disp.get(zona.dispenser))
            cor = VERMELHO if tem_erro else (VERDE if tem_leitura else CINZA)
            esperado = catalogo.esperado_em(zona.dispenser)
            rotulo = f"DISP {zona.dispenser}"
            if esperado:
                rotulo += f" - {esperado.nome}"
            total = sum(contagens.get(zona.dispenser, {}).values())
            if total:
                rotulo += f"  [{total} un]"
            desenhar_zona(img, zona, cor, 2, rotulo)

    for oc in resultado.ocorrencias:
        desenhar_leitura(img, oc)

    if fps is not None:
        _texto_com_fundo(img, f"{fps:4.1f} FPS", (10, img.shape[0] - 12), BRANCO, 0.45, 1)

    if mostrar_painel and zonas is not None:
        img = anexar_painel(img, resultado, catalogo, zonas)

    erros = resultado.erros_confirmados
    if erros:
        principal = erros[0]
        if principal.estado is Estado.ERRO_POSICAO:
            quantos = (f"{principal.quantidade_na_zona}x "
                       if principal.quantidade_na_zona > 1 else "")
            texto = (
                f"ALERTA: {quantos}{principal.nome_medicamento} no dispenser "
                f"{principal.dispenser_detectado} (correto: {principal.dispenser_esperado})"
            )
        else:
            texto = f"ALERTA: {principal.mensagem()[:70]}"
        if len(erros) > 1:
            texto += f"  [+{len(erros) - 1}]"
        desenhar_banner(img, texto)

    return img
