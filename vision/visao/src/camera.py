"""Abertura e escolha de camera, incluindo camera virtual (Camo, OBS, DroidCam).

No Windows o backend padrao (MSMF) costuma demorar ~5 s para abrir e as vezes
ignora a resolucao pedida; DirectShow resolve. No Linux, V4L2 e o caminho.

Camera virtual de celular (Camo, Iriun, DroidCam, EpocCam) e OBS aparecem para
o sistema como uma webcam comum — o unico problema e descobrir em qual indice
elas cairam, porque isso muda conforme a ordem em que os dispositivos sobem.
Por isso este modulo tenta descobrir o NOME de cada camera e aceita escolher
por nome ("camo") em vez de por numero.

Uso direto:
    python src/camera.py              # lista e deixa escolher, com previa
    python src/camera.py --qual       # qual camera os programas vao usar
    python src/camera.py --testar 1   # abre o indice 1 e mostra a imagem
    python src/camera.py --usar camo  # grava direto
    python src/camera.py --listar     # so lista
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2

RAIZ = Path(__file__).resolve().parent.parent
ARQ_PARAMETROS = RAIZ / "config" / "parametros.json"


@dataclass
class CameraEncontrada:
    indice: int
    nome: str
    largura: int
    altura: int
    nome_confiavel: bool = True

    @property
    def virtual(self) -> bool:
        if not self.nome_confiavel:
            return False   # sem certeza do nome, nao afirma nada
        alvo = self.nome.lower()
        return any(marca in alvo for marca in
                   ("camo", "obs", "droidcam", "iriun", "epoccam", "virtual",
                    "ndi", "elgato", "reincubate"))

    def __str__(self) -> str:
        if not self.nome_confiavel:
            return (f"[{self.indice}] {self.largura}x{self.altura}"
                    "  (nome nao confirmado — confira pela imagem)")
        etiqueta = "  <- camera virtual (celular/OBS)" if self.virtual else ""
        return f"[{self.indice}] {self.nome}  {self.largura}x{self.altura}{etiqueta}"


def _backends() -> list[int]:
    if sys.platform.startswith("win"):
        return [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
    if sys.platform == "darwin":
        return [cv2.CAP_AVFOUNDATION, cv2.CAP_ANY]
    return [cv2.CAP_V4L2, cv2.CAP_ANY]


# --------------------------------------------------------------------------- #
def nomes_de_cameras() -> tuple[dict[int, str], bool]:
    """(nomes por indice, se a ordem e confiavel).

    Detalhe que custou caro: o OpenCV nao expoe nome de dispositivo, e a lista
    de nomes que o sistema fornece NAO segue necessariamente a mesma ordem dos
    indices do OpenCV. Casar as duas listas por posicao — que era o que este
    modulo fazia — produz um mapa errado: mostra "Camo = 0" quando o 0 e a
    webcam integrada, e ai a escolha gravada abre a camera errada.

    Por isso o retorno agora traz um sinalizador de confianca:

      - Linux: confiavel. /sys/class/video4linux/videoN/name e o proprio N.
      - Windows COM pygrabber: confiavel. pygrabber enumera pelo mesmo
        DirectShow que o OpenCV usa com CAP_DSHOW, na mesma ordem.
      - Windows SEM pygrabber: NAO confiavel. A lista do PowerShell e de
        dispositivos PnP, com ordem propria — serve para dizer QUAIS cameras
        existem, nunca em que indice cada uma esta.
      - macOS: nao confiavel, mesma razao.

    Quando nao e confiavel, a unica verdade e a imagem: use a previa.
    """
    sistema = platform.system()

    if sistema == "Linux":
        nomes = {}
        base = Path("/sys/class/video4linux")
        if base.exists():
            for no in sorted(base.glob("video*")):
                try:
                    indice = int(no.name.replace("video", ""))
                    nomes[indice] = (no / "name").read_text(encoding="utf-8").strip()
                except (ValueError, OSError):
                    continue
        return nomes, True

    if sistema == "Windows":
        # pygrabber enumera pelo mesmo DirectShow que o OpenCV usa: ordem confiavel
        try:
            from pygrabber.dshow_graph import FilterGraph

            return dict(enumerate(FilterGraph().get_input_devices())), True
        except Exception:
            pass
        # sem pygrabber: da para saber QUAIS cameras existem, nao em que indice
        try:
            saida = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_PnPEntity | "
                 "Where-Object { $_.PNPClass -eq 'Camera' -or $_.PNPClass -eq 'Image' } | "
                 "Select-Object -ExpandProperty Name"],
                capture_output=True, text=True, timeout=8,
            )
            linhas = [l.strip() for l in saida.stdout.splitlines() if l.strip()]
            return dict(enumerate(linhas)), False
        except Exception:
            return {}, False

    if sistema == "Darwin":
        try:
            saida = subprocess.run(
                ["system_profiler", "SPCameraDataType"],
                capture_output=True, text=True, timeout=10,
            )
            nomes, indice = {}, 0
            for linha in saida.stdout.splitlines():
                despida = linha.strip()
                if despida.endswith(":") and not despida.startswith("Model"):
                    if despida not in ("Camera:", "Cameras:"):
                        nomes[indice] = despida.rstrip(":")
                        indice += 1
            return nomes, False
        except Exception:
            return {}, False

    return {}, False


def listar_cameras(maximo: int = 8) -> list[CameraEncontrada]:
    """Cameras que respondem, com nome e resolucao."""
    # sondar indices inexistentes faz o OpenCV cuspir um aviso por tentativa;
    # como as falhas aqui sao esperadas, o log fica em silencio durante a busca
    nivel = None
    try:
        nivel = cv2.utils.logging.getLogLevel()
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except Exception:
        pass

    nomes, confiavel = nomes_de_cameras()
    encontradas: list[CameraEncontrada] = []
    try:
        for i in range(maximo):
            cap = cv2.VideoCapture(i, _backends()[0])
            if cap.isOpened():
                ok, quadro = cap.read()
                if ok and quadro is not None:
                    encontradas.append(CameraEncontrada(
                        indice=i,
                        nome=nomes.get(i, f"camera {i}"),
                        largura=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                        altura=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                        nome_confiavel=confiavel and i in nomes,
                    ))
            cap.release()
    finally:
        if nivel is not None:
            try:
                cv2.utils.logging.setLogLevel(nivel)
            except Exception:
                pass
    return encontradas


def indices_disponiveis(maximo: int = 8) -> list[int]:
    return [c.indice for c in listar_cameras(maximo)]


# --------------------------------------------------------------------------- #
def resolver_camera(valor) -> int | None:
    """Aceita numero ou pedaco do nome. `resolver_camera("camo")` acha o Camo.

    Existe porque o indice de uma camera virtual muda conforme a ordem em que
    os dispositivos sobem: hoje o Camo e o 1, amanha e o 2 porque voce ligou
    uma webcam USB antes. Selecionar por nome sobrevive a isso.
    """
    if valor is None:
        return None
    if isinstance(valor, int):
        return valor
    texto = str(valor).strip()
    if texto.isdigit():
        return int(texto)

    alvo = texto.lower()
    for camera in listar_cameras():
        if camera.nome_confiavel and alvo in camera.nome.lower():
            print(f"Camera '{camera.nome}' encontrada no indice {camera.indice}.")
            return camera.indice

    print(f"[aviso] nao consegui localizar '{texto}' com seguranca. "
          "Escolha pela imagem: python src/camera.py")
    return None


# --------------------------------------------------------------------------- #
def aplicar_ajustes(cap: cv2.VideoCapture, cfg: dict) -> list[str]:
    """Fixa foco/exposicao/ganho quando configurados.

    Em camera barata esta e frequentemente a melhoria mais eficaz de todas:
    o autofoco fica cacando foco a cada movimento e a autoexposicao muda o
    brilho quando alguem passa na frente. Travando os dois, a imagem para de
    'respirar' e a taxa de leitura sobe muito.

    Camera virtual (Camo e afins) costuma ignorar estes controles — quem manda
    no foco e na exposicao e o app no celular. O programa avisa e segue.
    """
    aplicados: list[str] = []

    def definir(prop: int, valor, rotulo: str) -> None:
        if valor is None:
            return
        if cap.set(prop, float(valor)):
            aplicados.append(f"{rotulo}={valor}")
        else:
            print(f"[camera] '{rotulo}' nao e suportado por esta camera; ignorado")

    if cfg.get("autofoco") is not None:
        definir(cv2.CAP_PROP_AUTOFOCUS, 1 if cfg["autofoco"] else 0, "autofoco")
    definir(cv2.CAP_PROP_FOCUS, cfg.get("foco"), "foco")

    if cfg.get("autoexposicao") is not None:
        # 3 = automatico, 1 = manual (convencao V4L2/DirectShow)
        definir(cv2.CAP_PROP_AUTO_EXPOSURE, 3 if cfg["autoexposicao"] else 1,
                "autoexposicao")

    definir(cv2.CAP_PROP_EXPOSURE, cfg.get("exposicao"), "exposicao")
    definir(cv2.CAP_PROP_GAIN, cfg.get("ganho"), "ganho")
    definir(cv2.CAP_PROP_BRIGHTNESS, cfg.get("brilho"), "brilho")
    definir(cv2.CAP_PROP_CONTRAST, cfg.get("contraste"), "contraste")

    return aplicados


def abrir_camera(
    indice=0,
    largura: int = 1280,
    altura: int = 720,
    fps: int = 30,
    ajustes: dict | None = None,
) -> cv2.VideoCapture | None:
    """Abre a camera. `indice` aceita numero ou nome ('camo', 'obs', ...)."""
    resolvido = resolver_camera(indice)
    if resolvido is None:
        return None

    for backend in _backends():
        cap = cv2.VideoCapture(resolvido, backend)
        if not cap.isOpened():
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, largura)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, altura)
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # menos atraso entre cena e alerta
        ok, _ = cap.read()
        if ok:
            real_l = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            real_a = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            nome = nomes_de_cameras()[0].get(resolvido, f"camera {resolvido}")
            print(f"Camera {resolvido} ({nome}) aberta em {real_l}x{real_a}.")
            if (real_l, real_a) != (largura, altura):
                print(f"  [nota] pedi {largura}x{altura} e vieram {real_l}x{real_a}; "
                      "a camera escolheu o modo mais proximo")
            if ajustes:
                aplicados = aplicar_ajustes(cap, ajustes)
                if aplicados:
                    print(f"  ajustes fixados: {', '.join(aplicados)}")
            return cap
        cap.release()
    return None


# --------------------------------------------------------------------------- #
def salvar_escolha(indice: int, caminho: Path = ARQ_PARAMETROS) -> None:
    """Grava a camera escolhida em config/parametros.json (indice e, se der, nome).

    Guarda o nome junto quando ele e confiavel: na proxima vez o sistema procura
    a camera PELO NOME e so cai no indice se nao achar. E isso que faz a escolha
    sobreviver a trocar a ordem dos dispositivos.
    """
    caminho = Path(caminho)
    dados = json.loads(caminho.read_text(encoding="utf-8")) if caminho.exists() else {}
    cfg = dados.setdefault("camera", {})
    cfg["indice"] = int(indice)

    nomes, confiavel = nomes_de_cameras()
    if confiavel and indice in nomes:
        cfg["nome"] = nomes[indice]
        print(f"Gravado: camera {indice} ('{nomes[indice]}').")
    else:
        cfg.pop("nome", None)
        print(f"Gravado: camera {indice}.")

    caminho.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Arquivo: {caminho}")
    print("Confira com: python src/camera.py --qual")


def camera_configurada(caminho: Path = ARQ_PARAMETROS):
    """Qual camera os programas vao usar, resolvendo por nome quando possivel."""
    caminho = Path(caminho)
    if not caminho.exists():
        return 0, "padrao (config/parametros.json nao existe)"
    cfg = json.loads(caminho.read_text(encoding="utf-8")).get("camera", {})
    indice = int(cfg.get("indice", 0))
    nome = cfg.get("nome")

    if nome:
        nomes, confiavel = nomes_de_cameras()
        if confiavel:
            for i, n in nomes.items():
                if n == nome:
                    if i != indice:
                        return i, (f"'{nome}' mudou de indice ({indice} -> {i}); "
                                   "reencontrada pelo nome")
                    return i, f"'{nome}'"
            return indice, (f"'{nome}' nao esta conectada agora; "
                            f"usando o indice {indice} do arquivo")
    return indice, "por indice (sem nome gravado)"


def indice_para_usar(pedido, cfg: dict | None = None):
    """Qual camera abrir: o que veio por --camera, senao o que esta configurado.

    Passa pelo `camera_configurada()` para que o NOME gravado seja reencontrado
    mesmo se o indice mudou de uma sessao para outra.
    """
    if pedido is not None:
        return pedido
    indice, motivo = camera_configurada()
    if "mudou de indice" in motivo or "nao esta conectada" in motivo:
        print(f"[camera] {motivo}")
    return indice


def testar_camera(indice: int, segundos: float = 20.0) -> bool:
    """Abre uma camera e mostra a imagem, para confirmar com os olhos.

    Existe porque nome de dispositivo nao e confiavel em todo sistema: a unica
    prova de que o indice N e o Camo e ver a imagem do celular na tela.
    """
    import time as _t

    cap = abrir_camera(indice)
    if cap is None:
        print(f"Nao consegui abrir a camera {indice}.")
        return False

    print(f"Mostrando a camera {indice}. Aperte S se for a certa, N ou ESC se nao for.")
    inicio = _t.monotonic()
    confirmou = False
    try:
        while _t.monotonic() - inicio < segundos:
            ok, quadro = cap.read()
            if not ok:
                break
            cv2.rectangle(quadro, (0, 0), (quadro.shape[1], 56), (18, 18, 18), -1)
            cv2.putText(quadro, f"CAMERA {indice} — e esta?  S = sim   N/ESC = nao",
                        (14, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (245, 245, 245),
                        2, cv2.LINE_AA)
            cv2.imshow("Confirmar camera", quadro)
            k = cv2.waitKey(20) & 0xFF
            if k in (ord("s"), ord("S")):
                confirmou = True
                break
            if k in (27, ord("n"), ord("N")):
                break
    except cv2.error:
        print("(sem suporte a janela neste ambiente)")
    finally:
        cap.release()
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass
    return confirmou


def escolher_interativo(cameras: list[CameraEncontrada]) -> int | None:
    """Mostra uma previa de cada camera lado a lado e deixa escolher pela tecla."""
    import numpy as np

    quadros = []
    for camera in cameras:
        cap = cv2.VideoCapture(camera.indice, _backends()[0])
        ok, quadro = cap.read()
        cap.release()
        if not ok or quadro is None:
            quadro = np.full((360, 480, 3), 40, np.uint8)
        quadro = cv2.resize(quadro, (480, 360))

        cv2.rectangle(quadro, (0, 0), (480, 54), (18, 18, 18), -1)
        cv2.putText(quadro, f"[{camera.indice}] {camera.nome[:30]}", (10, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (245, 245, 245), 1, cv2.LINE_AA)
        cv2.putText(quadro, f"{camera.largura}x{camera.altura}"
                            f"{'   VIRTUAL (celular/OBS)' if camera.virtual else ''}",
                    (10, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (120, 220, 140) if camera.virtual else (170, 170, 170), 1, cv2.LINE_AA)
        cv2.rectangle(quadro, (0, 0), (479, 359), (90, 90, 90), 2)
        quadros.append(quadro)

    while len(quadros) % 2:
        import numpy as np

        quadros.append(np.full((360, 480, 3), 25, np.uint8))
    linhas = [cv2.hconcat(quadros[i:i + 2]) for i in range(0, len(quadros), 2)]
    montagem = cv2.vconcat(linhas)

    rodape = 46
    import numpy as np

    tela = np.full((montagem.shape[0] + rodape, montagem.shape[1], 3), 18, np.uint8)
    tela[:montagem.shape[0]] = montagem
    cv2.putText(tela, "Aperte o NUMERO da camera que voce quer usar   |   ESC cancela",
                (14, montagem.shape[0] + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (245, 245, 245), 1, cv2.LINE_AA)

    try:
        cv2.imshow("Escolha a camera", tela)
        escolha = None
        while True:
            k = cv2.waitKey(50) & 0xFF
            if k == 27:
                break
            if ord("0") <= k <= ord("9"):
                numero = k - ord("0")
                if any(c.indice == numero for c in cameras):
                    escolha = numero
                    break
        cv2.destroyAllWindows()
        return escolha
    except cv2.error:
        print("(sem suporte a janela; escolha pelo terminal)")
        return None


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Lista e escolhe a camera.")
    p.add_argument("--listar", action="store_true", help="so lista e sai")
    p.add_argument("--usar", type=str, default=None,
                   help="grava esta camera (numero ou nome, ex: camo)")
    p.add_argument("--qual", action="store_true",
                   help="mostra qual camera os programas vao usar")
    p.add_argument("--testar", type=int, default=None,
                   help="abre este indice e mostra a imagem para confirmar")
    args = p.parse_args()

    if args.qual:
        indice, motivo = camera_configurada()
        print(f"Os programas vao abrir a camera {indice}  ({motivo}).")
        print("Se nao for a que voce quer: python src/camera.py")
        return 0

    if args.testar is not None:
        if testar_camera(args.testar):
            salvar_escolha(args.testar)
        else:
            print("Nada gravado.")
        return 0

    if args.usar:
        indice = resolver_camera(args.usar)
        if indice is None:
            return 1
        salvar_escolha(indice)
        return 0

    print("Procurando cameras...\n")
    cameras = listar_cameras()
    if not cameras:
        print("Nenhuma camera respondeu.\n")
        print("Se voce usa o celular como webcam (Camo, Iriun, DroidCam):")
        print("  1. abra o app no computador E no celular")
        print("  2. confirme que a previa aparece no app antes de rodar isto")
        print("  3. feche outros programas que possam estar usando a camera")
        print("     (Teams, Meet, Zoom e o navegador seguram a camera)")
        return 1

    for camera in cameras:
        print(f"  {camera}")

    incertas = [c for c in cameras if not c.nome_confiavel]
    if incertas and platform.system() == "Windows":
        print("\n[atencao] Nao consegui confirmar QUAL nome corresponde a qual indice.")
        print("  O Windows lista os nomes numa ordem propria, que nem sempre bate")
        print("  com a numeracao do OpenCV — por isso nao vou afirmar qual e o Camo.")
        print("  Para ter os nomes certos:  pip install pygrabber")
        print("  Sem isso, escolha pela IMAGEM (e o que a previa abaixo mostra).")
    elif incertas:
        print("\n[atencao] nomes nao confirmados neste sistema; escolha pela imagem.")

    virtuais = [c for c in cameras if c.virtual]
    if virtuais:
        print(f"\nA camera virtual parece ser a [{virtuais[0].indice}] "
              f"{virtuais[0].nome}.")

    indice_atual, motivo = camera_configurada()
    print(f"\nHoje os programas abrem a camera {indice_atual} ({motivo}).")

    if args.listar:
        print("\nPara usar:   python src/camera.py --usar <numero ou nome>")
        print("Para conferir: python src/camera.py --testar <numero>")
        return 0

    print("\nAbrindo a previa de cada uma. Escolha pela IMAGEM, nao pelo nome.")
    escolha = escolher_interativo(cameras)
    if escolha is None:
        print("Nada escolhido. Use: python src/camera.py --usar <numero ou nome>")
        return 0
    salvar_escolha(escolha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
