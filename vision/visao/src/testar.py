"""Primeiro teste — com as etiquetas na mao, sem dispenser montado.

E o passo zero: antes de calibrar zona, antes de montar bancada, so aponte a
camera para uma etiqueta impressa e veja se ela e lida, a que distancia, e com
que folga. Nao precisa de config/zonas.json.

    python src/testar.py                 # modo livre, na camera
    python src/testar.py --camera camo   # celular como webcam (Camo, Iriun...)
    python src/testar.py --imagem foto.jpg
    python src/testar.py --checar        # so confere a instalacao e sai

Na janela:
    Q ou ESC ... sair
    E ......... salva um print em logs/
    C ......... conferir a instalacao no terminal

O que a tela mostra para cada codigo lido:
    - o medicamento e o dispenser a que ele pertence
    - o tipo (QR ou ArUco) e o tamanho em pixels
    - um veredito de folga: FOLGA BOA / NO LIMITE / PEQUENO DEMAIS
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import preprocessamento as pre  # noqa: E402
from camera import abrir_camera, indice_para_usar, listar_cameras  # noqa: E402
from configuracao import (  # noqa: E402
    ARQ_ZONAS,
    RAIZ,
    Catalogo,
    carregar_parametros,
)
from interface import reduzir_frame  # noqa: E402
from leitor_qr import (  # noqa: E402
    LeitorCodigos,
    agrupar_em_unidades,
    aruco_disponivel,
    backend_disponivel,
)

JANELA = "Primeiro teste"
FONTE = cv2.FONT_HERSHEY_SIMPLEX

VERDE = (80, 200, 120)
AMARELO = (60, 190, 240)
VERMELHO = (60, 60, 235)
CINZA = (170, 170, 170)
BRANCO = (245, 245, 245)

# limites medidos em tests/benchmark_pose.py
MIN_QR, BOM_QR = 40, 70
MIN_ARUCO, BOM_ARUCO = 25, 45


def conferir_instalacao() -> bool:
    """Diz o que esta pronto e o que falta, sem enrolacao."""
    print("=" * 66)
    print("CONFERENCIA DA INSTALACAO")
    print("=" * 66)
    tudo_ok = True

    print(f"  OpenCV ................. {cv2.__version__}")
    backend = backend_disponivel()
    if "pyzbar" in backend:
        print(f"  leitor de QR ........... pyzbar (o bom)")
    else:
        print("  leitor de QR ........... OpenCV (funciona, mas le menos)")
        print("      instale a libzbar para melhorar:")
        print("      Linux: sudo apt install libzbar0  |  macOS: brew install zbar")
    print(f"  ArUco .................. {'disponivel' if aruco_disponivel() else 'INDISPONIVEL'}")
    if not aruco_disponivel():
        print("      pip install opencv-contrib-python")
        tudo_ok = False

    try:
        catalogo = Catalogo.carregar()
        print(f"  catalogo ............... {len(catalogo.medicamentos)} medicamentos")
        for med in catalogo.medicamentos:
            extra = f", ArUco {med.aruco}" if med.aruco is not None else ""
            print(f"      dispenser {med.dispenser}: {med.nome} [{med.qr}{extra}]")
    except Exception as exc:
        print(f"  catalogo ............... ERRO: {exc}")
        tudo_ok = False

    etiquetas = sorted((RAIZ / "qrcodes").glob("*.png")) if (RAIZ / "qrcodes").exists() else []
    if etiquetas:
        print(f"  etiquetas geradas ...... {len(etiquetas)} arquivos em qrcodes/")
    else:
        print("  etiquetas geradas ...... NENHUMA — rode: python src/gerar_qrcodes.py")
        tudo_ok = False

    print(f"  zonas calibradas ....... "
          f"{'sim' if Path(ARQ_ZONAS).exists() else 'ainda nao (nao precisa para este teste)'}")

    referencias = RAIZ / "referencias" / "referencias.json"
    print(f"  embalagens cadastradas . "
          f"{'sim' if referencias.exists() else 'ainda nao (opcional)'}")

    cameras = listar_cameras()
    if cameras:
        print(f"  cameras ................ {len(cameras)} encontrada(s)")
        for camera in cameras:
            print(f"      {camera}")
        virtuais = [c for c in cameras if c.virtual]
        if virtuais:
            print(f"      -> camera virtual (celular/OBS) no indice {virtuais[0].indice}")
    else:
        print("  cameras ................ NENHUMA respondeu")
        print("      se usa o celular como webcam, abra o app antes (Camo, Iriun...)")
        tudo_ok = False

    print("=" * 66)
    return tudo_ok


def _veredito(leitura) -> tuple[str, tuple[int, int, int]]:
    lado = leitura.lado
    minimo, bom = (MIN_ARUCO, BOM_ARUCO) if leitura.tipo == "aruco" else (MIN_QR, BOM_QR)
    if lado >= bom:
        return "FOLGA BOA", VERDE
    if lado >= minimo:
        return "NO LIMITE", AMARELO
    return "PEQUENO DEMAIS", VERMELHO


def contar_unidades(leituras, catalogo) -> dict[str, int]:
    """Agrupa os codigos por medicamento e conta caixas fisicas, nao codigos."""
    por_medicamento: dict[str, list] = {}
    for l in leituras:
        med = catalogo.buscar(l.conteudo)
        por_medicamento.setdefault(med.nome if med else f"?? {l.conteudo}", []).append(l)
    return {nome: len(agrupar_em_unidades(grupo))
            for nome, grupo in por_medicamento.items()}


def desenhar(frame, leituras, catalogo, qualidade, fps=None) -> np.ndarray:
    img = frame.copy()
    altura, largura = img.shape[:2]

    # Um rotulo por UNIDADE, nao por codigo: com varias caixas na cena, rotular
    # cada QR e cada ArUco separadamente cobria a imagem inteira de texto.
    unidades_grupos = []
    por_medicamento: dict[str, list] = {}
    for l in leituras:
        med = catalogo.buscar(l.conteudo)
        por_medicamento.setdefault(med.nome if med else f"?? {l.conteudo}", []).append(l)
    for nome, grupo in por_medicamento.items():
        for cluster in agrupar_em_unidades(grupo):
            unidades_grupos.append((nome, cluster))

    for numero, (nome, cluster) in enumerate(unidades_grupos, start=1):
        principal = max(cluster, key=lambda l: (l.tipo == "qr", l.lado))
        med = catalogo.buscar(principal.conteudo)
        rotulo, cor = _veredito(principal)

        for l in cluster:
            pts = l.poligono.astype(np.int32).reshape(-1, 1, 2)
            cv2.polylines(img, [pts], True, cor, 3 if l is principal else 1, cv2.LINE_AA)

        x, y, w, h = principal.caixa
        texto = (f"#{numero}  {med.nome if med else 'NAO CADASTRADO'}"
                 f"   D{med.dispenser}" if med else f"#{numero}  NAO CADASTRADO")
        detalhe = (f"{principal.tipo.upper()} {principal.lado:.0f}px  {rotulo}"
                   f"   {len(cluster)} codigo(s)")

        largura_caixa = max(cv2.getTextSize(t, FONTE, 0.5, 1)[0][0]
                            for t in (texto, detalhe)) + 16
        topo = max(0, y - 48)
        esquerda = min(x, largura - largura_caixa - 4)
        cv2.rectangle(img, (esquerda, topo), (esquerda + largura_caixa, topo + 44),
                      (20, 20, 20), -1)
        cv2.putText(img, texto, (esquerda + 8, topo + 18), FONTE, 0.5, BRANCO, 1, cv2.LINE_AA)
        cv2.putText(img, detalhe, (esquerda + 8, topo + 36), FONTE, 0.46, cor, 1, cv2.LINE_AA)

    # contagem de unidades no canto superior
    unidades = contar_unidades(leituras, catalogo)
    if unidades:
        alt = 26 * len(unidades) + 14
        cv2.rectangle(img, (0, 0), (330, alt), (18, 18, 18), -1)
        y = 24
        for nome, quantidade in sorted(unidades.items(), key=lambda x: -x[1]):
            med = catalogo.buscar_por_nome(nome) if hasattr(catalogo, "buscar_por_nome") else None
            cv2.putText(img, f"{quantidade}x  {nome[:26]}", (12, y), FONTE, 0.62,
                        VERDE if not nome.startswith("??") else VERMELHO, 2, cv2.LINE_AA)
            y += 26

    # rodape com o estado da imagem
    cv2.rectangle(img, (0, altura - 76), (largura, altura), (18, 18, 18), -1)
    nitidez, brilho, reflexo = qualidade
    aviso = []
    if nitidez < 60:
        aviso.append("FORA DE FOCO")
    if brilho < 55:
        aviso.append("ESCURO")
    elif brilho > 205:
        aviso.append("ESTOURADO")
    if reflexo > 3:
        aviso.append("REFLEXO")

    total_unidades = sum(unidades.values())
    resumo = (f"{total_unidades} unidade(s) | {len(leituras)} codigo(s)"
              if leituras else "nenhum codigo lido")
    cv2.putText(img, resumo, (14, altura - 48),
                FONTE, 0.65, VERDE if leituras else CINZA, 2, cv2.LINE_AA)
    if fps is not None:
        cv2.putText(img, f"{fps:4.1f} FPS", (largura - 140, altura - 48), FONTE, 0.6,
                    VERDE if fps >= 15 else AMARELO, 2, cv2.LINE_AA)
    cv2.putText(img, f"nitidez {nitidez:4.0f}   brilho {brilho:3.0f}   reflexo {reflexo:4.1f}%",
                (14, altura - 22), FONTE, 0.5, CINZA, 1, cv2.LINE_AA)
    if aviso:
        cv2.putText(img, " | ".join(aviso), (largura - 340, altura - 22), FONTE, 0.58,
                    VERMELHO, 2, cv2.LINE_AA)
    cv2.putText(img, "Q sai   E salva print   C confere instalacao",
                (largura - 430, altura - 48), FONTE, 0.45, CINZA, 1, cv2.LINE_AA)
    return img


def main() -> int:
    p = argparse.ArgumentParser(description="Primeiro teste, sem precisar de calibracao.")
    p.add_argument("--camera", type=str, default=None, help="numero da camera ou parte do nome (ex: camo)")
    p.add_argument("--imagem", type=str, default=None)
    p.add_argument("--checar", action="store_true", help="so confere a instalacao")
    p.add_argument("--largura", type=int, default=None,
                   help="largura de processamento (px). Padrao: o mesmo do sistema. 0 desliga")
    args = p.parse_args()

    if args.checar:
        return 0 if conferir_instalacao() else 1

    conferir_instalacao()
    catalogo = Catalogo.carregar()
    parametros = carregar_parametros()
    cam_cfg = parametros.get("camera", {})
    det_cfg = parametros.get("deteccao", {})
    # Mede na MESMA resolucao em que o sistema vai rodar: um orcamento de
    # distancia medido em 1080p nao vale nada se a producao processa 960 px.
    largura_proc = (args.largura if args.largura is not None
                    else int(det_cfg.get("largura_processamento", 0)))

    # sem zonas: leitura livre no frame inteiro, que e o que se quer aqui
    leitor = LeitorCodigos(ler_por_zona=False, usar_aruco=True)

    if args.imagem:
        frame = cv2.imread(args.imagem)
        if frame is None:
            print(f"Nao consegui abrir {args.imagem}")
            return 1
        leituras = leitor.ler(frame)
        unidades = contar_unidades(leituras, catalogo)
        print(f"\n{len(leituras)} codigo(s) em {args.imagem} "
              f"= {sum(unidades.values())} unidade(s) fisica(s):")
        for nome, quantidade in sorted(unidades.items(), key=lambda x: -x[1]):
            print(f"  {quantidade}x  {nome}")
        print()
        for l in leituras:
            med = catalogo.buscar(l.conteudo)
            rotulo, _ = _veredito(l)
            print(f"  {l.tipo:6s} {l.conteudo:16s} {l.lado:5.0f} px  {rotulo:15s} "
                  f"{med.nome if med else 'NAO CADASTRADO'}")
        destino = RAIZ / "logs" / "primeiro_teste.png"
        destino.parent.mkdir(parents=True, exist_ok=True)
        g = pre.cinza(frame)
        cv2.imwrite(str(destino), desenhar(
            frame, leituras, catalogo,
            (pre.nitidez(g), pre.brilho(g), pre.percentual_estourado(g))))
        print(f"\nimagem anotada: {destino}")
        return 0

    indice = indice_para_usar(args.camera, cam_cfg)
    cap = abrir_camera(indice, int(cam_cfg.get("largura", 1280)),
                       int(cam_cfg.get("altura", 720)),
                       int(cam_cfg.get("fps", 30)), ajustes=cam_cfg)
    if cap is None:
        print("Camera indisponivel. Disponiveis:")
        for camera in listar_cameras():
            print(f"  {camera}")
        print("\nEscolha com: python src/camera.py")
        return 1

    print("\nAponte a camera para uma etiqueta impressa.")
    print("Aproxime e afaste para ver ate onde ele aguenta. Q encerra.\n")

    vistos: dict[str, float] = {}
    tempos: list[float] = []
    fps = 0.0
    try:
        while True:
            ok, bruto = cap.read()
            if not ok:
                break
            inicio = time.perf_counter()
            frame = reduzir_frame(bruto, largura_proc)
            leituras = leitor.ler(frame)
            g = pre.cinza(frame)
            qualidade = (pre.nitidez(g), pre.brilho(g), pre.percentual_estourado(g))

            tempos.append(time.perf_counter() - inicio)
            if len(tempos) > 30:
                tempos.pop(0)
            media = sum(tempos) / len(tempos)
            fps = 1.0 / media if media > 0 else 0.0

            for l in leituras:
                anterior = vistos.get(l.conteudo)
                if anterior is None or l.lado < anterior:
                    vistos[l.conteudo] = l.lado   # guarda o menor tamanho que leu

            try:
                cv2.imshow(JANELA, desenhar(frame, leituras, catalogo, qualidade, fps))
            except cv2.error:
                print("Sem suporte a janela neste ambiente; use --imagem")
                break

            k = cv2.waitKey(1) & 0xFF
            if k in (27, ord("q"), ord("Q")):
                break
            if k in (ord("e"), ord("E")):
                destino = RAIZ / "logs" / f"teste_{datetime.now():%Y%m%d_%H%M%S}.png"
                destino.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(destino), desenhar(frame, leituras, catalogo, qualidade, fps))
                print(f"print salvo em {destino}")
            if k in (ord("c"), ord("C")):
                conferir_instalacao()
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass

    if vistos:
        print("\nMenor tamanho em que cada codigo ainda foi lido:")
        for conteudo, lado in sorted(vistos.items(), key=lambda x: x[0]):
            med = catalogo.buscar(conteudo)
            tipo = "aruco" if conteudo.startswith("ARUCO:") else "qr"
            minimo = MIN_ARUCO if tipo == "aruco" else MIN_QR
            situacao = "ok" if lado >= minimo else "abaixo do limite util"
            print(f"  {conteudo:16s} {lado:5.0f} px  ({situacao})  "
                  f"{med.nome if med else ''}")
        print("\nProximo passo: monte os dispensers e rode  python src/calibrar.py")
    else:
        print("\nNenhum codigo foi lido. Confira, nesta ordem:")
        print("  1. a etiqueta esta impressa em 100% (meca a regua do rodape: 50 mm)")
        print("  2. a etiqueta aparece grande e nitida na imagem")
        print("  3. nao ha reflexo de luz em cima do codigo")
        print("  4. rode  python src/diagnostico.py  para medir foco, luz e reflexo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
