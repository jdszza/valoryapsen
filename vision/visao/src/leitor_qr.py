"""Leitura de codigos com tolerancia a camera ruim.

Estrategias combinadas, da mais barata para a mais cara:

  1. QR direto no frame inteiro                    (custo baixo, pega o caso bom)
  2. ArUco no frame inteiro                        (aguenta angulo, desfoque e
                                                    pouca luz muito melhor que QR)
  3. Recorte por zona + ampliacao + cascata de     (recupera codigo pequeno,
     pre-processamento                              desfocado ou mal iluminado)

QR e ArUco sao complementares: o QR aguenta reflexo (tem correcao de erro de
~30%), o ArUco aguenta angulo, desfoque e tamanho pequeno. Usando os dois na
mesma etiqueta, praticamente nao existe condicao em que os dois falhem juntos.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

import preprocessamento as pre

try:
    from pyzbar import pyzbar as _pyzbar

    _PYZBAR_OK = True
except Exception:  # pragma: no cover
    _pyzbar = None
    _PYZBAR_OK = False

_ARUCO_OK = hasattr(cv2, "aruco")

PREFIXO_ARUCO = "ARUCO:"


# --------------------------------------------------------------------------- #
@dataclass
class LeituraQR:
    """Um codigo encontrado no frame (QR ou ArUco)."""

    conteudo: str
    poligono: np.ndarray            # (N, 2) float32, em pixels do frame original
    tipo: str = "qr"                # 'qr' | 'aruco'
    variante: str = "direto"        # qual pre-processamento conseguiu ler
    zona: int | None = None         # dispenser cujo recorte produziu a leitura

    @property
    def centro(self) -> tuple[float, float]:
        m = self.poligono.mean(axis=0)
        return (float(m[0]), float(m[1]))

    @property
    def caixa(self) -> tuple[int, int, int, int]:
        x1, y1 = self.poligono.min(axis=0)
        x2, y2 = self.poligono.max(axis=0)
        return (int(x1), int(y1), int(x2 - x1), int(y2 - y1))

    @property
    def lado(self) -> float:
        _, _, w, h = self.caixa
        return float(max(w, h))


# --------------------------------------------------------------------------- #
class LeitorCodigos:
    """Decodificador multi-estrategia de QR e ArUco."""

    def __init__(
        self,
        backend: str = "auto",
        escala: float = 1.0,
        melhorar_contraste: bool = True,
        cascata: bool = True,
        cascata_completa: bool = True,
        usar_qr: bool = True,
        usar_aruco: bool = True,
        dicionario_aruco: str = "DICT_4X4_50",
        ler_por_zona: bool = True,
        margem_zona_px: int = 12,
        lado_alvo_zona: int = 320,
        lado_minimo_aruco: int = 16,
        intervalo_reavaliacao: float = 1.0,
        limiar_mudanca: float = 2.0,
        intervalo_varredura: float = 1.0,
        intervalo_cascata_completa: float = 0.5,
        orcamento_ms: float = 250.0,
        intervalo_aruco: float = 0.30,
        memoria_aruco: float = 0.60,
        acumular_aruco: bool = True,
    ) -> None:
        backend = (backend or "auto").lower()
        if backend == "auto":
            backend = "pyzbar" if _PYZBAR_OK else "opencv"
        if backend == "pyzbar" and not _PYZBAR_OK:
            backend = "opencv"

        self.backend = backend
        self.escala = float(escala) if escala and escala > 0 else 1.0
        self.melhorar_contraste = melhorar_contraste
        self.cascata = cascata
        self.cascata_completa = cascata_completa
        self.usar_qr = bool(usar_qr)
        self.ler_por_zona = ler_por_zona
        self.margem_zona_px = int(margem_zona_px)
        self.lado_alvo_zona = int(lado_alvo_zona)
        self.lado_minimo_aruco = int(lado_minimo_aruco)
        self.intervalo_reavaliacao = float(intervalo_reavaliacao)
        self.limiar_mudanca = float(limiar_mudanca)
        self.intervalo_varredura = float(intervalo_varredura)
        self.intervalo_cascata_completa = float(intervalo_cascata_completa)
        self.orcamento_ms = float(orcamento_ms)
        self.intervalo_aruco = float(intervalo_aruco)
        self.memoria_aruco = float(memoria_aruco)
        self.acumular_aruco = bool(acumular_aruco)
        self._ultimo_aruco_livre = -1e9
        self._cache_aruco_livre: list[LeituraQR] = []
        self._ultima_cascata: dict[int, float] = {}
        self._rodizio = 0
        self._prazo = float("inf")
        self._cache_zona: dict[int, dict] = {}
        self.execucoes_pesadas = 0   # quantas vezes a cascata rodou de verdade
        self._ultima_varredura = 0.0

        self._cv_detector = cv2.QRCodeDetector() if backend == "opencv" else None
        self._variante_preferida: dict[int, str] = {}
        self.estatisticas: dict[str, int] = {}

        self.usar_aruco = bool(usar_aruco and _ARUCO_OK)
        self._aruco = _montar_aruco(dicionario_aruco) if self.usar_aruco else None

    # ------------------------------------------------------------------ #
    def ler(self, frame: np.ndarray, zonas=None, agora: float | None = None) -> list[LeituraQR]:
        """Le o frame. Com `zonas`, faz a busca dirigida (mais barata e mais eficaz)."""
        agora = time.monotonic() if agora is None else agora

        if zonas is None or not self.ler_por_zona or not zonas.zonas:
            leituras = self._ler_frame_inteiro(frame, agora)
        else:
            leituras = []
            # Orcamento de tempo por frame: as tentativas caras rodam enquanto
            # houver folga. Sem isso, um frame em que nada e legivel poderia
            # gastar segundos tentando todas as variantes em todas as zonas —
            # e o sistema deixaria de ser tempo real justamente quando importa.
            self._prazo = time.monotonic() + self.orcamento_ms / 1000.0

            # rodizio: a cada frame comeca por uma zona diferente, entao a folga
            # do orcamento nao fica sempre com o dispenser 1
            ordem = list(zonas.zonas)
            if ordem:
                deslocamento = self._rodizio % len(ordem)
                ordem = ordem[deslocamento:] + ordem[:deslocamento]
                self._rodizio += 1

            for zona in ordem:
                leituras += self._ler_zona_com_cache(frame, zona, agora)

            # varredura do frame inteiro de vez em quando: pega codigo fora das
            # zonas (caixa na bancada, dispenser mal calibrado) sem pagar esse
            # custo em todo frame
            if agora - self._ultima_varredura >= self.intervalo_varredura:
                self._ultima_varredura = agora
                fora = [
                    l for l in self._qr(pre.cinza(frame), "varredura")
                    if zonas.zona_do_ponto(*l.centro) is None
                ]
                leituras += fora

        leituras = _deduplicar(leituras)
        for l in leituras:
            chave = f"{l.tipo}:{l.variante}"
            self.estatisticas[chave] = self.estatisticas.get(chave, 0) + 1
        return leituras

    # ------------------------------------------------------------------ #
    def _ler_frame_inteiro(self, frame: np.ndarray, agora: float | None = None) -> list[LeituraQR]:
        agora = time.monotonic() if agora is None else agora
        g = pre.cinza(frame)
        if self.escala != 1.0:
            g = cv2.resize(g, None, fx=self.escala, fy=self.escala,
                           interpolation=cv2.INTER_AREA)
        base = pre.equalizar(g) if self.melhorar_contraste else g

        leituras = self._qr(base, "direto") if self.usar_qr else []

        if self.usar_aruco:
            # ArUco no frame inteiro custa ~70 ms contra ~30 ms do QR: rodar os
            # dois em todo frame derrubava o preview para 10 FPS. Aqui ele roda
            # em intervalo fixo (ou na hora, se o QR nao achou nada) e o
            # resultado vale por alguns decimos de segundo. Como o alvo e
            # estatico na mao de quem testa, nao se perde leitura — so custo.
            vencido = (agora - self._ultimo_aruco_livre) >= self.intervalo_aruco
            if vencido or not leituras:
                self._cache_aruco_livre = self._aruco_ler(g, "direto")
                self._ultimo_aruco_livre = agora
            if (agora - self._ultimo_aruco_livre) <= self.memoria_aruco:
                leituras += [_copiar(l) for l in self._cache_aruco_livre]

        if self.escala != 1.0:
            fator = 1.0 / self.escala
            for l in leituras:
                l.poligono = l.poligono * fator
        return leituras

    # ------------------------------------------------------------------ #
    def _ler_zona_com_cache(self, frame: np.ndarray, zona, agora: float) -> list[LeituraQR]:
        """Le a zona, reaproveitando o resultado enquanto a imagem nao mudar.

        Os dispensers sao estaticos: na maior parte dos frames o recorte da zona
        e praticamente identico ao anterior. Comparar uma miniatura 16x16 custa
        microssegundos e evita rodar toda a cascata a cada frame — e o que
        mantem o sistema em tempo real mesmo com o processamento pesado ligado.
        """
        recorte, origem, fator_base = self._recortar(frame, zona)
        if recorte is None:
            return []

        assinatura = cv2.resize(recorte, (16, 16), interpolation=cv2.INTER_AREA).astype(np.int16)
        cache = self._cache_zona.get(zona.dispenser)

        if cache is not None:
            mudanca = float(np.abs(assinatura - cache["assinatura"]).mean())
            recente = (agora - cache["tempo"]) < self.intervalo_reavaliacao
            if mudanca <= self.limiar_mudanca and recente:
                return [_copiar(l, "cache") for l in cache["leituras"]]

        # A cascata completa e cara. Se a imagem esta mudando (mao na frente da
        # camera, por exemplo), rodar tudo em cada frame derruba o FPS sem
        # ganho: aqui ela roda no maximo a cada `intervalo_cascata_completa`,
        # e nos frames intermediarios so as tentativas baratas.
        completo = (agora - self._ultima_cascata.get(zona.dispenser, -1e9)
                    >= self.intervalo_cascata_completa)
        if completo:
            self._ultima_cascata[zona.dispenser] = agora
        achadas = self._pipeline_zona(recorte, zona, origem, fator_base, completo)
        self._cache_zona[zona.dispenser] = {
            "assinatura": assinatura,
            "leituras": achadas,
            "tempo": agora,
        }
        return [_copiar(l) for l in achadas]

    def _recortar(self, frame: np.ndarray, zona):
        h, w = frame.shape[:2]
        m = self.margem_zona_px
        x1, y1 = max(0, zona.x - m), max(0, zona.y - m)
        x2, y2 = min(w, zona.x2 + m), min(h, zona.y2 + m)
        if x2 - x1 < 20 or y2 - y1 < 20:
            return None, (0, 0), 1.0

        recorte = pre.cinza(frame[y1:y2, x1:x2])
        lado = max(recorte.shape[:2])
        fator_base = 1.0
        if lado < self.lado_alvo_zona:  # zona pequena: amplia antes de decodificar
            fator_base = min(3.0, self.lado_alvo_zona / lado)
            recorte = pre.ampliar(recorte, fator_base)
        return recorte, (x1, y1), fator_base

    def _pipeline_zona(self, recorte, zona, origem, fator_base, completo=True) -> list[LeituraQR]:
        """Tentativas em ordem de custo/beneficio, parando na primeira que ler."""
        self.execucoes_pesadas += 1
        x1, y1 = origem
        preferida = self._variante_preferida.get(zona.dispenser)

        for indice, (nome, metodo, tratada, escala) in enumerate(
            self._tentativas(recorte, preferida, completo)
        ):
            # a primeira tentativa (a mais barata) sempre roda; as demais so
            # enquanto o orcamento do frame permitir
            if indice > 0 and time.monotonic() > self._prazo:
                break
            achadas = metodo(tratada, nome)
            if not achadas:
                continue

            # Parar no primeiro sucesso economiza CPU, mas esconde caixa cujo
            # QR esta tapado enquanto a vizinha e lida: a zona "deu certo" e o
            # ArUco nunca roda. Para CONTAR unidades isso subestima o estoque,
            # entao aqui a passada de ArUco e somada mesmo quando o QR ja leu.
            # `metodo is self._qr` NAO funciona: cada acesso a um metodo ligado
            # cria um objeto novo, entao a comparacao por identidade e sempre
            # falsa. Comparar pelo nome e o que de fato identifica a estrategia.
            if (self.acumular_aruco and self.usar_aruco
                    and getattr(metodo, "__name__", "") == "_qr"
                    and time.monotonic() <= self._prazo):
                extras = self._aruco_ler(tratada, nome)
                if extras:
                    achadas = achadas + extras

            self._variante_preferida[zona.dispenser] = f"{metodo.__name__}|{nome}"
            fator = 1.0 / (fator_base * escala)
            for l in achadas:
                l.poligono = l.poligono * fator + np.array([x1, y1], dtype=np.float32)
                l.zona = zona.dispenser
            return achadas

        self._variante_preferida.pop(zona.dispenser, None)
        return []

    def _tentativas(self, recorte, preferida: str | None, completo: bool = True):
        """Gera (nome, metodo, imagem, escala).

        A ordem alterna QR e ArUco de proposito: o QR direto e o mais barato,
        mas quando a imagem esta ruim o ArUco resolve mais que insistir em
        variantes de QR cada vez mais caras.
        """
        plano: list[tuple[str, object]] = []
        if self.usar_qr:
            plano.append(("direto", self._qr))
            if self.cascata:
                plano.append(("x2+adaptativo", self._qr))
        if self.usar_aruco:
            plano += [("direto", self._aruco_ler), ("x2", self._aruco_ler)]
        if not completo:
            if preferida:
                plano.sort(key=lambda t: f"{t[1].__name__}|{t[0]}" != preferida)
            yield from self._materializar(plano, recorte)
            return
        if self.cascata and self.usar_qr:
            plano += [
                ("realce", self._qr),
                ("x2+clahe", self._qr),
            ]
            if self.usar_aruco:
                plano += [("x2+adaptativo", self._aruco_ler)]
            if self.cascata_completa and self.usar_qr:
                plano += [
                    ("x3+adaptativo", self._qr),
                    ("x2+realce+adaptativo", self._qr),
                    ("bilateral+x2+adaptativo", self._qr),
                    ("gama+x2+adaptativo", self._qr),
                    ("otsu", self._qr),
                ]
                if self.usar_aruco:
                    plano += [("x2+clahe", self._aruco_ler)]

        if preferida:
            plano.sort(key=lambda t: f"{t[1].__name__}|{t[0]}" != preferida)
        yield from self._materializar(plano, recorte)

    def _materializar(self, plano, recorte):
        cache: dict[str, tuple[np.ndarray, float]] = {}
        for nome, metodo in plano:
            if nome not in cache:
                funcao = _FUNCOES.get(nome)
                if funcao is None:
                    continue
                try:
                    img = funcao(recorte)
                except cv2.error:
                    continue
                cache[nome] = (img, img.shape[1] / recorte.shape[1])
            img, escala = cache[nome]
            yield nome, metodo, img, escala

    # ------------------------------------------------------------------ #
    def _qr(self, g: np.ndarray, variante: str) -> list[LeituraQR]:
        if self.backend == "pyzbar":
            return self._qr_pyzbar(g, variante)
        return self._qr_opencv(g, variante)

    def _qr_pyzbar(self, g: np.ndarray, variante: str) -> list[LeituraQR]:
        saida: list[LeituraQR] = []
        try:
            objetos = _pyzbar.decode(g, symbols=[_pyzbar.ZBarSymbol.QRCODE])
        except Exception:
            return saida
        for obj in objetos:
            try:
                conteudo = obj.data.decode("utf-8").strip()
            except UnicodeDecodeError:
                conteudo = obj.data.decode("latin-1", errors="replace").strip()
            if not conteudo:
                continue
            pts = np.array([[p.x, p.y] for p in obj.polygon], dtype=np.float32)
            if len(pts) < 3:
                r = obj.rect
                pts = np.array(
                    [[r.left, r.top], [r.left + r.width, r.top],
                     [r.left + r.width, r.top + r.height], [r.left, r.top + r.height]],
                    dtype=np.float32,
                )
            saida.append(LeituraQR(conteudo, pts, "qr", variante))
        return saida

    def _qr_opencv(self, g: np.ndarray, variante: str) -> list[LeituraQR]:
        assert self._cv_detector is not None
        try:
            ok, textos, pontos, _ = self._cv_detector.detectAndDecodeMulti(g)
        except cv2.error:
            return []
        if not ok or pontos is None:
            return []
        saida = []
        for texto, quad in zip(textos, pontos):
            texto = (texto or "").strip()
            if texto:
                saida.append(
                    LeituraQR(texto, np.asarray(quad, dtype=np.float32), "qr", variante)
                )
        return saida

    # ------------------------------------------------------------------ #
    def _aruco_ler(self, g: np.ndarray, variante: str) -> list[LeituraQR]:
        if self._aruco is None:
            return []
        try:
            cantos, ids, _ = self._aruco.detectMarkers(g)
        except cv2.error:
            return []
        if ids is None:
            return []
        saida = []
        for quad, ident in zip(cantos, ids.flatten()):
            pts = np.asarray(quad, dtype=np.float32).reshape(-1, 2)
            lado = float(max(pts.max(axis=0) - pts.min(axis=0)))
            # Marcador minusculo quase sempre e falso positivo: textura de texto
            # ou os proprios quadrados de referencia do QR podem virar um "id".
            if lado < self.lado_minimo_aruco:
                continue
            saida.append(
                LeituraQR(f"{PREFIXO_ARUCO}{int(ident)}", pts, "aruco", variante)
            )
        return saida

    # ------------------------------------------------------------------ #
    @staticmethod
    def _zona_do_centro(leitura: LeituraQR, zonas) -> int | None:
        cx, cy = leitura.centro
        z = zonas.zona_do_ponto(cx, cy)
        return z.dispenser if z else None

    def resumo_estatisticas(self) -> str:
        if not self.estatisticas:
            return "nenhuma leitura ainda"
        total = sum(self.estatisticas.values())
        partes = [
            f"{k} {v * 100 // total}%"
            for k, v in sorted(self.estatisticas.items(), key=lambda x: -x[1])[:4]
        ]
        return ", ".join(partes)


# --------------------------------------------------------------------------- #
_FUNCOES = {
    "direto": lambda g: g,
    "x2": lambda g: pre.ampliar(g, 2),
    "x2+adaptativo": lambda g: pre.binarizar_adaptativo(pre.ampliar(g, 2)),
    "realce": pre.realcar,
    "x2+clahe": lambda g: pre.equalizar(pre.ampliar(g, 2)),
    "x3+adaptativo": lambda g: pre.binarizar_adaptativo(pre.ampliar(g, 3), 81, 9),
    "x2+realce+adaptativo": lambda g: pre.binarizar_adaptativo(pre.realcar(pre.ampliar(g, 2))),
    "bilateral+x2+adaptativo": lambda g: pre.binarizar_adaptativo(
        pre.ampliar(pre.suavizar_preservando_bordas(g), 2), 51, 5),
    "gama+x2+adaptativo": lambda g: pre.binarizar_adaptativo(
        pre.ampliar(pre.corrigir_gama(g), 2)),
    "otsu": pre.binarizar_otsu,
}


def _copiar(l: LeituraQR, variante: str | None = None) -> LeituraQR:
    return LeituraQR(
        conteudo=l.conteudo,
        poligono=l.poligono.copy(),
        tipo=l.tipo,
        variante=variante or l.variante,
        zona=l.zona,
    )


def _montar_aruco(nome_dicionario: str):
    dic_id = getattr(cv2.aruco, nome_dicionario, cv2.aruco.DICT_4X4_50)
    dicionario = cv2.aruco.getPredefinedDictionary(dic_id)
    p = cv2.aruco.DetectorParameters()
    # janela adaptativa larga: aguenta iluminacao desigual entre os dispensers
    p.adaptiveThreshWinSizeMin = 5
    p.adaptiveThreshWinSizeMax = 29
    p.adaptiveThreshWinSizeStep = 8
    # aceita marcador pequeno na imagem
    p.minMarkerPerimeterRate = 0.02
    p.maxMarkerPerimeterRate = 4.0
    # tolera contorno menos que perfeito (angulo, desfoque)
    p.polygonalApproxAccuracyRate = 0.06
    p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    p.errorCorrectionRate = 0.8
    return cv2.aruco.ArucoDetector(dicionario, p)


def dicionario_aruco(nome: str = "DICT_4X4_50"):
    dic_id = getattr(cv2.aruco, nome, cv2.aruco.DICT_4X4_50)
    return cv2.aruco.getPredefinedDictionary(dic_id)


def _deduplicar(leituras: list[LeituraQR], tolerancia: float = 20.0) -> list[LeituraQR]:
    unicas: list[LeituraQR] = []
    for l in leituras:
        cx, cy = l.centro
        repetida = False
        for u in unicas:
            ux, uy = u.centro
            if (u.conteudo == l.conteudo and abs(ux - cx) < tolerancia
                    and abs(uy - cy) < tolerancia):
                repetida = True
                break
        if not repetida:
            unicas.append(l)
    return unicas


def agrupar_em_unidades(leituras: list[LeituraQR],
                        folga: float = 1.7) -> list[list[LeituraQR]]:
    """Agrupa codigos que pertencem a MESMA caixa fisica.

    Necessario porque uma etiqueta traz tres codigos (1 QR + 2 ArUco): contar
    codigos daria tres caixas onde ha uma. A regra segue a fisica da etiqueta:

      - cada QR lido e uma caixa (o QR e unico por etiqueta);
      - cada ArUco gruda no QR mais proximo, se estiver dentro do alcance de
        uma etiqueta;
      - ArUco que sobrou (QR ilegivel naquela caixa) forma sua propria caixa,
        agrupando com os vizinhos proximos — uma etiqueta tem dois marcadores,
        entao os dois caem no mesmo grupo.

    `folga` e o alcance de agrupamento em multiplos do lado do codigo. 1.7 cobre
    a distancia do QR ate o ArUco do canto oposto na etiqueta padrao.
    """
    if not leituras:
        return []

    qrs = [l for l in leituras if l.tipo == "qr"]
    arucos = [l for l in leituras if l.tipo != "qr"]

    grupos: list[list[LeituraQR]] = [[q] for q in qrs]
    sobrando: list[LeituraQR] = []

    for marca in arucos:
        mx, my = marca.centro
        melhor, menor = None, float("inf")
        for grupo in grupos:
            qx, qy = grupo[0].centro
            alcance = folga * max(grupo[0].lado, marca.lado)
            distancia = ((mx - qx) ** 2 + (my - qy) ** 2) ** 0.5
            if distancia <= alcance and distancia < menor:
                melhor, menor = grupo, distancia
        if melhor is not None:
            melhor.append(marca)
        else:
            sobrando.append(marca)

    # ArUco sem QR por perto: agrupa entre si (ligacao simples)
    for marca in sobrando:
        mx, my = marca.centro
        destino = None
        for grupo in grupos:
            if grupo[0].tipo == "qr":
                continue
            for outro in grupo:
                ox, oy = outro.centro
                alcance = folga * max(outro.lado, marca.lado)
                if ((mx - ox) ** 2 + (my - oy) ** 2) ** 0.5 <= alcance:
                    destino = grupo
                    break
            if destino:
                break
        if destino is not None:
            destino.append(marca)
        else:
            grupos.append([marca])

    return grupos


def backend_disponivel() -> str:
    partes = ["pyzbar" if _PYZBAR_OK else "opencv(QR)"]
    if _ARUCO_OK:
        partes.append("aruco")
    return " + ".join(partes)


def aruco_disponivel() -> bool:
    return _ARUCO_OK


# compatibilidade com a versao anterior
LeitorQR = LeitorCodigos
