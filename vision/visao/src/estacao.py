"""Estacao completa: codigo + embalagem + estoque + eventos + painel.

E a versao "produto" do sistema. O main.py continua existindo como a versao
enxuta (so leitura de codigo); esta aqui junta as tres evidencias, grava a
trilha de auditoria e publica o painel.

    python src/estacao.py                    # tudo ligado
    python src/estacao.py --sem-visual       # so codigo + estoque
    python src/estacao.py --sem-estoque
    python src/estacao.py --porta 8080
    python src/estacao.py --video teste.mp4 --sem-janela

O custo pesado (reconhecer embalagem, medir pilha) so roda quando a imagem da
zona muda. Em bancada parada o loop custa poucos milissegundos por frame.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alertas import Notificador  # noqa: E402
from camera import abrir_camera, indice_para_usar, listar_cameras  # noqa: E402
from configuracao import Catalogo, MapaZonas, carregar_parametros  # noqa: E402
from contagem import ContadorDeEstoque  # noqa: E402
from detector import DetectorDispensers  # noqa: E402
from eventos import Evento, RegistroDeEventos, eventos_de_fusao  # noqa: E402
from fusao import MotorDeFusao, Veredito  # noqa: E402
from integracao_backend import PublicadorEstoque, buscar_catalogo  # noqa: E402
from interface import (  # noqa: E402
    Medidas,
    PainelApsen,
    desenhar_zonas_no_video,
    reduzir_frame,
)
from leitor_qr import LeitorCodigos, backend_disponivel  # noqa: E402
import preprocessamento as pre  # noqa: E402
from reconhecimento import ReconhecedorVisual  # noqa: E402

JANELA = "Estacao de conferencia"

CORES = {
    Veredito.OK: (80, 200, 120),
    Veredito.ERRO_POSICAO: (60, 60, 235),
    Veredito.DIVERGENCIA: (200, 60, 235),
    Veredito.NAO_CADASTRADO: (60, 60, 235),
    Veredito.VAZIO: (150, 150, 150),
    Veredito.INDETERMINADO: (60, 190, 240),
}


class Estacao:
    """Um posto de conferencia: uma camera, N dispensers."""

    def __init__(self, args) -> None:
        self.args = args
        self.parametros = carregar_parametros()

        # O backend Apsen manda no mapeamento dispenser -> medicamento. O
        # arquivo local vira reserva, usada so quando ele nao responde: a
        # estacao nao pode parar de conferir porque a rede caiu, mas tambem nao
        # pode ficar com uma verdade propria concorrente quando ele esta no ar.
        cfg_backend = {} if getattr(args, "sem_backend", False) else \
            (self.parametros.get("backend", {}) or {})
        self.backend_url = cfg_backend.get("url", "") if cfg_backend.get("ativo") else ""
        self.backend_timeout = float(cfg_backend.get("timeout_segundos", 5.0))
        self.intervalo_catalogo = float(cfg_backend.get("intervalo_catalogo", 60.0))
        self._proxima_recarga = 0.0

        self.catalogo, self.origem_catalogo = self._carregar_catalogo()
        self.zonas = MapaZonas.carregar()

        det_cfg = self.parametros.get("deteccao", {})
        ale_cfg = self.parametros.get("alertas", {})
        self.ui_cfg = self.parametros.get("interface", {})

        # Resolucao de trabalho. Camera de celular entrega 1080p (as vezes mais),
        # e todo o pipeline — captura, copia, deteccao, desenho — paga por cada
        # pixel. Reduzir aqui e a alavanca mais direta de FPS, e quase nao custa
        # precisao: o que importa e o tamanho do CODIGO em pixels, e a 720p um
        # QR de 40 mm continua com folga.
        self.largura_processamento = int(det_cfg.get("largura_processamento", 0))

        self.detector = DetectorDispensers(
            catalogo=self.catalogo,
            zonas=self.zonas,
            leitor=LeitorCodigos(
                backend=det_cfg.get("backend", "auto"),
                cascata=bool(det_cfg.get("cascata", True)),
                usar_aruco=bool(det_cfg.get("usar_aruco", True)),
                ler_por_zona=True,
            ),
            frames_para_confirmar=int(ale_cfg.get("frames_para_confirmar", 3)),
            cooldown_segundos=float(ale_cfg.get("cooldown_segundos", 10.0)),
            memoria_segundos=float(ale_cfg.get("memoria_segundos", 2.0)),
        )

        self.reconhecedor = None
        if not args.sem_visual:
            self.reconhecedor = ReconhecedorVisual()
            quantos = self.reconhecedor.carregar()
            if quantos == 0:
                print("[aviso] nenhuma embalagem cadastrada; rode src/cadastrar_visual.py")
                self.reconhecedor = None
            else:
                print(f"  embalagens cadastradas .. {quantos}")

        self.contador = None
        if not args.sem_estoque:
            self.contador = ContadorDeEstoque()
            self.contador.carregar()

        self.fusao = MotorDeFusao(self.catalogo, exigir_visual=args.exigir_visual)
        self.registro = RegistroDeEventos(estacao=args.estacao)
        self.notificador = Notificador.a_partir_dos_parametros(self.parametros)

        # Publicacao do nivel para o backend Apsen. Fica desligada se
        # parametros.json nao trouxer 'backend.ativo' — a estacao continua
        # funcionando sozinha, gravando em eventos.db como sempre fez.
        # getattr porque os testes montam um Namespace so com o que interessa
        # aquele caso; uma flag nova nao pode quebrar quem ja construia Estacao.
        self.publicador = PublicadorEstoque.a_partir_dos_parametros(
            {} if getattr(args, "sem_backend", False) else self.parametros,
            estacao=self.registro.estacao,
        )

        # cache: reconhecimento e contagem so refazem quando a zona muda
        self._assinaturas: dict[int, np.ndarray] = {}
        self._identificacoes: dict[int, object] = {}
        self._niveis: dict[int, object] = {}
        self._ultimo_veredito: dict[int, str] = {}
        self._ultimo_estoque_gravado: dict[int, int | None] = {}
        self._camera_cega = False
        self._frames_cegos = 0

    # ------------------------------------------------------------------ #
    def _carregar_catalogo(self) -> tuple[Catalogo, str]:
        """Backend primeiro, arquivo local como reserva."""
        if self.backend_url:
            itens = buscar_catalogo(self.backend_url, self.backend_timeout)
            if itens:
                try:
                    return Catalogo.de_itens(itens), "backend"
                except ValueError as exc:
                    # Mapeamento ambiguo (dispenser, SKU ou ArUco repetido). Cair
                    # para o local aqui e melhor que rodar com catalogo pela
                    # metade, mas tem que ser barulhento: alguem precisa corrigir
                    # a tela /visao.
                    print(f"  [backend] catalogo recusado ({exc}); usando arquivo local")
        return Catalogo.carregar(), "arquivo local"

    def recarregar_catalogo(self) -> None:
        """Repuxa o mapeamento do backend de tempos em tempos.

        Sem isso, trocar o medicamento de um dispenser na tela /visao so teria
        efeito depois de reiniciar a estacao — e uma estacao que precisa de
        reinicio para acompanhar o cadastro acaba rodando desatualizada.
        """
        if not self.backend_url:
            return
        agora = time.monotonic()
        if agora < self._proxima_recarga:
            return
        self._proxima_recarga = agora + self.intervalo_catalogo

        itens = buscar_catalogo(self.backend_url, self.backend_timeout)
        if not itens:
            return
        try:
            novo = Catalogo.de_itens(itens)
        except ValueError:
            return
        if {m.qr: (m.nome, m.dispenser) for m in novo.medicamentos} == \
           {m.qr: (m.nome, m.dispenser) for m in self.catalogo.medicamentos}:
            return

        self.catalogo = novo
        self.detector.catalogo = novo
        self.fusao.catalogo = novo
        self.origem_catalogo = "backend"
        print("  [backend] catalogo atualizado: "
              + ", ".join(f"D{m.dispenser}={m.nome}" for m in novo.medicamentos))

    # ------------------------------------------------------------------ #
    def _camera_utilizavel(self, frame: np.ndarray) -> bool:
        """Detecta camera tapada, desfocada ou apagada.

        Sem esta checagem o sistema falha em SILENCIO: lente coberta produz
        zonas sem codigo e sem textura, o que o pipeline interpreta como
        "dispenser vazio" — o estado mais tranquilizador possivel. Sistema de
        seguranca que falha em silencio e pior que sistema nenhum, entao aqui
        a perda de imagem vira alarme explicito, nao ausencia de alarme.
        """
        g = pre.cinza(frame)
        pequeno = cv2.resize(g, (160, 120), interpolation=cv2.INTER_AREA)
        nitidez = pre.nitidez(pequeno)
        brilho = pre.brilho(pequeno)
        contraste = pre.contraste(pequeno)

        cega = nitidez < 8.0 or contraste < 6.0 or brilho < 12 or brilho > 245
        self._frames_cegos = self._frames_cegos + 1 if cega else 0

        if self._frames_cegos >= 15 and not self._camera_cega:
            self._camera_cega = True
            detalhe = (f"camera sem imagem utilizavel (nitidez {nitidez:.0f}, "
                       f"contraste {contraste:.0f}, brilho {brilho:.0f}) — "
                       "lente tapada, fora de foco ou luz apagada")
            print(f"  [CAMERA] {detalhe}")
            self.registro.registrar(Evento(tipo="falha", veredito="NAO_CADASTRADO",
                                           detalhe=detalhe, confianca=1.0))
        elif self._frames_cegos == 0 and self._camera_cega:
            self._camera_cega = False
            print("  [CAMERA] imagem recuperada")
            self.registro.registrar(Evento(tipo="sistema",
                                           detalhe="imagem da camera recuperada"))
        return not self._camera_cega

    # ------------------------------------------------------------------ #
    def _mudou(self, frame: np.ndarray, zona) -> bool:
        h, w = frame.shape[:2]
        x1, y1 = max(0, zona.x), max(0, zona.y)
        x2, y2 = min(w, zona.x2), min(h, zona.y2)
        if x2 - x1 < 10 or y2 - y1 < 10:
            return False
        recorte = frame[y1:y2, x1:x2]
        if recorte.ndim == 3:
            recorte = cv2.cvtColor(recorte, cv2.COLOR_BGR2GRAY)
        assinatura = cv2.resize(recorte, (16, 16), interpolation=cv2.INTER_AREA).astype(np.int16)

        anterior = self._assinaturas.get(zona.dispenser)
        self._assinaturas[zona.dispenser] = assinatura
        if anterior is None:
            return True
        return float(np.abs(assinatura - anterior).mean()) > 2.0

    def _recorte(self, frame: np.ndarray, zona) -> np.ndarray | None:
        h, w = frame.shape[:2]
        x1, y1 = max(0, zona.x), max(0, zona.y)
        x2, y2 = min(w, zona.x2), min(h, zona.y2)
        if x2 - x1 < 20 or y2 - y1 < 20:
            return None
        return frame[y1:y2, x1:x2]

    # ------------------------------------------------------------------ #
    def reduzir(self, frame: np.ndarray) -> np.ndarray:
        """Leva o frame a resolucao de trabalho, se configurada."""
        return reduzir_frame(frame, self.largura_processamento)

    # ------------------------------------------------------------------ #
    def processar(self, frame: np.ndarray):
        self.recarregar_catalogo()   # barato: so age quando o intervalo vence

        if not self._camera_utilizavel(frame):
            # nao produz veredito nenhum enquanto a imagem nao presta: melhor
            # ficar sem informacao do que publicar "tudo vazio, tudo bem"
            return None, []

        resultado = self.detector.processar_frame(frame)
        zonas = resultado.zonas or self.zonas

        # lista, nao uma so: varias caixas do mesmo dispenser precisam ser
        # contadas e classificadas juntas
        por_zona: dict[int, list] = {}
        for oc in resultado.ocorrencias:
            if oc.dispenser_detectado is not None:
                por_zona.setdefault(oc.dispenser_detectado, []).append(oc)

        for zona in zonas.zonas:
            if not self._mudou(frame, zona):
                continue
            recorte = self._recorte(frame, zona)
            if recorte is None:
                continue
            if self.reconhecedor is not None:
                self._identificacoes[zona.dispenser] = self.reconhecedor.identificar(recorte)
            if self.contador is not None:
                self._niveis[zona.dispenser] = self.contador.medir(frame, zona)

        vereditos = self.fusao.avaliar_frame(zonas, por_zona, self._identificacoes)
        self._publicar(vereditos)
        return resultado, vereditos

    # ------------------------------------------------------------------ #
    def _publicar(self, vereditos) -> None:
        """Grava evento so quando o estado muda — log de mudanca, nao de frame."""
        for r in vereditos:
            nivel = self._niveis.get(r.dispenser)
            self.registro.atualizar_estado(
                dispenser=r.dispenser,
                veredito=r.veredito.value,
                sku=r.medicamento.qr if r.medicamento else None,
                medicamento=r.medicamento.nome if r.medicamento else None,
                confianca=r.confianca,
                caixas=nivel.caixas if nivel else None,
                fracao=nivel.fracao if nivel else None,
                unidades=r.total_unidades or None,
                precisa_repor=bool(nivel and nivel.precisa_repor),
            )

            chave = (f"{r.veredito.value}|{r.medicamento.qr if r.medicamento else '-'}"
                     f"|{r.quantidade_certa}/{r.quantidade_errada}")
            if self._ultimo_veredito.get(r.dispenser) != chave:
                self._ultimo_veredito[r.dispenser] = chave
                evento = eventos_de_fusao([r], self._niveis)[0]
                self.registro.registrar(evento)
                if r.critico:
                    print(f"  [{r.veredito.value}] {r.mensagem()}")

            if nivel is not None:
                anterior = self._ultimo_estoque_gravado.get(r.dispenser, "?")
                if nivel.caixas != anterior:
                    self._ultimo_estoque_gravado[r.dispenser] = nivel.caixas
                    self.registro.registrar(Evento(
                        tipo="estoque", dispenser=r.dispenser,
                        sku=r.medicamento.qr if r.medicamento else None,
                        medicamento=r.medicamento.nome if r.medicamento else None,
                        detalhe=nivel.mensagem(), dados=nivel.para_dict(),
                    ))

            # Fora do bloco do nivel de proposito: a estacao precisa reportar
            # mesmo sem contagem de pilha, porque a contagem por CODIGO (quantas
            # etiquetas aparecem na zona) nao depende de calibracao nenhuma.
            # Chamado a cada frame, nao so na mudanca: a histerese do publicador
            # precisa ver o valor se repetir para considera-lo estavel.
            self.publicador.atualizar(
                dispenser=r.dispenser,
                caixas=nivel.caixas if nivel else None,
                confianca=nivel.confianca if nivel else 0.0,
                veredito=r.veredito.value,
                sku=r.medicamento.qr if r.medicamento else None,
                medicamento=r.medicamento.nome if r.medicamento else None,
                detalhe=r.mensagem(),
                # Quantas unidades DO MEDICAMENTO CERTO foram efetivamente
                # identificadas na zona. Unidade errada nao entra: ela ja vira
                # ERRO_POSICAO, e somar ao estoque o que nao pertence ali seria
                # transformar um erro de separacao em estoque valido.
                unidades_vistas=r.quantidade_certa,
            )

    # ------------------------------------------------------------------ #
    def desenhar(self, frame: np.ndarray, vereditos, fps: float) -> np.ndarray:
        img = frame.copy()
        zonas = self.zonas.para_resolucao(img.shape[1], img.shape[0])
        fonte = cv2.FONT_HERSHEY_SIMPLEX

        for r in vereditos:
            zona = zonas.por_dispenser(r.dispenser)
            if zona is None:
                continue
            cor = CORES.get(r.veredito, (150, 150, 150))
            cv2.rectangle(img, (zona.x, zona.y), (zona.x2, zona.y2), cor, 2, cv2.LINE_AA)

            titulo = f"D{r.dispenser} {r.veredito.value}"
            if r.total_unidades:
                titulo += f"  {r.total_unidades}un"
            linhas = [titulo]
            if r.itens:
                linhas += [f"{q}x {nome[:20]}" for nome, q in
                           sorted(r.itens.items(), key=lambda x: -x[1])[:3]]
            else:
                linhas.append((r.medicamento.nome if r.medicamento else "-")[:24])

            altura_caixa = 20 * len(linhas) + 8
            cv2.rectangle(img, (zona.x, zona.y), (zona.x + 232, zona.y + altura_caixa),
                          (18, 18, 18), -1)
            for i, texto in enumerate(linhas):
                cv2.putText(img, texto, (zona.x + 6, zona.y + 19 + i * 20), fonte,
                            0.5 if i == 0 else 0.42,
                            cor if i == 0 else (225, 225, 225),
                            2 if i == 0 else 1, cv2.LINE_AA)

            nivel = self._niveis.get(r.dispenser)
            if nivel is not None:
                largura = int(zona.largura * min(1.0, nivel.fracao))
                base = zona.y2 - 12
                cv2.rectangle(img, (zona.x, base), (zona.x2, base + 8), (30, 30, 30), -1)
                cor_barra = (60, 60, 235) if nivel.precisa_repor else (247, 169, 90)
                cv2.rectangle(img, (zona.x, base), (zona.x + largura, base + 8),
                              cor_barra, -1)
                rotulo = "-" if nivel.caixas is None else f"{nivel.caixas} cx"
                cv2.putText(img, rotulo, (zona.x + 6, base - 4), fonte, 0.45,
                            (235, 235, 235), 1, cv2.LINE_AA)

        criticos = [r for r in vereditos if r.critico]
        if criticos:
            faixa = img[0:44, :]
            cv2.addWeighted(np.full_like(faixa, CORES[criticos[0].veredito]), 0.85,
                            faixa, 0.15, 0, faixa)
            cv2.putText(img, criticos[0].mensagem()[:95], (14, 29), fonte, 0.6,
                        (255, 255, 255), 2, cv2.LINE_AA)

        cv2.putText(img, f"{fps:4.1f} FPS", (10, img.shape[0] - 12), fonte, 0.45,
                    (235, 235, 235), 1, cv2.LINE_AA)
        return img


# --------------------------------------------------------------------------- #
def main() -> int:
    p = argparse.ArgumentParser(description="Estacao completa de conferencia.")
    p.add_argument("--camera", type=str, default=None, help="numero da camera ou parte do nome (ex: camo)")
    p.add_argument("--video", type=str, default=None)
    p.add_argument("--estacao", type=str, default=None, help="nome desta bancada")
    p.add_argument("--porta", type=int, default=8000)
    p.add_argument("--sem-janela", action="store_true")
    p.add_argument("--sem-painel", action="store_true")
    p.add_argument("--sem-visual", action="store_true")
    p.add_argument("--sem-estoque", action="store_true")
    p.add_argument("--sem-backend", action="store_true",
                   help="nao publica o estoque medido no backend Apsen")
    p.add_argument("--exigir-visual", action="store_true",
                   help="so aceita como conferido quando codigo E embalagem concordam")
    args = p.parse_args()

    print("=" * 70)
    print("ESTACAO DE CONFERENCIA")
    print(f"  leitor de codigo ........ {backend_disponivel()}")
    estacao = Estacao(args)
    print(f"  dispensers .............. {len(estacao.zonas.zonas)}")
    print(f"  banco de eventos ........ {estacao.registro.caminho}")
    print(f"  backend Apsen ........... "
          f"{estacao.publicador.url if estacao.publicador.ativo else 'desligado'}")
    print(f"  catalogo ................ {estacao.origem_catalogo}")
    for m in estacao.catalogo.medicamentos:
        print(f"      D{m.dispenser} = {m.nome}  (qr {m.qr}"
              + (f", aruco {m.aruco}" if m.aruco is not None else "") + ")")

    servidor = None
    if not args.sem_painel:
        from api import ServidorPainel

        servidor = ServidorPainel(estacao.registro, args.porta)
        print(f"  painel .................. {servidor.iniciar()}")
    print("=" * 70)

    cam_cfg = estacao.parametros.get("camera", {})
    if args.video:
        cap = cv2.VideoCapture(args.video)
        if not cap.isOpened():
            raise SystemExit(f"Nao consegui abrir {args.video}")
    else:
        indice = indice_para_usar(args.camera, cam_cfg)
        cap = abrir_camera(indice, int(cam_cfg.get("largura", 1280)),
                           int(cam_cfg.get("altura", 720)),
                           int(cam_cfg.get("fps", 30)), ajustes=cam_cfg)
        if cap is None:
            encontradas = "\n".join(f"  {c}" for c in listar_cameras()) or "  nenhuma"
            raise SystemExit(f"Camera indisponivel. Encontradas:\n{encontradas}\n"
                             "Escolha com: python src/camera.py")

    estacao.registro.registrar(Evento(tipo="sistema", detalhe="estacao iniciada"))

    ui_cfg = estacao.ui_cfg
    painel = PainelApsen(
        largura=int(ui_cfg.get("largura", 1280)),
        altura=int(ui_cfg.get("altura", 720)),
        estacao=estacao.registro.estacao,
    )
    medidas = Medidas()
    tempos: list[float] = []
    tela_cheia = bool(ui_cfg.get("tela_cheia", False))
    pausado = False
    ultimo_pesadas = 0
    janela_criada = False

    try:
        while True:
            inicio = time.monotonic()

            if not pausado:
                ok, bruto = cap.read()
                if not ok:
                    print("fim do video" if args.video else "falha na camera")
                    break
                frame = estacao.reduzir(bruto)
                t_captura = time.monotonic()

                _, vereditos = estacao.processar(frame)
                t_deteccao = time.monotonic()

                medidas.captura_ms = (t_captura - inicio) * 1000
                medidas.deteccao_ms = (t_deteccao - t_captura) * 1000
                medidas.frames += 1
                pesadas = estacao.detector.leitor.execucoes_pesadas
                medidas.reavaliacoes += 1 if pesadas > ultimo_pesadas else 0
                ultimo_pesadas = pesadas

                for r in vereditos:
                    if getattr(r, "critico", False):
                        painel.registrar_alerta(r.mensagem())
                        break

            if args.sem_janela:
                continue

            t_ui = time.monotonic()
            zonas_vivas = estacao.zonas.para_resolucao(frame.shape[1], frame.shape[0])
            video = desenhar_zonas_no_video(frame.copy(), vereditos, zonas_vivas)
            tela = painel.compor(video, vereditos, estacao.catalogo, medidas,
                                 estacao._niveis, estacao._camera_cega)
            medidas.interface_ms = (time.monotonic() - t_ui) * 1000

            tempos.append(time.monotonic() - inicio)
            if len(tempos) > 30:
                tempos.pop(0)
            media = sum(tempos) / len(tempos)
            medidas.fps = 1.0 / media if media > 0 else 0.0

            try:
                if not janela_criada:
                    cv2.namedWindow(JANELA, cv2.WINDOW_NORMAL)
                    cv2.resizeWindow(JANELA, painel.largura, painel.altura)
                    if tela_cheia:
                        cv2.setWindowProperty(JANELA, cv2.WND_PROP_FULLSCREEN,
                                              cv2.WINDOW_FULLSCREEN)
                    janela_criada = True
                cv2.imshow(JANELA, tela)
            except cv2.error:
                args.sem_janela = True
                continue

            k = cv2.waitKey(1) & 0xFF
            # So ESC encerra. 'q' saia daqui porque e uma letra que escapa ao
            # digitar em outra janela: bastava a janela da estacao estar com o
            # foco para a conferencia inteira cair no meio de uma operacao, sem
            # nada no log dizendo o que houve. Fechar por engano um sistema que
            # esta contando estoque e caro demais para custar uma tecla.
            if k == 27:
                break
            if k in (ord("p"), ord("P")):
                pausado = not pausado
            elif k in (ord("f"), ord("F")):
                tela_cheia = not tela_cheia
                cv2.setWindowProperty(
                    JANELA, cv2.WND_PROP_FULLSCREEN,
                    cv2.WINDOW_FULLSCREEN if tela_cheia else cv2.WINDOW_NORMAL)
            elif k in (ord("e"), ord("E")):
                from configuracao import RAIZ
                from datetime import datetime as _dt

                destino = RAIZ / "logs" / f"tela_{_dt.now():%Y%m%d_%H%M%S}.png"
                destino.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(destino), tela)
                print(f"print salvo em {destino}")

    except KeyboardInterrupt:
        print("\nencerrando")
    finally:
        estacao.registro.registrar(Evento(tipo="sistema", detalhe="estacao encerrada"))
        cap.release()
        if servidor:
            servidor.parar()
        estacao.publicador.fechar()
        estacao.notificador.fechar()
        estacao.registro.fechar()
        if not args.sem_janela:
            try:
                cv2.destroyAllWindows()
            except cv2.error:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
