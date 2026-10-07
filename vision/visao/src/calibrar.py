"""Ferramenta interativa de calibracao das zonas dos dispensers.

Voce desenha 4 retangulos sobre a imagem da camera (um em volta da brecha de
cada dispenser), ajusta com o mouse ou o teclado, e salva em config/zonas.json.
Enquanto calibra, os QR codes visiveis sao lidos ao vivo e mostrados com a cor
do resultado — assim da para conferir se o retangulo esta pegando o codigo certo.

Uso:
    python src/calibrar.py                    # camera padrao
    python src/calibrar.py --camera 1
    python src/calibrar.py --imagem foto.jpg  # calibrar sobre uma foto fixa

Controles:
    mouse arrastar (area livre)  cria uma nova zona
    mouse clicar dentro          seleciona a zona
    mouse arrastar dentro        move a zona
    mouse arrastar num canto     redimensiona a zona
    TAB                          seleciona a proxima zona
    1..9                         define o numero do dispenser da zona selecionada
    setas                        move a zona selecionada (1 px; +SHIFT = 10 px)
    W/A/S/D                      redimensiona a zona selecionada
    G                            gera 4 zonas iguais em coluna (ponto de partida)
    DEL / X                      apaga a zona selecionada
    R                            apaga todas as zonas
    F                            congela / descongela a imagem
    H                            mostra / esconde a ajuda
    ENTER ou C                   salva em config/zonas.json
    ESC ou Q                     sai sem salvar
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from camera import abrir_camera, indice_para_usar, listar_cameras  # noqa: E402
from configuracao import (  # noqa: E402
    ARQ_ZONAS,
    Catalogo,
    MapaZonas,
    Zona,
    carregar_parametros,
)
from desenho import (  # noqa: E402
    AMARELO,
    BRANCO,
    CINZA,
    VERDE,
    VERMELHO,
    _texto_com_fundo,
    desenhar_zona,
)
from leitor_qr import LeitorCodigos  # noqa: E402

JANELA = "Calibracao dos dispensers"
TOLERANCIA_ALCA = 12

CORES_ZONA = [
    (120, 200, 255),
    (140, 235, 140),
    (255, 190, 120),
    (220, 150, 255),
    (150, 220, 235),
    (200, 200, 120),
]

AJUDA = [
    "arrastar area livre .. nova zona      TAB .......... proxima zona",
    "arrastar dentro ...... mover          1..9 ......... numero do dispenser",
    "arrastar canto ....... redimensionar  setas/WASD ... ajuste fino (SHIFT=10px)",
    "G .... 4 zonas em coluna              DEL/X ........ apagar zona",
    "F .... congelar imagem                R ............ apagar todas",
    "ENTER ou C ... SALVAR                 ESC ou Q ..... sair sem salvar",
]


class EstadoCalibracao:
    def __init__(self, mapa: MapaZonas) -> None:
        self.mapa = mapa
        self.selecionada: int | None = 0 if mapa.zonas else None
        self.arrastando: str | None = None  # 'novo' | 'mover' | canto
        self.ancora = (0, 0)
        self.zona_inicial: Zona | None = None
        self.nova: Zona | None = None
        self.mostrar_ajuda = True
        self.mensagem = ""

    # ------------------------------------------------------------------ #
    def proximo_numero(self) -> int:
        usados = {z.dispenser for z in self.mapa.zonas}
        n = 1
        while n in usados:
            n += 1
        return n

    def zona_no_ponto(self, x: int, y: int) -> int | None:
        candidatas = [
            (i, z) for i, z in enumerate(self.mapa.zonas) if z.contem(x, y, TOLERANCIA_ALCA)
        ]
        if not candidatas:
            return None
        # prioriza a selecionada, depois a menor
        for i, _ in candidatas:
            if i == self.selecionada:
                return i
        return min(candidatas, key=lambda p: p[1].largura * p[1].altura)[0]

    @staticmethod
    def canto_no_ponto(z: Zona, x: int, y: int) -> str | None:
        cantos = {
            "ne": (z.x, z.y),
            "nd": (z.x2, z.y),
            "se": (z.x, z.y2),
            "sd": (z.x2, z.y2),
        }
        for nome, (cx, cy) in cantos.items():
            if abs(x - cx) <= TOLERANCIA_ALCA and abs(y - cy) <= TOLERANCIA_ALCA:
                return nome
        return None


def _ao_usar_mouse(evento: int, x: int, y: int, flags: int, estado: EstadoCalibracao) -> None:
    zonas = estado.mapa.zonas

    if evento == cv2.EVENT_LBUTTONDOWN:
        idx = estado.zona_no_ponto(x, y)
        if idx is not None:
            estado.selecionada = idx
            z = zonas[idx]
            canto = estado.canto_no_ponto(z, x, y)
            estado.arrastando = canto or "mover"
            estado.ancora = (x, y)
            estado.zona_inicial = Zona(**z.para_dict())
        else:
            estado.arrastando = "novo"
            estado.ancora = (x, y)
            estado.nova = Zona(
                dispenser=estado.proximo_numero(), x=x, y=y, largura=0, altura=0
            )

    elif evento == cv2.EVENT_MOUSEMOVE and estado.arrastando:
        dx, dy = x - estado.ancora[0], y - estado.ancora[1]

        if estado.arrastando == "novo" and estado.nova is not None:
            estado.nova.largura = dx
            estado.nova.altura = dy

        elif estado.selecionada is not None and estado.zona_inicial is not None:
            z = zonas[estado.selecionada]
            z0 = estado.zona_inicial
            if estado.arrastando == "mover":
                z.x, z.y = z0.x + dx, z0.y + dy
            else:
                canto = estado.arrastando  # 'ne' | 'nd' | 'se' | 'sd'
                if canto in ("ne", "se"):        # borda esquerda
                    z.x, z.largura = z0.x + dx, z0.largura - dx
                elif canto in ("nd", "sd"):      # borda direita
                    z.largura = z0.largura + dx
                if canto in ("ne", "nd"):        # borda superior
                    z.y, z.altura = z0.y + dy, z0.altura - dy
                elif canto in ("se", "sd"):      # borda inferior
                    z.altura = z0.altura + dy

    elif evento == cv2.EVENT_LBUTTONUP and estado.arrastando:
        if estado.arrastando == "novo" and estado.nova is not None:
            estado.nova.normalizar()
            if estado.nova.largura >= 20 and estado.nova.altura >= 20:
                zonas.append(estado.nova)
                estado.selecionada = len(zonas) - 1
                estado.mensagem = f"Zona criada para o dispenser {estado.nova.dispenser}"
            estado.nova = None
        elif estado.selecionada is not None:
            zonas[estado.selecionada].normalizar()
        estado.arrastando = None
        estado.zona_inicial = None


def _gerar_colunas(largura: int, altura: int, quantidade: int = 4) -> list[Zona]:
    margem_x = int(largura * 0.04)
    margem_y = int(altura * 0.18)
    util = largura - 2 * margem_x
    vao = int(util * 0.02)
    w = (util - vao * (quantidade - 1)) // quantidade
    h = altura - 2 * margem_y
    return [
        Zona(dispenser=i + 1, x=margem_x + i * (w + vao), y=margem_y, largura=w, altura=h)
        for i in range(quantidade)
    ]


def _desenhar(
    frame: np.ndarray,
    estado: EstadoCalibracao,
    catalogo: Catalogo | None,
    leitor: LeitorCodigos | None,
    congelado: bool,
) -> np.ndarray:
    img = frame.copy()

    # QR codes lidos ao vivo, so para conferencia visual
    if leitor is not None:
        for leitura in leitor.ler(frame):
            cx, cy = leitura.centro
            zona = estado.mapa.zona_do_ponto(cx, cy)
            med = catalogo.buscar(leitura.conteudo) if catalogo else None
            if med is None:
                cor = AMARELO
                rotulo = f"?? {leitura.conteudo[:16]}"
            elif zona is None:
                cor = AMARELO
                rotulo = f"{med.nome} (fora de zona)"
            elif zona.dispenser == med.dispenser:
                cor = VERDE
                rotulo = f"{med.nome} OK"
            else:
                cor = VERMELHO
                rotulo = f"{med.nome} -> disp {med.dispenser}, esta no {zona.dispenser}"
            pts = leitura.poligono.astype(np.int32).reshape(-1, 1, 2)
            cv2.polylines(img, [pts], True, cor, 2, cv2.LINE_AA)
            x, y, _, _ = leitura.caixa
            _texto_com_fundo(img, rotulo, (x, y - 8), cor, 0.45, 1)
            cv2.circle(img, (int(cx), int(cy)), 4, cor, -1)

    for i, z in enumerate(estado.mapa.zonas):
        cor = CORES_ZONA[z.dispenser % len(CORES_ZONA)]
        sel = i == estado.selecionada
        med = catalogo.esperado_em(z.dispenser) if catalogo else None
        rotulo = f"DISP {z.dispenser}" + (f" - {med.nome}" if med else "")
        desenhar_zona(img, z, cor, 2, rotulo, selecionada=sel, alcas=sel)

    if estado.nova is not None:
        cv2.rectangle(
            img,
            (estado.nova.x, estado.nova.y),
            (estado.nova.x + estado.nova.largura, estado.nova.y + estado.nova.altura),
            BRANCO,
            1,
            cv2.LINE_AA,
        )

    # rodape
    y = img.shape[0] - 10
    if estado.mostrar_ajuda:
        for linha in reversed(AJUDA):
            _texto_com_fundo(img, linha, (12, y), BRANCO, 0.42, 1)
            y -= 20
    else:
        _texto_com_fundo(img, "H = ajuda", (12, y), CINZA, 0.42, 1)

    topo = f"{len(estado.mapa.zonas)} zona(s)"
    if estado.selecionada is not None and estado.mapa.zonas:
        topo += f" | selecionada: dispenser {estado.mapa.zonas[estado.selecionada].dispenser}"
    if congelado:
        topo += " | IMAGEM CONGELADA (F)"
    _texto_com_fundo(img, topo, (12, 26), BRANCO, 0.5, 1)

    if estado.mensagem:
        _texto_com_fundo(img, estado.mensagem, (12, 52), VERDE, 0.5, 1)

    return img


def main() -> int:
    p = argparse.ArgumentParser(description="Calibra as zonas dos dispensers.")
    p.add_argument("--camera", type=str, default=None, help="numero da camera ou parte do nome (ex: camo)")
    p.add_argument("--imagem", type=str, default=None, help="calibrar sobre uma foto")
    p.add_argument("--saida", type=str, default=str(ARQ_ZONAS))
    p.add_argument("--largura", type=int, default=None)
    p.add_argument("--altura", type=int, default=None)
    args = p.parse_args()

    parametros = carregar_parametros()
    cam_cfg = parametros.get("camera", {})
    det_cfg = parametros.get("deteccao", {})

    try:
        catalogo = Catalogo.carregar()
    except Exception as exc:
        print(f"[aviso] catalogo de medicamentos nao carregado: {exc}")
        catalogo = None

    leitor = LeitorCodigos(
        backend=det_cfg.get("backend", "auto"),
        escala=float(det_cfg.get("escala_processamento", 1.0)),
        melhorar_contraste=bool(det_cfg.get("melhorar_contraste", True)),
        cascata=bool(det_cfg.get("cascata", True)),
        usar_aruco=bool(det_cfg.get("usar_aruco", True)),
        dicionario_aruco=det_cfg.get("dicionario_aruco", "DICT_4X4_50"),
        ler_por_zona=False,   # na calibracao as zonas ainda estao mudando
    )

    # fonte da imagem
    cap = None
    frame_fixo = None
    if args.imagem:
        frame_fixo = cv2.imread(args.imagem)
        if frame_fixo is None:
            print(f"Nao consegui abrir a imagem {args.imagem}")
            return 1
    else:
        indice = indice_para_usar(args.camera, cam_cfg)
        cap = abrir_camera(
            indice,
            args.largura or int(cam_cfg.get("largura", 1280)),
            args.altura or int(cam_cfg.get("altura", 720)),
            int(cam_cfg.get("fps", 30)),
            ajustes=cam_cfg,
        )
        if cap is None:
            print(f"Nao consegui abrir a camera {indice}.")
            for camera in listar_cameras():
                print(f"  {camera}")
            print("Escolha com: python src/camera.py")
            return 1

    # zonas ja existentes
    try:
        mapa = MapaZonas.carregar(args.saida)
        print(f"Zonas existentes carregadas de {args.saida}.")
    except Exception:
        mapa = MapaZonas()

    estado = EstadoCalibracao(mapa)
    cv2.namedWindow(JANELA, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(JANELA, _ao_usar_mouse, estado)

    congelado = frame_fixo is not None
    ultimo_frame = frame_fixo

    while True:
        if not congelado and cap is not None:
            ok, frame = cap.read()
            if not ok:
                print("Falha ao ler da camera.")
                break
            if cam_cfg.get("espelhar"):
                frame = cv2.flip(frame, 1)
            ultimo_frame = frame
        frame = ultimo_frame
        if frame is None:
            break

        estado.mapa.resolucao = (frame.shape[1], frame.shape[0])
        cv2.imshow(JANELA, _desenhar(frame, estado, catalogo, leitor, congelado))

        tecla = cv2.waitKey(20) & 0xFFFF
        if tecla == 0xFFFF:
            if cv2.getWindowProperty(JANELA, cv2.WND_PROP_VISIBLE) < 1:
                break
            continue

        k = tecla & 0xFF
        sel = estado.selecionada
        zonas = estado.mapa.zonas
        z = zonas[sel] if (sel is not None and 0 <= sel < len(zonas)) else None
        passo = 10 if (tecla & 0x10000) or (k in b"WASD") else 1

        if k in (27, ord("q"), ord("Q")):
            print("Saindo sem salvar.")
            break

        elif k in (13, 10, ord("c"), ord("C")):
            estado.mapa.resolucao = (frame.shape[1], frame.shape[0])
            avisos = estado.mapa.validar(catalogo)
            for a in avisos:
                print(f"[aviso] {a}")
            estado.mapa.salvar(args.saida)
            print(f"Zonas salvas em {args.saida} ({len(zonas)} zonas).")
            estado.mensagem = "Salvo!"
            if not avisos:
                break

        elif k == 9 and zonas:  # TAB
            estado.selecionada = 0 if sel is None else (sel + 1) % len(zonas)

        elif ord("1") <= k <= ord("9") and z is not None:
            novo = k - ord("0")
            for outra in zonas:
                if outra is not z and outra.dispenser == novo:
                    outra.dispenser = z.dispenser  # troca
            z.dispenser = novo
            estado.mensagem = f"Zona agora e o dispenser {novo}"

        elif k in (ord("g"), ord("G")):
            estado.mapa.zonas = _gerar_colunas(frame.shape[1], frame.shape[0], 4)
            estado.selecionada = 0
            estado.mensagem = "4 zonas em coluna geradas - ajuste com o mouse"

        elif k in (255, 8, ord("x"), ord("X")) and z is not None:
            zonas.pop(sel)  # type: ignore[arg-type]
            estado.selecionada = len(zonas) - 1 if zonas else None
            estado.mensagem = "Zona apagada"

        elif k in (ord("r"), ord("R")):
            zonas.clear()
            estado.selecionada = None
            estado.mensagem = "Todas as zonas apagadas"

        elif k in (ord("f"), ord("F")) and cap is not None:
            congelado = not congelado

        elif k in (ord("h"), ord("H")):
            estado.mostrar_ajuda = not estado.mostrar_ajuda

        elif z is not None:
            if k in (81, 2):        # <-
                z.x -= passo
            elif k in (83, 3):      # ->
                z.x += passo
            elif k in (82, 0):      # ^
                z.y -= passo
            elif k in (84, 1):      # v
                z.y += passo
            elif k in (ord("a"), ord("A")):
                z.largura = max(20, z.largura - passo)
            elif k in (ord("d"), ord("D")):
                z.largura += passo
            elif k in (ord("w"), ord("W")):
                z.altura = max(20, z.altura - passo)
            elif k in (ord("s"), ord("S")):
                z.altura += passo

    if cap is not None:
        cap.release()
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
