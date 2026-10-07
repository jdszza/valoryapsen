"""Cadastro da aparencia das embalagens — o "treino" do reconhecimento visual.

Nao ha treino de rede: cadastrar um medicamento e tirar de 4 a 8 fotos dele na
propria bancada, na propria luz, no proprio angulo. Isso e uma vantagem
operacional, nao uma limitacao: quem cadastra e o operador, em um minuto, sem
precisar de ninguem de dados no meio.

Uso:
    python src/cadastrar_visual.py --sku MED-001         # ao vivo, pela camera
    python src/cadastrar_visual.py --sku MED-001 --zona 1
    python src/cadastrar_visual.py --sku MED-001 --pasta fotos/dipirona/
    python src/cadastrar_visual.py --listar
    python src/cadastrar_visual.py --remover MED-001

Ao vivo:
    ESPACO  captura uma foto do recorte da zona
    Z       muda a zona de captura
    ENTER   salva o cadastro
    ESC     sai sem salvar

Dica de qualidade: varie um pouco o angulo e a distancia entre as fotos, e
inclua uma com a luz mais fraca. Foto sempre igual gera cadastro fragil.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))

from camera import abrir_camera  # noqa: E402
from configuracao import Catalogo, MapaZonas, carregar_parametros  # noqa: E402
from reconhecimento import DIR_REFERENCIAS, ReconhecedorVisual  # noqa: E402

JANELA = "Cadastro de embalagem"
EXTENSOES = (".png", ".jpg", ".jpeg", ".bmp", ".webp")


def _listar(rec: ReconhecedorVisual) -> None:
    if not rec.referencias:
        print("Nenhuma embalagem cadastrada ainda.")
        return
    print(f"{'SKU':14s} {'medicamento':28s} {'fotos':>6s} {'pontos':>8s}")
    for sku, ref in sorted(rec.referencias.items()):
        print(f"{sku:14s} {ref.nome[:28]:28s} {len(ref.amostras):6d} {ref.total_pontos:8d}")


def main() -> int:
    p = argparse.ArgumentParser(description="Cadastra a aparencia de uma embalagem.")
    p.add_argument("--sku", type=str, default=None, help="codigo do medicamento (ex: MED-001)")
    p.add_argument("--pasta", type=str, default=None, help="cadastra a partir de fotos existentes")
    p.add_argument("--zona", type=int, default=None, help="dispenser de onde recortar")
    p.add_argument("--camera", type=int, default=None)
    p.add_argument("--listar", action="store_true")
    p.add_argument("--remover", type=str, default=None)
    p.add_argument("--saida", type=str, default=str(DIR_REFERENCIAS))
    args = p.parse_args()

    catalogo = Catalogo.carregar()
    rec = ReconhecedorVisual()
    rec.carregar(args.saida)

    if args.listar:
        _listar(rec)
        return 0

    if args.remover:
        rec.remover(args.remover)
        rec.salvar(args.saida)
        print(f"{args.remover} removido do cadastro visual.")
        return 0

    if not args.sku:
        p.error("informe --sku (ou use --listar / --remover)")

    medicamento = catalogo.buscar(args.sku)
    if medicamento is None:
        print(f"[aviso] {args.sku} nao esta em config/medicamentos.json")
    nome = medicamento.nome if medicamento else args.sku

    # ---------------- a partir de uma pasta de fotos ---------------- #
    if args.pasta:
        pasta = Path(args.pasta)
        arquivos = sorted(a for a in pasta.iterdir() if a.suffix.lower() in EXTENSOES)
        imagens = [img for img in (cv2.imread(str(a)) for a in arquivos) if img is not None]
        if not imagens:
            print(f"Nenhuma imagem utilizavel em {pasta}")
            return 1
        usadas = rec.cadastrar(args.sku, nome, imagens)
        rec.salvar(args.saida)
        print(f"{usadas}/{len(imagens)} fotos aproveitadas para {nome} [{args.sku}].")
        return 0

    # ---------------- ao vivo ---------------- #
    parametros = carregar_parametros()
    cam_cfg = parametros.get("camera", {})
    try:
        zonas = MapaZonas.carregar()
    except FileNotFoundError:
        print("Calibre as zonas primeiro: python src/calibrar.py")
        return 1

    numeros = [z.dispenser for z in zonas.zonas]
    zona_atual = args.zona if args.zona in numeros else (
        medicamento.dispenser if medicamento and medicamento.dispenser in numeros
        else numeros[0]
    )

    indice = args.camera if args.camera is not None else int(cam_cfg.get("indice", 0))
    cap = abrir_camera(indice, int(cam_cfg.get("largura", 1280)),
                       int(cam_cfg.get("altura", 720)),
                       int(cam_cfg.get("fps", 30)), ajustes=cam_cfg)
    if cap is None:
        print("Camera indisponivel.")
        return 1

    capturas: list = []
    fonte = cv2.FONT_HERSHEY_SIMPLEX
    print(f"Cadastrando '{nome}' [{args.sku}]. ESPACO captura, ENTER salva, ESC sai.")

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        vivas = zonas.para_resolucao(frame.shape[1], frame.shape[0])
        zona = vivas.por_dispenser(zona_atual) or vivas.zonas[0]

        tela = frame.copy()
        cv2.rectangle(tela, (zona.x, zona.y), (zona.x2, zona.y2), (90, 220, 120), 2)
        cv2.rectangle(tela, (0, 0), (tela.shape[1], 62), (18, 18, 18), -1)
        cv2.putText(tela, f"{nome} [{args.sku}]  zona {zona.dispenser}", (14, 26),
                    fonte, 0.6, (235, 235, 235), 2, cv2.LINE_AA)
        cv2.putText(tela, f"{len(capturas)} foto(s)  |  ESPACO captura  Z muda zona  "
                          f"ENTER salva  ESC sai", (14, 50), fonte, 0.5,
                    (170, 170, 170), 1, cv2.LINE_AA)
        cv2.imshow(JANELA, tela)

        k = cv2.waitKey(20) & 0xFF
        if k in (27, ord("q")):
            print("saindo sem salvar")
            break
        if k == 32:  # espaco
            recorte = frame[max(0, zona.y):zona.y2, max(0, zona.x):zona.x2].copy()
            if recorte.size:
                capturas.append(recorte)
                print(f"  foto {len(capturas)} capturada")
        elif k in (ord("z"), ord("Z")):
            zona_atual = numeros[(numeros.index(zona_atual) + 1) % len(numeros)]
        elif k in (13, 10):
            if not capturas:
                print("  nenhuma foto capturada ainda")
                continue
            usadas = rec.cadastrar(args.sku, nome, capturas)
            rec.salvar(args.saida)
            destino = Path(args.saida) / "fotos" / args.sku
            destino.mkdir(parents=True, exist_ok=True)
            for i, img in enumerate(capturas):
                cv2.imwrite(str(destino / f"{i:02d}.png"), img)
            print(f"Cadastrado: {usadas}/{len(capturas)} fotos aproveitadas. "
                  f"Originais em {destino}")
            break

    cap.release()
    try:
        cv2.destroyAllWindows()
    except cv2.error:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
