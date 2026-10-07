"""Gera os QR codes de cada medicamento cadastrado em config/medicamentos.json.

Saida em qrcodes/:
  - MED-001_Dipirona-500mg.png   (etiqueta individual, com legenda)
  - folha_etiquetas.png / .pdf   (varias copias por pagina, prontas para imprimir)

Uso:
    python src/gerar_qrcodes.py
    python src/gerar_qrcodes.py --copias 24 --tamanho-mm 25
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_H

sys.path.insert(0, str(Path(__file__).resolve().parent))

from configuracao import RAIZ, Catalogo, carregar_parametros  # noqa: E402

DIR_SAIDA = RAIZ / "qrcodes"
DPI = 300


def _mm_para_px(mm: float, dpi: int = DPI) -> int:
    return int(round(mm / 25.4 * dpi))


def _fonte(tamanho: int) -> ImageFont.ImageFont:
    for caminho in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arial.ttf",
    ):
        if Path(caminho).exists():
            try:
                return ImageFont.truetype(caminho, tamanho)
            except Exception:
                pass
    return ImageFont.load_default()


def _slug(texto: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", texto).strip("-")


def _imagem_qr(texto: str, lado_px: int) -> Image.Image:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_H,  # tolera sujeira/reflexo na embalagem
        box_size=10,
        border=4,                          # zona silenciosa: NAO reduzir
    )
    qr.add_data(texto)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img.resize((lado_px, lado_px), Image.NEAREST)


def _imagem_aruco(ident: int, lado_px: int, dicionario: str = "DICT_4X4_50") -> Image.Image:
    """Marcador ArUco com zona silenciosa branca de 1 modulo em volta."""
    import cv2
    import numpy as np

    from leitor_qr import dicionario_aruco

    dic = dicionario_aruco(dicionario)
    interno = int(lado_px * 0.78)
    marca = cv2.aruco.generateImageMarker(dic, int(ident), interno)
    tela = np.full((lado_px, lado_px), 255, dtype=np.uint8)
    off = (lado_px - interno) // 2
    tela[off : off + interno, off : off + interno] = marca
    return Image.fromarray(tela).convert("RGB")


def gerar_etiqueta(
    qr_texto: str,
    nome: str,
    dispenser: int,
    lado_mm: float,
    aruco: int | None = None,
    dicionario_aruco_nome: str = "DICT_4X4_50",
) -> Image.Image:
    """Etiqueta com QR central, dois ArUco nos cantos e legenda.

    Por que dois ArUco em cantos opostos: o reflexo especular do plastico e
    quase sempre uma mancha localizada. Com os marcadores em cantos opostos,
    a mancha que apaga um deixa o outro legivel — e o QR ainda cobre o caso de
    reflexo suave, porque tem 30% de correcao de erro.
    """
    lado = _mm_para_px(lado_mm)
    # 50% do lado do QR: com menos modulos que o QR, o ArUco fica com
    # modulos ~2x maiores nesse tamanho — e por isso sobrevive ao desfoque
    lado_marca = int(lado * 0.50) if aruco is not None else 0
    folga = int(lado * 0.04)

    largura = lado + (lado_marca + folga if aruco is not None else 0)
    altura_texto = int(lado * 0.28)
    altura = max(lado, lado_marca * 2 + folga) + altura_texto

    etiqueta = Image.new("RGB", (largura, altura), "white")
    etiqueta.paste(_imagem_qr(qr_texto, lado), (0, 0))

    if aruco is not None:
        marca = _imagem_aruco(aruco, lado_marca, dicionario_aruco_nome)
        x = lado + folga
        etiqueta.paste(marca, (x, 0))                                  # canto superior
        etiqueta.paste(marca, (x, max(lado - lado_marca, lado_marca + folga)))  # oposto

    d = ImageDraw.Draw(etiqueta)
    sufixo = f"  |  ARUCO {aruco}" if aruco is not None else ""
    linhas = [
        (nome, max(10, int(lado * 0.085))),
        (f"{qr_texto}  |  DISPENSER {dispenser}{sufixo}", max(9, int(lado * 0.068))),
    ]

    y = altura - altura_texto + int(altura_texto * 0.05)
    util = largura - _mm_para_px(2)
    for texto, tamanho in linhas:
        fonte = _fonte(tamanho)
        while tamanho > 8 and d.textlength(texto, font=fonte) > util:
            tamanho -= 1                      # encolhe ate caber na etiqueta
            fonte = _fonte(tamanho)
        larg = d.textlength(texto, font=fonte)
        d.text(((largura - larg) / 2, y), texto, fill="black", font=fonte)
        y += int(tamanho * 1.5)

    d.rectangle([0, 0, etiqueta.width - 1, etiqueta.height - 1], outline="#cccccc")
    return etiqueta


def montar_folhas(
    etiquetas: list[tuple[Image.Image, int]],
    largura_mm: float = 210,
    altura_mm: float = 297,
    margem_mm: float = 10,
    espaco_mm: float = 4,
) -> list[Image.Image]:
    """Distribui as etiquetas (imagem, copias) em quantas paginas A4 forem precisas.

    Nenhuma etiqueta e descartada: quando a pagina enche, comeca uma nova.
    """
    largura_px = _mm_para_px(largura_mm)
    altura_px = _mm_para_px(altura_mm)
    margem = _mm_para_px(margem_mm)
    espaco = _mm_para_px(espaco_mm)

    rodape = _mm_para_px(14)  # espaco reservado para a regua de conferencia

    paginas: list[Image.Image] = []
    pagina = Image.new("RGB", (largura_px, altura_px), "white")
    paginas.append(pagina)
    x, y, altura_linha = margem, margem, 0

    for etiqueta, copias in etiquetas:
        for _ in range(copias):
            if x + etiqueta.width > largura_px - margem:  # quebra de linha
                x = margem
                y += altura_linha + espaco
                altura_linha = 0
            if y + etiqueta.height > altura_px - margem - rodape:  # quebra de pagina
                pagina = Image.new("RGB", (largura_px, altura_px), "white")
                paginas.append(pagina)
                x, y, altura_linha = margem, margem, 0
            pagina.paste(etiqueta, (x, y))
            x += etiqueta.width + espaco
            altura_linha = max(altura_linha, etiqueta.height)

    for pagina in paginas:
        _desenhar_regua(pagina, margem)
    return paginas


def _desenhar_regua(pagina: Image.Image, margem: int, mm_referencia: int = 50) -> None:
    """Regua de conferencia: se nao medir exatamente 50 mm, a impressao saiu fora de escala."""
    d = ImageDraw.Draw(pagina)
    comprimento = _mm_para_px(mm_referencia)
    y = pagina.height - margem
    x0 = margem
    x1 = x0 + comprimento

    d.line([(x0, y), (x1, y)], fill="black", width=4)
    for i in range(0, mm_referencia + 1, 10):
        x = x0 + _mm_para_px(i)
        d.line([(x, y - _mm_para_px(2)), (x, y + _mm_para_px(2))], fill="black", width=3)

    fonte = _fonte(28)
    d.text(
        (x1 + _mm_para_px(4), y - _mm_para_px(3)),
        f"CONFERENCIA: esta linha deve medir {mm_referencia} mm com regua. "
        "Se nao medir, imprima em 100% (Tamanho real).",
        fill="black",
        font=fonte,
    )


def cabem_por_pagina(
    etiqueta: Image.Image,
    largura_mm: float = 210,
    altura_mm: float = 297,
    margem_mm: float = 10,
    espaco_mm: float = 4,
) -> tuple[int, int]:
    """(colunas, linhas) de etiquetas que cabem numa pagina A4."""
    util_l = _mm_para_px(largura_mm - 2 * margem_mm)
    util_a = _mm_para_px(altura_mm - 2 * margem_mm)
    espaco = _mm_para_px(espaco_mm)
    colunas = max(1, (util_l + espaco) // (etiqueta.width + espaco))
    linhas = max(1, (util_a + espaco) // (etiqueta.height + espaco))
    return int(colunas), int(linhas)


def main() -> int:
    p = argparse.ArgumentParser(description="Gera os QR codes dos medicamentos.")
    p.add_argument("--tamanho-mm", type=float, default=30.0, help="lado do QR impresso, em mm")
    p.add_argument("--copias", type=int, default=8, help="copias de cada etiqueta na folha")
    p.add_argument("--saida", type=str, default=str(DIR_SAIDA))
    p.add_argument("--sem-aruco", action="store_true",
                   help="gera so o QR, sem o marcador ArUco redundante")
    args = p.parse_args()

    catalogo = Catalogo.carregar()
    parametros = carregar_parametros()
    dic = parametros.get("deteccao", {}).get("dicionario_aruco", "DICT_4X4_50")

    saida = Path(args.saida)
    saida.mkdir(parents=True, exist_ok=True)

    etiquetas: list[tuple[Image.Image, int]] = []
    for med in catalogo.medicamentos:
        aruco = None if args.sem_aruco else med.aruco
        etiqueta = gerar_etiqueta(
            med.qr, med.nome, med.dispenser, args.tamanho_mm, aruco, dic
        )
        nome_arq = saida / f"{_slug(med.qr)}_{_slug(med.nome)}.png"
        etiqueta.save(nome_arq, dpi=(DPI, DPI))
        etiquetas.append((etiqueta, args.copias))
        extra = f" + ArUco {aruco}" if aruco is not None else ""
        print(f"  {nome_arq.name}  -> dispenser {med.dispenser}{extra}")

    paginas = montar_folhas(etiquetas)
    colunas, linhas = cabem_por_pagina(etiquetas[0][0])
    total = sum(c for _, c in etiquetas)

    pdf = saida / "folha_etiquetas.pdf"
    paginas[0].save(
        pdf, "PDF", resolution=DPI,
        save_all=True, append_images=paginas[1:],
    )
    for i, pagina in enumerate(paginas, start=1):
        nome = "folha_etiquetas.png" if len(paginas) == 1 else f"folha_etiquetas_{i}.png"
        pagina.save(saida / nome, dpi=(DPI, DPI))

    et_l = etiquetas[0][0].width / DPI * 25.4
    et_a = etiquetas[0][0].height / DPI * 25.4
    print(f"\nFolha de impressao: {pdf}")
    print(f"  papel ............ A4 (210 x 297 mm), {len(paginas)} pagina(s)")
    print(f"  etiqueta ......... {et_l:.0f} x {et_a:.0f} mm "
          f"(QR de {args.tamanho_mm:.0f} mm + legenda)")
    print(f"  por pagina ....... {colunas} x {linhas} = {colunas * linhas} etiquetas")
    print(f"  total gerado ..... {total} etiquetas ({args.copias} de cada medicamento)")
    print("\nIMPRIMA EM 100% / 'Tamanho real'. Se marcar 'Ajustar a pagina', o QR sai")
    print("menor do que o previsto e o alcance de leitura da camera cai junto.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
