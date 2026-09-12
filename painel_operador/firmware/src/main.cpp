#include "display.h"
#include <lvgl.h>
#include <string.h>
#include <stdio.h>
#include <ArduinoJson.h>
#include <time.h>
#include <SPI.h>
#include <SD.h>

#define NUM_DISPENSERS 8
#define MAX_ORDENS 5
#define MAX_OPERADORES 5

// ============================================================
// Tamanho dos campos que chegam do computador central
// ============================================================
// Nao sao numeros redondos escolhidos a esmo: no central, `ordens.os_id` e
// VARCHAR(60) e `ordens.descricao` e VARCHAR(200) (central-computer/
// database.py). O os_id tem a forma {template_id}-{AAAAMMDDTHHMMSS}-{6 hex} —
// `OS-INFECTO-01-20260909T143012-A1B2C3` tem 36 caracteres, e o template_id
// varia de tamanho.
//
// Com os 16 bytes de antes o id era truncado na copia, e o sintoma nao era
// tela feia: dois templates de nome parecido passavam a ter o MESMO id
// truncado, o set_status ia para a ordem errada e o push de status do backend
// casava com a linha errada no strcmp. Corrupcao silenciosa.
#define MAX_OS_ID_LEN 64          // 63 + terminador: cobre VARCHAR(60) com folga
#define MAX_DESTINO_LEN 96        // recorte da descricao (VARCHAR(200) na origem)
#define MAX_STATUS_LEN 20         // maior valor em uso: "Em Processo" (11)
#define MAX_LOTE_LEN (MAX_OS_ID_LEN + 8) // "LOT-" + o os_id inteiro
// Pior caso de uma OS que use a celula inteira: 8 itens x ("nome" ate 31, o
// limite do catalogo, + '|' + qtd 3 digitos) + 7 separadores = 287 bytes. Os
// 320 dao a folga que o laco de montagem reserva para as reticencias, de modo
// que 8 medicamentos SEMPRE cabem inteiros e o truncamento fica sendo o que
// deve ser: caminho de excecao, nao o caso normal.
#define MAX_ITENS_RESUMO_LEN 320

// SD Card (TF slot ESP32-8048S070)
#define SD_CS 10
#define SD_MOSI 11
#define SD_SCK 12
#define SD_MISO 13
static bool sd_ok = false;

// ============================================================
// Fuso horario (usado por settimeofday, ver sync_time_with_backend)
// ============================================================
#define TZ_STRING "<-03>3"

// URL do dashboard web, usada so para gerar o QR code da tela de relatorio
// (o ESP32 em si nao acessa rede — comunicacao com o backend e via Serial).
const char *WEB_DASHBOARD_URL = "http://192.168.15.16:5000";

// ============================================================
// Data Model
// ============================================================
// O display NAO guarda credencial. O campo `senha` saiu daqui, do
// /operadores.json do cartao SD e da resposta de `get_operadores`: quem confere
// o PIN e o backend, por `cmd: validar_operador` (ver validar_pin_backend).
// Guardar o PIN aqui — mesmo hasheado — seria publicar a lista inteira para
// quem tirar o cartao SD, e um PIN de 4 digitos tem 10 mil candidatos: hash
// nao protege entrada que da para enumerar.
struct Operador
{
    char nome[32];
    bool ativo;
};

static Operador operadores[MAX_OPERADORES] = {};
static int num_operadores = 0;

struct Dispenser
{
    char nome[32];
    int quantidade;
    int capacidade;
    int minimo;
    char lote[24];
    char validade[12]; // "YYYY-MM-DD"
};

struct OrdemExpedicao
{
    char id[MAX_OS_ID_LEN];
    char lote[MAX_LOTE_LEN];
    char itens[MAX_ITENS_RESUMO_LEN];
    char destino[MAX_DESTINO_LEN];
    char status[MAX_STATUS_LEN];
    // origem='central': espelho do computador central, SO-LEITURA. Quem executa
    // e a celula; o backend recusa qualquer escrita sobre ela.
    bool central;
    unsigned long tempo_inicio;
    unsigned long tempo_acumulado;
    char hora_inicio[20];
};

static struct
{
    int operador_logado;
    bool logado;

    Dispenser dispensers[NUM_DISPENSERS];

    OrdemExpedicao ordens[MAX_ORDENS];
    int num_ordens;
    int ordem_atual;

    bool maquina_ok;
    char status_msg[64];
    char hora[10];
    bool online;
} app = {
    -1, false, {}, {}, 0, -1, true, "Operacional", "08:45", true};

// ============================================================
// Catalogo de medicamentos (vindo do backend)
// ============================================================
#define MAX_CATALOGO 100
static char catalogo[MAX_CATALOGO][32];
static int num_catalogo = 0;
static int disp_edit_slot = -1;

// ============================================================
// Copia limitada, com truncamento VISIVEL
// ============================================================
// Copia `src` em `dst` (capacidade `cap`, terminador incluido). Quando nao
// cabe, o resultado termina em "..." — truncar de proposito e deixar rastro na
// tela, nunca cortar em silencio e deixar quem le achar que viu o valor
// inteiro. Devolve true se coube por completo.
static bool copy_trunc(char *dst, size_t cap, const char *src)
{
    if (cap == 0)
        return false;
    if (!src)
        src = "";
    size_t n = strlen(src);
    if (n < cap)
    {
        memcpy(dst, src, n + 1);
        return true;
    }
    size_t keep = cap - 1;
    if (keep > 3)
    {
        memcpy(dst, src, keep - 3);
        memcpy(dst + keep - 3, "...", 3);
    }
    else
    {
        memcpy(dst, src, keep);
    }
    dst[keep] = '\0';
    return false;
}

// Estado terminal: a ordem acabou, de um jeito ou de outro, e o slot dela pode
// ser reciclado. "Erro" e "Cancelado" entraram junto com o espelho do central —
// sem eles aqui, uma OS que a celula abortou prenderia um dos 5 slots do
// display para sempre, e depois de cinco abortos a lista pararia de aceitar
// ordem nova sem nada no log dizendo por que.
static bool ordem_terminal(const char *status)
{
    return strcmp(status, "Pronto") == 0 ||
           strcmp(status, "Erro") == 0 ||
           strcmp(status, "Cancelado") == 0;
}

// Traduz o vocabulario de status do backend para o do painel. Os dois ultimos
// ("Erro" e "Cancelado") sao novos: chegam de OS que a celula abortou. Sem
// eles a ordem ficaria parada em "Aguardando" no display para sempre, com o
// operador esperando uma execucao que ja terminou.
static const char *status_backend_para_painel(const char *st)
{
    if (strcmp(st, "Em Processo") == 0)
        return "Separando";
    if (strcmp(st, "Pausado") == 0)
        return "Pausado";
    if (strcmp(st, "Concluido") == 0)
        return "Pronto";
    if (strcmp(st, "Erro") == 0)
        return "Erro";
    if (strcmp(st, "Cancelado") == 0)
        return "Cancelado";
    return "Aguardando";
}

// ============================================================
// Pages
// ============================================================
enum AppPage
{
    PAGE_SCREENSAVER,
    PAGE_LOGIN,
    PAGE_DASHBOARD,
    PAGE_DISPENSERS,
    PAGE_RELATORIO,
    PAGE_STATUS
};

static AppPage currentPage = PAGE_SCREENSAVER;

#define SCREENSAVER_TIMEOUT_MS 60000
unsigned long last_touch_time = 0;

// ============================================================
// Prototypes
// ============================================================
void show_page(AppPage page);
void build_ui();
void update_all_ui();
void update_dashboard_ui();
void update_dispensers_ui();
void update_relatorio_ui();
void update_status_ui();
static void get_datetime(char *buf, size_t len);
static void concluir_ordem(int idx);
static void fetch_ordens_api();
static void api_update_status(const char *numero_os, const char *novo_status);
static void handle_push_ordem_status(JsonObject doc);
static void handle_push_dispensers(JsonObject doc);
static void fetch_catalogo_api();
static void fetch_operadores_api();
static void api_update_dispenser_med(int slot, const char *nome);
static void api_log_historico(const char *operador, const char *acao, const char *detalhes);
static void save_operadores_to_sd();
static void load_operadores_from_sd();
static void queue_pending_action(const char *type, const char *p1, const char *p2);
static void process_pending_actions();
static void show_med_selection();
static void disp_card_click_cb(lv_event_t *e);
static void do_login(int op_idx);
static int validar_pin_backend(const char *pin);
static void serial2_send(const char *json);
static void serial2_send_evento_ordem_concluida(const OrdemExpedicao &o);
static int dias_para_vencer(const char *validade);
static void api_post_desvio(const char *numero_os, const char *tipo, const char *descricao);
static bool serial_request(const char *cmd_json, const char *expect_resp, JsonDocument &out, unsigned long timeout_ms = 800);
static void sync_time_with_backend();

static bool force_fetch_ordens = false;

// ============================================================
// UI Pointers
// ============================================================
static lv_obj_t *ui_screen = nullptr;
static lv_obj_t *ui_header = nullptr;
static lv_obj_t *ui_body = nullptr;
static lv_obj_t *page_screensaver = nullptr;

static lv_obj_t *ui_title_label = nullptr;
static lv_obj_t *ui_time_label = nullptr;
static lv_obj_t *ui_net_badge = nullptr;
static lv_obj_t *ui_operator_label = nullptr;
static lv_obj_t *ui_menu_btn = nullptr;
static lv_obj_t *ui_menu_panel = nullptr;
static lv_obj_t *ui_menu_overlay = nullptr;

static lv_obj_t *page_login = nullptr;
static lv_obj_t *page_dashboard = nullptr;
static lv_obj_t *page_dispensers = nullptr;
static lv_obj_t *page_relatorio = nullptr;
static lv_obj_t *page_status = nullptr;

// Login
static lv_obj_t *login_ta = nullptr;
static lv_obj_t *login_msg = nullptr;

// Dashboard - lista de ordens (painel esquerdo)
static lv_obj_t *dash_list_panel = nullptr;
static lv_obj_t *dash_rows[MAX_ORDENS] = {};
static lv_obj_t *dash_row_id[MAX_ORDENS] = {};
static lv_obj_t *dash_row_itens[MAX_ORDENS] = {};
static lv_obj_t *dash_row_btn[MAX_ORDENS] = {};
static lv_obj_t *dash_row_btn_lbl[MAX_ORDENS] = {};
// Ocupa o lugar do botao nas linhas de ordem espelhada: o operador precisa
// entender que a ordem e do computador central ANTES de clicar, nao depois.
static lv_obj_t *dash_row_tag[MAX_ORDENS] = {};
// Mapeia linha visivel (0..MAX_ORDENS-1, fixa) -> indice real em app.ordens[]
// (muda a cada refresh, conforme a lista e compactada).
static int dash_row_ordem_idx[MAX_ORDENS];
// Dashboard - ordem ativa (substituindo lista)
static lv_obj_t *dash_active_panel = nullptr;
static lv_obj_t *dash_ordem_id = nullptr;
static lv_obj_t *dash_ordem_itens = nullptr;
static lv_obj_t *dash_ordem_dest = nullptr;
static lv_obj_t *dash_timer = nullptr;
static lv_obj_t *dash_btn_concluir = nullptr;
static lv_obj_t *dash_btn_pausar = nullptr;
// Dashboard - painel direito
static lv_obj_t *dash_machine_icon = nullptr;
static lv_obj_t *dash_machine_msg = nullptr;
static lv_obj_t *dash_disp_name[NUM_DISPENSERS] = {};
static lv_obj_t *dash_disp_bar[NUM_DISPENSERS] = {};
static lv_obj_t *dash_disp_pct[NUM_DISPENSERS] = {};

// Dispensers
static lv_obj_t *disp_card[NUM_DISPENSERS] = {};
static lv_obj_t *disp_nome_lbl[NUM_DISPENSERS] = {};
static lv_obj_t *disp_qtd_lbl[NUM_DISPENSERS] = {};
static lv_obj_t *disp_bar[NUM_DISPENSERS] = {};
static lv_obj_t *disp_status_lbl[NUM_DISPENSERS] = {};
static lv_obj_t *disp_validade_lbl[NUM_DISPENSERS] = {};

// Dispenser Edit - PIN popup
static lv_obj_t *disp_pin_overlay = nullptr;
static lv_obj_t *disp_pin_ta = nullptr;
static lv_obj_t *disp_pin_msg = nullptr;
static lv_obj_t *disp_pin_title = nullptr;

// Dispenser Edit - Med selection popup
static lv_obj_t *disp_med_overlay = nullptr;
static lv_obj_t *disp_med_list = nullptr;
static lv_obj_t *disp_med_title = nullptr;

// Relatorio
static lv_obj_t *rel_qr = nullptr;
static lv_obj_t *rel_total_lbl = nullptr;
static lv_obj_t *rel_concl_lbl = nullptr;
static lv_obj_t *rel_pend_lbl = nullptr;
static lv_obj_t *rel_log_list = nullptr;
#define REL_LOG_MAX 10
static lv_obj_t *rel_log_rows[REL_LOG_MAX] = {};
static lv_obj_t *rel_log_id[REL_LOG_MAX] = {};
static lv_obj_t *rel_log_itens[REL_LOG_MAX] = {};
static lv_obj_t *rel_log_status[REL_LOG_MAX] = {};

// Status
static lv_obj_t *st_icon = nullptr;
static lv_obj_t *st_title = nullptr;
static lv_obj_t *st_msg = nullptr;
static lv_obj_t *st_net = nullptr;

// ============================================================
// Styles
// ============================================================
static lv_style_t sty_header;
static lv_style_t sty_panel;
static lv_style_t sty_row;
static lv_style_t sty_badge_ok;
static lv_style_t sty_badge_alert;
static lv_style_t sty_badge_info;
static lv_style_t sty_badge_pending;
static lv_style_t sty_menu_btn;
static lv_style_t sty_menu_panel;
static lv_style_t sty_btn_primary;
static lv_style_t sty_btn_danger;
static lv_style_t sty_btn_warning;

static void init_badge(lv_style_t *s, uint32_t bg, uint32_t text)
{
    lv_style_init(s);
    lv_style_set_bg_color(s, lv_color_hex(bg));
    lv_style_set_bg_opa(s, LV_OPA_COVER);
    lv_style_set_text_color(s, lv_color_hex(text));
    lv_style_set_radius(s, 10);
    lv_style_set_pad_hor(s, 8);
    lv_style_set_pad_ver(s, 4);
}

static void init_btn(lv_style_t *s, uint32_t bg)
{
    lv_style_init(s);
    lv_style_set_bg_color(s, lv_color_hex(bg));
    lv_style_set_bg_opa(s, LV_OPA_COVER);
    lv_style_set_text_color(s, lv_color_hex(0xFFFFFF));
    lv_style_set_radius(s, 10);
    lv_style_set_border_width(s, 0);
}

static void init_styles()
{
    lv_style_init(&sty_header);
    lv_style_set_bg_color(&sty_header, lv_color_hex(0x1E40AF));
    lv_style_set_bg_opa(&sty_header, LV_OPA_COVER);

    lv_style_init(&sty_panel);
    lv_style_set_bg_color(&sty_panel, lv_color_hex(0xFFFFFF));
    lv_style_set_bg_opa(&sty_panel, LV_OPA_COVER);
    lv_style_set_border_color(&sty_panel, lv_color_hex(0xD9E1EA));
    lv_style_set_border_width(&sty_panel, 1);
    lv_style_set_radius(&sty_panel, 12);
    lv_style_set_pad_all(&sty_panel, 12);

    lv_style_init(&sty_row);
    lv_style_set_bg_color(&sty_row, lv_color_hex(0xF8FAFC));
    lv_style_set_bg_opa(&sty_row, LV_OPA_COVER);
    lv_style_set_border_color(&sty_row, lv_color_hex(0xE2E8F0));
    lv_style_set_border_width(&sty_row, 1);
    lv_style_set_radius(&sty_row, 8);

    init_badge(&sty_badge_ok, 0xDCFCE7, 0x166534);
    init_badge(&sty_badge_alert, 0xFEE2E2, 0x991B1B);
    init_badge(&sty_badge_info, 0xDBEAFE, 0x1D4ED8);
    init_badge(&sty_badge_pending, 0xFEF3C7, 0x92400E);

    lv_style_init(&sty_menu_btn);
    lv_style_set_bg_color(&sty_menu_btn, lv_color_hex(0x1D4ED8));
    lv_style_set_bg_opa(&sty_menu_btn, LV_OPA_COVER);
    lv_style_set_border_width(&sty_menu_btn, 0);
    lv_style_set_radius(&sty_menu_btn, 10);

    lv_style_init(&sty_menu_panel);
    lv_style_set_bg_color(&sty_menu_panel, lv_color_hex(0xF8FAFC));
    lv_style_set_bg_opa(&sty_menu_panel, LV_OPA_COVER);
    lv_style_set_border_color(&sty_menu_panel, lv_color_hex(0xDCE3EA));
    lv_style_set_border_width(&sty_menu_panel, 1);

    init_btn(&sty_btn_primary, 0x2563EB);
    init_btn(&sty_btn_danger, 0xDC2626);
    init_btn(&sty_btn_warning, 0xEA580C);
}

// ============================================================
// Helpers
// ============================================================
static void apply_badge(lv_obj_t *obj, const char *status)
{
    lv_obj_remove_style_all(obj);
    if (strcmp(status, "Pronto") == 0 || strcmp(status, "OK") == 0 ||
        strcmp(status, "ONLINE") == 0 || strcmp(status, "Operacional") == 0)
        lv_obj_add_style(obj, &sty_badge_ok, 0);
    else if (strcmp(status, "ERRO") == 0 || strcmp(status, "OFFLINE") == 0 ||
             strcmp(status, "Critico") == 0)
        lv_obj_add_style(obj, &sty_badge_alert, 0);
    else if (strcmp(status, "Aguardando") == 0 || strcmp(status, "Pausado") == 0)
        lv_obj_add_style(obj, &sty_badge_pending, 0);
    else
        lv_obj_add_style(obj, &sty_badge_info, 0);
    lv_obj_set_style_text_font(obj, &lv_font_montserrat_14, 0);
}

static int disp_pct(int idx)
{
    if (app.dispensers[idx].capacidade <= 0)
        return 0;
    return app.dispensers[idx].quantidade * 100 / app.dispensers[idx].capacidade;
}

static void style_bar(lv_obj_t *bar)
{
    lv_obj_set_style_bg_color(bar, lv_color_hex(0xE2E8F0), 0);
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_radius(bar, 6, 0);
    lv_obj_set_style_radius(bar, 6, LV_PART_INDICATOR);
}

static void set_bar_color(lv_obj_t *bar, int pct)
{
    uint32_t c = (pct > 50) ? 0x22C55E : (pct > 20) ? 0xEAB308
                                                    : 0xEF4444;
    lv_obj_set_style_bg_color(bar, lv_color_hex(c), LV_PART_INDICATOR);
}

static void set_bar_color_with_min(lv_obj_t *bar, int idx)
{
    int qty = app.dispensers[idx].quantidade;
    int minimo = app.dispensers[idx].minimo;
    int pct = disp_pct(idx);
    uint32_t c;
    if (qty <= minimo)
        c = 0xEF4444;
    else if (pct <= 50)
        c = 0xEAB308;
    else
        c = 0x22C55E;
    lv_obj_set_style_bg_color(bar, lv_color_hex(c), LV_PART_INDICATOR);
}

static void set_disp_status_with_min(lv_obj_t *lbl, int idx)
{
    int qty = app.dispensers[idx].quantidade;
    int minimo = app.dispensers[idx].minimo;
    int pct = disp_pct(idx);
    if (qty <= minimo)
    {
        lv_label_set_text(lbl, "Critico!");
        lv_obj_set_style_text_color(lbl, lv_color_hex(0x991B1B), 0);
    }
    else if (pct <= 50)
    {
        lv_label_set_text(lbl, "Baixo");
        lv_obj_set_style_text_color(lbl, lv_color_hex(0x92400E), 0);
    }
    else
    {
        lv_label_set_text(lbl, "Normal");
        lv_obj_set_style_text_color(lbl, lv_color_hex(0x166534), 0);
    }
}

// Retorna dias ate a validade (negativo = ja vencido). Requer NTP sincronizado;
// se o relogio ainda nao foi ajustado, retorna um valor alto para nao disparar alarme falso.
static int dias_para_vencer(const char *validade)
{
    if (!validade || strlen(validade) < 10)
        return 9999;
    struct tm tm_val = {};
    if (sscanf(validade, "%d-%d-%d", &tm_val.tm_year, &tm_val.tm_mon, &tm_val.tm_mday) != 3)
        return 9999;
    tm_val.tm_year -= 1900;
    tm_val.tm_mon -= 1;
    time_t t_val = mktime(&tm_val);

    time_t agora = time(nullptr);
    if (agora < 1700000000)
        return 9999; // NTP ainda nao sincronizado

    return (int)((t_val - agora) / 86400);
}

static void set_disp_validade_lbl(lv_obj_t *lbl, int idx)
{
    const char *validade = app.dispensers[idx].validade;
    const char *lote = app.dispensers[idx].lote;
    if (lote[0] == '\0')
    {
        lv_label_set_text(lbl, "Sem lote cadastrado");
        lv_obj_set_style_text_color(lbl, lv_color_hex(0x94A3B8), 0);
        return;
    }
    int dias = dias_para_vencer(validade);
    char buf[48];
    snprintf(buf, sizeof(buf), "Lote %s - val %s", lote, validade[0] ? validade : "--");
    lv_label_set_text(lbl, buf);
    if (dias <= 30)
        lv_obj_set_style_text_color(lbl, lv_color_hex(0x991B1B), 0);
    else if (dias <= 60)
        lv_obj_set_style_text_color(lbl, lv_color_hex(0x92400E), 0);
    else
        lv_obj_set_style_text_color(lbl, lv_color_hex(0x64748B), 0);
}

static void format_elapsed(unsigned long ms, char *buf, size_t len)
{
    unsigned long s = ms / 1000;
    snprintf(buf, len, "%02lu:%02lu", s / 60, s % 60);
}

// ============================================================
// SCREENSAVER PAGE - Logos carregados do SD Card
// ============================================================
static lv_image_dsc_t img_apsen_dsc;
static lv_image_dsc_t img_softtek_dsc;
static bool logos_loaded = false;

static bool load_raw_image_from_sd(const char *path, lv_image_dsc_t *dsc, uint16_t w, uint16_t h)
{
    uint32_t expected = (uint32_t)w * h * 2;
    File f = SD.open(path, FILE_READ);
    if (!f)
    {
        Serial.printf("SD: nao abriu %s\n", path);
        return false;
    }
    if (f.size() != expected)
    {
        Serial.printf("SD: %s tamanho errado: %u vs %u\n", path, (uint32_t)f.size(), expected);
        f.close();
        return false;
    }
    uint8_t *buf = (uint8_t *)heap_caps_malloc(expected, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buf)
    {
        f.close();
        return false;
    }
    f.read(buf, expected);
    f.close();

    dsc->header.cf = LV_COLOR_FORMAT_RGB565;
    dsc->header.w = w;
    dsc->header.h = h;
    dsc->data_size = expected;
    dsc->data = buf;
    Serial.printf("SD: %s carregado (%ux%u)\n", path, w, h);
    return true;
}

static void load_logos_from_sd()
{
    if (!sd_ok)
        return;
    bool ok1 = load_raw_image_from_sd("/logo_apsen.bin", &img_apsen_dsc, 120, 120);
    bool ok2 = load_raw_image_from_sd("/logo_softtek.bin", &img_softtek_dsc, 130, 121);
    logos_loaded = ok1 && ok2;
}

static void screensaver_touch_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    last_touch_time = millis();
    lv_obj_add_flag(page_screensaver, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(page_login, LV_OBJ_FLAG_HIDDEN);
    currentPage = PAGE_LOGIN;
}

static void build_screensaver_page()
{
    page_screensaver = lv_obj_create(ui_screen);
    lv_obj_remove_style_all(page_screensaver);
    lv_obj_set_size(page_screensaver, 800, 480);
    lv_obj_set_pos(page_screensaver, 0, 0);
    lv_obj_set_style_bg_color(page_screensaver, lv_color_hex(0x000000), 0);
    lv_obj_set_style_bg_opa(page_screensaver, LV_OPA_COVER, 0);
    lv_obj_clear_flag(page_screensaver, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(page_screensaver, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(page_screensaver, screensaver_touch_cb, LV_EVENT_CLICKED, NULL);

    if (logos_loaded)
    {
        lv_obj_t *logo1 = lv_image_create(page_screensaver);
        lv_image_set_src(logo1, &img_apsen_dsc);
        lv_image_set_scale(logo1, 512);
        lv_obj_align(logo1, LV_ALIGN_CENTER, -170, 0);

        lv_obj_t *logo2 = lv_image_create(page_screensaver);
        lv_image_set_src(logo2, &img_softtek_dsc);
        lv_image_set_scale(logo2, 512);
        lv_obj_align(logo2, LV_ALIGN_CENTER, 170, 0);
    }
    else
    {
        lv_obj_t *lbl = lv_label_create(page_screensaver);
        lv_label_set_text(lbl, "APSEN + Softtek");
        lv_obj_set_style_text_color(lbl, lv_color_hex(0xFFFFFF), 0);
        lv_obj_set_style_text_font(lbl, &lv_font_montserrat_24, 0);
        lv_obj_align(lbl, LV_ALIGN_CENTER, 0, 0);
    }
}

static void activate_screensaver()
{
    if (currentPage == PAGE_SCREENSAVER)
        return;

    if (app.logado)
    {
        app.logado = false;
        app.operador_logado = -1;
        lv_obj_add_flag(ui_header, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(ui_body, LV_OBJ_FLAG_HIDDEN);
        lv_textarea_set_text(login_ta, "");
        lv_label_set_text(login_msg, "Digite a senha de acesso");
        lv_obj_set_style_text_color(login_msg, lv_color_hex(0x64748B), 0);
    }

    lv_obj_add_flag(page_login, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(page_screensaver, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(page_screensaver);
    currentPage = PAGE_SCREENSAVER;
}

// ============================================================
// LOGIN PAGE
// ============================================================
static void login_numpad_cb(lv_event_t *e)
{
    const char *txt = (const char *)lv_event_get_user_data(e);

    last_touch_time = millis();
    if (strcmp(txt, "OK") == 0)
    {
        const char *typed = lv_textarea_get_text(login_ta);
        int found = validar_pin_backend(typed);
        if (found >= 0)
        {
            do_login(found);
        }
        else
        {
            // Duas falhas diferentes, e a distincao importa para quem esta na
            // bancada: PIN errado se resolve digitando de novo; backend fora do
            // ar, nao. Sem a distincao o operador fica repetindo o PIN certo.
            lv_label_set_text(login_msg, app.online ? "SENHA INCORRETA!"
                                                    : "BACKEND OFFLINE");
            lv_obj_set_style_text_color(login_msg, lv_color_hex(0xDC2626), 0);
        }
        lv_textarea_set_text(login_ta, "");
    }
    else if (strcmp(txt, "C") == 0)
    {
        lv_textarea_set_text(login_ta, "");
        lv_label_set_text(login_msg, "Digite a senha de acesso");
        lv_obj_set_style_text_color(login_msg, lv_color_hex(0x64748B), 0);
    }
    else
    {
        lv_textarea_add_text(login_ta, txt);
    }
}

static lv_obj_t *make_numpad_btn(lv_obj_t *parent, const char *txt, int x, int y,
                                 int w, int h, lv_event_cb_t cb,
                                 const lv_font_t *font = &lv_font_montserrat_20)
{
    lv_obj_t *btn = lv_btn_create(parent);
    lv_obj_set_size(btn, w, h);
    lv_obj_set_pos(btn, x, y);
    lv_obj_set_style_bg_color(btn, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_bg_opa(btn, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(btn, lv_color_hex(0xCBD5E1), 0);
    lv_obj_set_style_border_width(btn, 1, 0);
    lv_obj_set_style_radius(btn, 10, 0);
    lv_obj_add_event_cb(btn, cb, LV_EVENT_CLICKED, (void *)txt);

    lv_obj_t *lbl = lv_label_create(btn);
    lv_label_set_text(lbl, txt);
    lv_obj_set_style_text_color(lbl, lv_color_hex(0x0F172A), 0);
    lv_obj_set_style_text_font(lbl, font, 0);
    lv_obj_center(lbl);
    return btn;
}

static void build_login_page()
{
    page_login = lv_obj_create(ui_screen);
    lv_obj_remove_style_all(page_login);
    lv_obj_set_size(page_login, 800, 480);
    lv_obj_set_pos(page_login, 0, 0);
    lv_obj_set_style_bg_color(page_login, lv_color_hex(0xEEF2F6), 0);
    lv_obj_set_style_bg_opa(page_login, LV_OPA_COVER, 0);
    lv_obj_clear_flag(page_login, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *top_bar = lv_obj_create(page_login);
    lv_obj_remove_style_all(top_bar);
    lv_obj_set_size(top_bar, 800, 60);
    lv_obj_set_pos(top_bar, 0, 0);
    lv_obj_add_style(top_bar, &sty_header, 0);
    lv_obj_clear_flag(top_bar, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *top_title = lv_label_create(top_bar);
    lv_label_set_text(top_title, "APSEN - Sistema de Dispensacao");
    lv_obj_set_style_text_color(top_title, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_text_font(top_title, &lv_font_montserrat_20, 0);
    lv_obj_center(top_title);

    lv_obj_t *panel = lv_obj_create(page_login);
    lv_obj_remove_style_all(panel);
    lv_obj_set_size(panel, 680, 380);
    lv_obj_add_style(panel, &sty_panel, 0);
    lv_obj_set_pos(panel, 60, 75);
    lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *lock_icon = lv_label_create(panel);
    lv_label_set_text(lock_icon, LV_SYMBOL_WARNING " Acesso Restrito");
    lv_obj_set_style_text_color(lock_icon, lv_color_hex(0x1E40AF), 0);
    lv_obj_set_style_text_font(lock_icon, &lv_font_montserrat_20, 0);
    lv_obj_set_pos(lock_icon, 20, 10);

    lv_obj_t *login_hint_lbl = lv_label_create(panel);
    lv_label_set_text(login_hint_lbl, "Digite a senha abaixo para acessar");
    lv_obj_set_style_text_color(login_hint_lbl, lv_color_hex(0x94A3B8), 0);
    lv_obj_set_style_text_font(login_hint_lbl, &lv_font_montserrat_14, 0);
    lv_obj_set_pos(login_hint_lbl, 20, 45);

    login_msg = lv_label_create(panel);
    lv_label_set_text(login_msg, "Digite a senha de acesso");
    lv_obj_set_style_text_color(login_msg, lv_color_hex(0x64748B), 0);
    lv_obj_set_style_text_font(login_msg, &lv_font_montserrat_16, 0);
    lv_obj_set_pos(login_msg, 20, 80);

    login_ta = lv_textarea_create(panel);
    lv_textarea_set_placeholder_text(login_ta, "Senha...");
    lv_textarea_set_password_mode(login_ta, true);
    lv_textarea_set_one_line(login_ta, true);
    lv_textarea_set_max_length(login_ta, 12);
    lv_textarea_set_accepted_chars(login_ta, "0123456789");
    lv_obj_set_size(login_ta, 250, 50);
    lv_obj_set_pos(login_ta, 20, 110);
    lv_obj_set_style_border_color(login_ta, lv_color_hex(0x2563EB), 0);
    lv_obj_set_style_border_width(login_ta, 2, 0);
    lv_obj_set_style_radius(login_ta, 10, 0);
    lv_obj_set_style_text_font(login_ta, &lv_font_montserrat_20, 0);

    int kx = 320, ky = 10;
    int bw = 80, bh = 65, gap = 8;

    make_numpad_btn(panel, "1", kx, ky, bw, bh, login_numpad_cb);
    make_numpad_btn(panel, "2", kx + (bw + gap), ky, bw, bh, login_numpad_cb);
    make_numpad_btn(panel, "3", kx + 2 * (bw + gap), ky, bw, bh, login_numpad_cb);
    make_numpad_btn(panel, "4", kx, ky + (bh + gap), bw, bh, login_numpad_cb);
    make_numpad_btn(panel, "5", kx + (bw + gap), ky + (bh + gap), bw, bh, login_numpad_cb);
    make_numpad_btn(panel, "6", kx + 2 * (bw + gap), ky + (bh + gap), bw, bh, login_numpad_cb);
    make_numpad_btn(panel, "7", kx, ky + 2 * (bh + gap), bw, bh, login_numpad_cb);
    make_numpad_btn(panel, "8", kx + (bw + gap), ky + 2 * (bh + gap), bw, bh, login_numpad_cb);
    make_numpad_btn(panel, "9", kx + 2 * (bw + gap), ky + 2 * (bh + gap), bw, bh, login_numpad_cb);
    make_numpad_btn(panel, "C", kx, ky + 3 * (bh + gap), bw, bh, login_numpad_cb);
    make_numpad_btn(panel, "0", kx + (bw + gap), ky + 3 * (bh + gap), bw, bh, login_numpad_cb);

    lv_obj_t *ok_btn = make_numpad_btn(panel, "OK", kx + 2 * (bw + gap), ky + 3 * (bh + gap), bw, bh, login_numpad_cb);
    lv_obj_set_style_bg_color(ok_btn, lv_color_hex(0x2563EB), 0);
    lv_obj_t *ok_lbl = lv_obj_get_child(ok_btn, 0);
    lv_obj_set_style_text_color(ok_lbl, lv_color_hex(0xFFFFFF), 0);
}

// ============================================================
// DASHBOARD PAGE
// ============================================================
static int find_dispenser_by_name(const char *name)
{
    for (int i = 0; i < NUM_DISPENSERS; i++)
    {
        if (strncasecmp(app.dispensers[i].nome, name, strlen(name)) == 0)
            return i;
    }
    return -1;
}

static void descontar_itens_ordem(const char *itens)
{
    // Segue MAX_ITENS_RESUMO_LEN: um buffer menor que o campo de origem perde
    // os ultimos itens em silencio, e o estoque deixa de ser descontado deles.
    char buf[MAX_ITENS_RESUMO_LEN];
    strncpy(buf, itens, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = '\0';

    char *tok = strtok(buf, ";");
    while (tok)
    {
        char *pipe = strchr(tok, '|');
        if (pipe)
        {
            *pipe = '\0';
            int qty = atoi(pipe + 1);
            int d = find_dispenser_by_name(tok);
            if (d >= 0 && qty > 0)
            {
                app.dispensers[d].quantidade -= qty;
                if (app.dispensers[d].quantidade < 0)
                    app.dispensers[d].quantidade = 0;
                Serial.printf("  Dispenser %s: -%d (restam %d)\n",
                              app.dispensers[d].nome, qty, app.dispensers[d].quantidade);
            }
        }
        tok = strtok(NULL, ";");
    }
}

// ============================================================
// POPUP DE ALERTA
// ============================================================
static lv_obj_t *alert_overlay = nullptr;
static lv_obj_t *alert_panel = nullptr;
static lv_obj_t *alert_msg_lbl = nullptr;

static void alert_close_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    lv_obj_add_flag(alert_overlay, LV_OBJ_FLAG_HIDDEN);
}

static void build_alert_popup()
{
    alert_overlay = lv_obj_create(ui_screen);
    lv_obj_remove_style_all(alert_overlay);
    lv_obj_set_size(alert_overlay, 800, 480);
    lv_obj_set_pos(alert_overlay, 0, 0);
    lv_obj_set_style_bg_color(alert_overlay, lv_color_hex(0x000000), 0);
    lv_obj_set_style_bg_opa(alert_overlay, LV_OPA_50, 0);
    lv_obj_add_flag(alert_overlay, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(alert_overlay, LV_OBJ_FLAG_SCROLLABLE);

    alert_panel = lv_obj_create(alert_overlay);
    lv_obj_remove_style_all(alert_panel);
    lv_obj_set_size(alert_panel, 500, 280);
    lv_obj_set_pos(alert_panel, 150, 100);
    lv_obj_set_style_bg_color(alert_panel, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_bg_opa(alert_panel, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(alert_panel, 16, 0);
    lv_obj_set_style_border_color(alert_panel, lv_color_hex(0xEF4444), 0);
    lv_obj_set_style_border_width(alert_panel, 3, 0);
    lv_obj_set_style_pad_all(alert_panel, 20, 0);
    lv_obj_clear_flag(alert_panel, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *icon = lv_label_create(alert_panel);
    lv_label_set_text(icon, LV_SYMBOL_WARNING " ESTOQUE INSUFICIENTE");
    lv_obj_set_style_text_color(icon, lv_color_hex(0xDC2626), 0);
    lv_obj_set_style_text_font(icon, &lv_font_montserrat_20, 0);
    lv_obj_set_pos(icon, 0, 0);

    alert_msg_lbl = lv_label_create(alert_panel);
    lv_label_set_text(alert_msg_lbl, "");
    lv_obj_set_width(alert_msg_lbl, 460);
    lv_label_set_long_mode(alert_msg_lbl, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_color(alert_msg_lbl, lv_color_hex(0x334155), 0);
    lv_obj_set_style_text_font(alert_msg_lbl, &lv_font_montserrat_16, 0);
    lv_obj_set_pos(alert_msg_lbl, 0, 35);

    lv_obj_t *ok_btn = lv_btn_create(alert_panel);
    lv_obj_remove_style_all(ok_btn);
    lv_obj_set_size(ok_btn, 140, 44);
    lv_obj_set_style_bg_color(ok_btn, lv_color_hex(0xDC2626), 0);
    lv_obj_set_style_bg_opa(ok_btn, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(ok_btn, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_radius(ok_btn, 10, 0);
    lv_obj_set_pos(ok_btn, 160, 190);
    lv_obj_add_event_cb(ok_btn, alert_close_cb, LV_EVENT_CLICKED, NULL);

    lv_obj_t *ok_lbl = lv_label_create(ok_btn);
    lv_label_set_text(ok_lbl, "Entendido");
    lv_obj_set_style_text_font(ok_lbl, &lv_font_montserrat_16, 0);
    lv_obj_center(ok_lbl);
}

static void show_alert(const char *msg)
{
    lv_label_set_text(alert_msg_lbl, msg);
    lv_obj_clear_flag(alert_overlay, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(alert_overlay);
}

// ============================================================
// VERIFICAR ESTOQUE ANTES DE INICIAR
// ============================================================
static bool verificar_estoque(const char *itens, char *erro, size_t erro_len)
{
    char buf[MAX_ITENS_RESUMO_LEN];
    strncpy(buf, itens, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = '\0';

    erro[0] = '\0';
    bool ok = true;

    char *tok = strtok(buf, ";");
    while (tok)
    {
        char *pipe = strchr(tok, '|');
        if (pipe)
        {
            *pipe = '\0';
            int qty = atoi(pipe + 1);
            int d = find_dispenser_by_name(tok);
            if (d < 0)
            {
                char tmp[64];
                snprintf(tmp, sizeof(tmp), "- %s: nao encontrado\n", tok);
                strncat(erro, tmp, erro_len - strlen(erro) - 1);
                ok = false;
            }
            else if (app.dispensers[d].quantidade < qty)
            {
                char tmp[80];
                snprintf(tmp, sizeof(tmp), "- %s: precisa %d, tem %d\n",
                         app.dispensers[d].nome, qty, app.dispensers[d].quantidade);
                strncat(erro, tmp, erro_len - strlen(erro) - 1);
                ok = false;
            }
        }
        tok = strtok(NULL, ";");
    }
    return ok;
}

static void log_ordem_to_csv(const OrdemExpedicao &ordem)
{
    if (!sd_ok)
        return;

    bool novo = !SD.exists("/historico.csv");
    File f = SD.open("/historico.csv", FILE_APPEND);
    if (!f)
        return;

    if (novo)
        f.println("ordem_id,lote,itens,destino,operador,hora_inicio,hora_fim,preparo_seg");

    char hora_fim[22];
    get_datetime(hora_fim, sizeof(hora_fim));

    unsigned long tempo_seg = 0;
    if (ordem.tempo_inicio > 0)
        tempo_seg = (millis() - ordem.tempo_inicio) / 1000;

    const char *op = (app.operador_logado >= 0) ? operadores[app.operador_logado].nome : "---";

    char itens_safe[MAX_ITENS_RESUMO_LEN];
    strncpy(itens_safe, ordem.itens, sizeof(itens_safe) - 1);
    itens_safe[sizeof(itens_safe) - 1] = '\0';
    for (char *p = itens_safe; *p; p++)
        if (*p == ',')
            *p = ';';

    f.printf("%s,%s,%s,%s,%s,%s,%s,%lu\n",
             ordem.id, ordem.lote, itens_safe, ordem.destino,
             op, ordem.hora_inicio, hora_fim, tempo_seg);
    f.close();
    Serial.printf("SD: ordem %s salva em historico.csv\n", ordem.id);
}

static void sync_dispensers_to_api()
{
    if (!app.online)
        return;

    JsonDocument doc;
    doc["cmd"] = "sync_dispensers";
    JsonArray arr = doc["itens"].to<JsonArray>();
    for (int i = 0; i < NUM_DISPENSERS; i++)
    {
        JsonObject d = arr.add<JsonObject>();
        d["slot"] = i + 1;
        d["quantidade"] = app.dispensers[i].quantidade;
    }

    char payload[512];
    serializeJson(doc, payload, sizeof(payload));

    JsonDocument resp;
    if (!serial_request(payload, "ok", resp))
    {
        Serial.println("Serial: sync dispensers falhou (timeout)");
        return;
    }
    // O backend recusa slot espelhado do central — quem manda no numero e quem
    // o mede. Ignorar o `ok` faria o log dizer "sincronizados" sobre uma
    // recusa, que e a mesma mentira que os botoes de ordem espelhada contavam.
    if (resp["ok"] | false)
        Serial.println("Serial: dispensers sincronizados");
    else
        Serial.printf("Serial: sync dispensers recusado: %s\n",
                      (const char *)(resp["msg"] | ""));
}

static void concluir_ordem(int idx)
{
    if (idx < 0 || idx >= app.num_ordens)
        return;
    if (app.ordens[idx].central)
    {
        // Concluir descontaria estoque local de novo: quem descontou foi a
        // celula. O backend ja recusa o set_status; o desconto seria local e
        // ninguem o veria acontecer.
        Serial.printf("Ordem %s e do computador central: conclusao ignorada\n", app.ordens[idx].id);
        return;
    }
    Serial.printf("Concluindo ordem %s - descontando itens:\n", app.ordens[idx].id);
    descontar_itens_ordem(app.ordens[idx].itens);
    log_ordem_to_csv(app.ordens[idx]);
    serial2_send_evento_ordem_concluida(app.ordens[idx]);
    strcpy(app.ordens[idx].status, "Pronto");

    const char *op = (app.operador_logado >= 0) ? operadores[app.operador_logado].nome : "---";

    if (app.online)
    {
        api_update_status(app.ordens[idx].id, "Concluido");
        sync_dispensers_to_api();
        api_log_historico(op, "Concluir Ordem", app.ordens[idx].id);
    }
    else
    {
        queue_pending_action("status", app.ordens[idx].id, "Concluido");
        queue_pending_action("sync", "", "");
    }

    app.ordem_atual = -1;
    update_all_ui();
}

static void dash_concluir_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    if (app.ordem_atual >= 0 && app.ordem_atual < app.num_ordens)
        concluir_ordem(app.ordem_atual);
}

static void dash_pausar_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    if (app.ordem_atual < 0 || app.ordem_atual >= app.num_ordens)
        return;
    int idx = app.ordem_atual;
    if (app.ordens[idx].central)
        return;
    app.ordens[idx].tempo_acumulado = millis() - app.ordens[idx].tempo_inicio;
    strcpy(app.ordens[idx].status, "Pausado");
    app.ordem_atual = -1;
    if (app.online)
    {
        api_update_status(app.ordens[idx].id, "Pausado");
        const char *op = (app.operador_logado >= 0) ? operadores[app.operador_logado].nome : "---";
        api_log_historico(op, "Pausar Ordem", app.ordens[idx].id);
    }
    else
    {
        queue_pending_action("status", app.ordens[idx].id, "Pausado");
    }
    update_dashboard_ui();
}

static void dash_order_action_cb(lv_event_t *e)
{
    int row = (int)(intptr_t)lv_event_get_user_data(e);
    if (row < 0 || row >= MAX_ORDENS)
        return;
    int idx = dash_row_ordem_idx[row];
    if (idx < 0 || idx >= app.num_ordens)
        return;
    // O botao fica escondido para ordem espelhada, entao aqui nao se chega pela
    // tela. A guarda existe para o caminho que ainda nao existe: linha reciclada
    // entre o refresh e o toque, ou uma tela nova que reaproveite este callback.
    if (app.ordens[idx].central)
    {
        Serial.printf("Ordem %s e do computador central: acao ignorada\n", app.ordens[idx].id);
        return;
    }

    if (strcmp(app.ordens[idx].status, "Aguardando") == 0)
    {
        if (app.ordem_atual >= 0 && strcmp(app.ordens[app.ordem_atual].status, "Separando") == 0)
            return;
        char erro[256] = "";
        if (!verificar_estoque(app.ordens[idx].itens, erro, sizeof(erro)))
        {
            char msg[448];
            snprintf(msg, sizeof(msg),
                     "Nao e possivel iniciar a ordem %s.\n"
                     "Medicamentos insuficientes:\n%s",
                     app.ordens[idx].id, erro);
            show_alert(msg);
            api_post_desvio(app.ordens[idx].id, "Estoque Insuficiente", erro);
            return;
        }
        strcpy(app.ordens[idx].status, "Separando");
        app.ordens[idx].tempo_inicio = millis();
        app.ordens[idx].tempo_acumulado = 0;
        get_datetime(app.ordens[idx].hora_inicio, sizeof(app.ordens[idx].hora_inicio));
        app.ordem_atual = idx;
        const char *op = (app.operador_logado >= 0) ? operadores[app.operador_logado].nome : "---";
        if (app.online)
        {
            api_update_status(app.ordens[idx].id, "Em Processo");
            api_log_historico(op, "Iniciar Ordem", app.ordens[idx].id);
        }
        else
        {
            queue_pending_action("status", app.ordens[idx].id, "Em Processo");
        }
    }
    else if (strcmp(app.ordens[idx].status, "Pausado") == 0)
    {
        if (app.ordem_atual >= 0 && strcmp(app.ordens[app.ordem_atual].status, "Separando") == 0)
            return;
        strcpy(app.ordens[idx].status, "Separando");
        app.ordens[idx].tempo_inicio = millis() - app.ordens[idx].tempo_acumulado;
        app.ordem_atual = idx;
        if (app.online)
            api_update_status(app.ordens[idx].id, "Em Processo");
        else
            queue_pending_action("status", app.ordens[idx].id, "Em Processo");
    }
    update_dashboard_ui();
}

static void build_dashboard_page()
{
    page_dashboard = lv_obj_create(ui_body);
    lv_obj_remove_style_all(page_dashboard);
    lv_obj_set_size(page_dashboard, 800, 412);
    lv_obj_set_pos(page_dashboard, 0, 0);
    lv_obj_set_style_bg_opa(page_dashboard, LV_OPA_TRANSP, 0);
    lv_obj_clear_flag(page_dashboard, LV_OBJ_FLAG_SCROLLABLE);

    // --- Left Panel: Lista de ordens / Ordem ativa ---
    lv_obj_t *left = lv_obj_create(page_dashboard);
    lv_obj_remove_style_all(left);
    lv_obj_set_size(left, 385, 392);
    lv_obj_add_style(left, &sty_panel, 0);
    lv_obj_set_pos(left, 8, 8);
    lv_obj_clear_flag(left, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *lt = lv_label_create(left);
    lv_label_set_text(lt, LV_SYMBOL_LIST " Ordens");
    lv_obj_set_style_text_color(lt, lv_color_hex(0x0F172A), 0);
    lv_obj_set_style_text_font(lt, &lv_font_montserrat_20, 0);
    lv_obj_set_pos(lt, 0, 0);

    // Lista de ordens pendentes
    dash_list_panel = lv_obj_create(left);
    lv_obj_remove_style_all(dash_list_panel);
    lv_obj_set_size(dash_list_panel, 361, 355);
    lv_obj_set_pos(dash_list_panel, 0, 28);
    lv_obj_set_style_bg_opa(dash_list_panel, LV_OPA_TRANSP, 0);
    lv_obj_clear_flag(dash_list_panel, LV_OBJ_FLAG_SCROLLABLE);

    for (int i = 0; i < MAX_ORDENS; i++)
    {
        int y = i * 68;
        dash_rows[i] = lv_obj_create(dash_list_panel);
        lv_obj_remove_style_all(dash_rows[i]);
        lv_obj_set_size(dash_rows[i], 355, 62);
        lv_obj_add_style(dash_rows[i], &sty_row, 0);
        lv_obj_set_pos(dash_rows[i], 0, y);
        lv_obj_clear_flag(dash_rows[i], LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(dash_rows[i], LV_OBJ_FLAG_HIDDEN);

        dash_row_id[i] = lv_label_create(dash_rows[i]);
        lv_label_set_text(dash_row_id[i], "--");
        // O os_id do central chega com dezenas de caracteres e o botao da linha
        // comeca em x=260: sem largura + LONG_DOT o texto passaria por baixo
        // dele e a linha viraria uma tira ilegivel.
        lv_obj_set_width(dash_row_id[i], 240);
        lv_label_set_long_mode(dash_row_id[i], LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_color(dash_row_id[i], lv_color_hex(0x1E40AF), 0);
        lv_obj_set_style_text_font(dash_row_id[i], &lv_font_montserrat_14, 0);
        lv_obj_set_pos(dash_row_id[i], 8, 6);

        dash_row_itens[i] = lv_label_create(dash_rows[i]);
        lv_label_set_text(dash_row_itens[i], "--");
        lv_obj_set_width(dash_row_itens[i], 220);
        lv_label_set_long_mode(dash_row_itens[i], LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_color(dash_row_itens[i], lv_color_hex(0x475569), 0);
        lv_obj_set_style_text_font(dash_row_itens[i], &lv_font_montserrat_14, 0);
        lv_obj_set_pos(dash_row_itens[i], 8, 30);

        dash_row_ordem_idx[i] = -1;

        dash_row_btn[i] = lv_btn_create(dash_rows[i]);
        lv_obj_remove_style_all(dash_row_btn[i]);
        lv_obj_set_size(dash_row_btn[i], 85, 36);
        lv_obj_add_style(dash_row_btn[i], &sty_btn_primary, 0);
        lv_obj_set_pos(dash_row_btn[i], 260, 12);
        lv_obj_add_event_cb(dash_row_btn[i], dash_order_action_cb, LV_EVENT_CLICKED, (void *)(intptr_t)i);

        dash_row_btn_lbl[i] = lv_label_create(dash_row_btn[i]);
        lv_label_set_text(dash_row_btn_lbl[i], "Iniciar");
        lv_obj_set_style_text_font(dash_row_btn_lbl[i], &lv_font_montserrat_14, 0);
        lv_obj_center(dash_row_btn_lbl[i]);

        // Mesmo lugar do botao, mostrado no lugar dele quando a ordem e do
        // computador central. Sem botao nao ha clique, e sem clique nao ha
        // popup de erro para explicar o que nao ia acontecer mesmo.
        dash_row_tag[i] = lv_label_create(dash_rows[i]);
        lv_label_set_text(dash_row_tag[i], "");
        lv_obj_set_width(dash_row_tag[i], 92);
        lv_label_set_long_mode(dash_row_tag[i], LV_LABEL_LONG_WRAP);
        lv_obj_set_style_text_align(dash_row_tag[i], LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_set_style_text_color(dash_row_tag[i], lv_color_hex(0x475569), 0);
        lv_obj_set_style_text_font(dash_row_tag[i], &lv_font_montserrat_14, 0);
        lv_obj_set_pos(dash_row_tag[i], 258, 12);
        lv_obj_add_flag(dash_row_tag[i], LV_OBJ_FLAG_HIDDEN);
    }

    // Painel da ordem ativa (escondido por padrao)
    dash_active_panel = lv_obj_create(left);
    lv_obj_remove_style_all(dash_active_panel);
    lv_obj_set_size(dash_active_panel, 361, 355);
    lv_obj_set_pos(dash_active_panel, 0, 28);
    lv_obj_set_style_bg_opa(dash_active_panel, LV_OPA_TRANSP, 0);
    lv_obj_clear_flag(dash_active_panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(dash_active_panel, LV_OBJ_FLAG_HIDDEN);

    lv_obj_t *id_title = lv_label_create(dash_active_panel);
    lv_label_set_text(id_title, "Pedido:");
    lv_obj_set_style_text_color(id_title, lv_color_hex(0x64748B), 0);
    lv_obj_set_style_text_font(id_title, &lv_font_montserrat_14, 0);
    lv_obj_set_pos(id_title, 0, 5);

    dash_ordem_id = lv_label_create(dash_active_panel);
    lv_label_set_text(dash_ordem_id, "--");
    lv_obj_set_style_text_color(dash_ordem_id, lv_color_hex(0x1E40AF), 0);
    lv_obj_set_style_text_font(dash_ordem_id, &lv_font_montserrat_20, 0);
    lv_obj_set_pos(dash_ordem_id, 70, 2);

    lv_obj_t *it_title = lv_label_create(dash_active_panel);
    lv_label_set_text(it_title, "Itens:");
    lv_obj_set_style_text_color(it_title, lv_color_hex(0x64748B), 0);
    lv_obj_set_style_text_font(it_title, &lv_font_montserrat_14, 0);
    lv_obj_set_pos(it_title, 0, 40);

    dash_ordem_itens = lv_label_create(dash_active_panel);
    lv_label_set_text(dash_ordem_itens, "--");
    lv_obj_set_width(dash_ordem_itens, 340);
    lv_label_set_long_mode(dash_ordem_itens, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_color(dash_ordem_itens, lv_color_hex(0x334155), 0);
    lv_obj_set_style_text_font(dash_ordem_itens, &lv_font_montserrat_16, 0);
    lv_obj_set_pos(dash_ordem_itens, 0, 58);

    lv_obj_t *dt_title = lv_label_create(dash_active_panel);
    lv_label_set_text(dt_title, "Destino:");
    lv_obj_set_style_text_color(dt_title, lv_color_hex(0x64748B), 0);
    lv_obj_set_style_text_font(dt_title, &lv_font_montserrat_14, 0);
    lv_obj_set_pos(dt_title, 0, 110);

    dash_ordem_dest = lv_label_create(dash_active_panel);
    lv_label_set_text(dash_ordem_dest, "--");
    lv_obj_set_style_text_color(dash_ordem_dest, lv_color_hex(0x334155), 0);
    lv_obj_set_style_text_font(dash_ordem_dest, &lv_font_montserrat_16, 0);
    lv_obj_set_pos(dash_ordem_dest, 75, 108);

    lv_obj_t *tm_title = lv_label_create(dash_active_panel);
    lv_label_set_text(tm_title, "Tempo:");
    lv_obj_set_style_text_color(tm_title, lv_color_hex(0x64748B), 0);
    lv_obj_set_style_text_font(tm_title, &lv_font_montserrat_14, 0);
    lv_obj_set_pos(tm_title, 0, 160);

    dash_timer = lv_label_create(dash_active_panel);
    lv_label_set_text(dash_timer, "00:00");
    lv_obj_set_style_text_color(dash_timer, lv_color_hex(0x0F172A), 0);
    lv_obj_set_style_text_font(dash_timer, &lv_font_montserrat_24, 0);
    lv_obj_set_pos(dash_timer, 0, 180);

    dash_btn_concluir = lv_btn_create(dash_active_panel);
    lv_obj_remove_style_all(dash_btn_concluir);
    lv_obj_set_size(dash_btn_concluir, 170, 50);
    lv_obj_add_style(dash_btn_concluir, &sty_btn_primary, 0);
    lv_obj_set_pos(dash_btn_concluir, 0, 240);
    lv_obj_add_event_cb(dash_btn_concluir, dash_concluir_cb, LV_EVENT_CLICKED, NULL);

    lv_obj_t *conc_lbl = lv_label_create(dash_btn_concluir);
    lv_label_set_text(conc_lbl, LV_SYMBOL_OK " Concluir");
    lv_obj_set_style_text_font(conc_lbl, &lv_font_montserrat_16, 0);
    lv_obj_center(conc_lbl);

    dash_btn_pausar = lv_btn_create(dash_active_panel);
    lv_obj_remove_style_all(dash_btn_pausar);
    lv_obj_set_size(dash_btn_pausar, 150, 50);
    lv_obj_add_style(dash_btn_pausar, &sty_btn_warning, 0);
    lv_obj_set_pos(dash_btn_pausar, 180, 240);
    lv_obj_add_event_cb(dash_btn_pausar, dash_pausar_cb, LV_EVENT_CLICKED, NULL);

    lv_obj_t *pause_lbl = lv_label_create(dash_btn_pausar);
    lv_label_set_text(pause_lbl, LV_SYMBOL_PAUSE " Pausar");
    lv_obj_set_style_text_font(pause_lbl, &lv_font_montserrat_16, 0);
    lv_obj_center(pause_lbl);

    // --- Right Panel: Status + Dispensers ---
    lv_obj_t *right = lv_obj_create(page_dashboard);
    lv_obj_remove_style_all(right);
    lv_obj_set_size(right, 385, 392);
    lv_obj_add_style(right, &sty_panel, 0);
    lv_obj_set_pos(right, 403, 8);
    lv_obj_clear_flag(right, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *rt = lv_label_create(right);
    lv_label_set_text(rt, LV_SYMBOL_SETTINGS " Status");
    lv_obj_set_style_text_color(rt, lv_color_hex(0x0F172A), 0);
    lv_obj_set_style_text_font(rt, &lv_font_montserrat_20, 0);
    lv_obj_set_pos(rt, 0, 0);

    dash_machine_icon = lv_label_create(right);
    lv_label_set_text(dash_machine_icon, LV_SYMBOL_OK " OPERACIONAL");
    lv_obj_set_style_text_color(dash_machine_icon, lv_color_hex(0x166534), 0);
    lv_obj_set_style_text_font(dash_machine_icon, &lv_font_montserrat_16, 0);
    lv_obj_set_pos(dash_machine_icon, 0, 30);

    dash_machine_msg = lv_label_create(right);
    lv_label_set_text(dash_machine_msg, "Operacional");
    lv_obj_set_style_text_color(dash_machine_msg, lv_color_hex(0x64748B), 0);
    lv_obj_set_style_text_font(dash_machine_msg, &lv_font_montserrat_14, 0);
    lv_obj_set_pos(dash_machine_msg, 0, 52);

    lv_obj_t *disp_title = lv_label_create(right);
    lv_label_set_text(disp_title, "Dispensers:");
    lv_obj_set_style_text_color(disp_title, lv_color_hex(0x475569), 0);
    lv_obj_set_style_text_font(disp_title, &lv_font_montserrat_14, 0);
    lv_obj_set_pos(disp_title, 0, 75);

    for (int i = 0; i < NUM_DISPENSERS; i++)
    {
        int y = 95 + i * 35;
        dash_disp_name[i] = lv_label_create(right);
        lv_label_set_text(dash_disp_name[i], app.dispensers[i].nome);
        lv_obj_set_width(dash_disp_name[i], 95);
        lv_label_set_long_mode(dash_disp_name[i], LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_color(dash_disp_name[i], lv_color_hex(0x334155), 0);
        lv_obj_set_style_text_font(dash_disp_name[i], &lv_font_montserrat_14, 0);
        lv_obj_set_pos(dash_disp_name[i], 0, y + 3);

        dash_disp_bar[i] = lv_bar_create(right);
        lv_obj_set_size(dash_disp_bar[i], 170, 14);
        lv_obj_set_pos(dash_disp_bar[i], 100, y + 5);
        lv_bar_set_range(dash_disp_bar[i], 0, 100);
        int pct = disp_pct(i);
        style_bar(dash_disp_bar[i]);
        lv_bar_set_value(dash_disp_bar[i], pct, LV_ANIM_OFF);
        set_bar_color_with_min(dash_disp_bar[i], i);

        dash_disp_pct[i] = lv_label_create(right);
        // Quantidade em unidades, nao porcentagem: a barra ao lado ja mostra a
        // proporcao, entao repetir "42%" ao lado dela nao acrescenta nada,
        // enquanto "63/150" responde a pergunta que o operador realmente faz.
        char buf[16];
        snprintf(buf, sizeof(buf), "%d/%d", app.dispensers[i].quantidade,
                 app.dispensers[i].capacidade);
        lv_label_set_text(dash_disp_pct[i], buf);
        lv_obj_set_style_text_color(dash_disp_pct[i], lv_color_hex(0x64748B), 0);
        lv_obj_set_style_text_font(dash_disp_pct[i], &lv_font_montserrat_14, 0);
        lv_obj_set_pos(dash_disp_pct[i], 280, y + 3);
    }
}

void update_dashboard_ui()
{
    // Ordem espelhada nunca vira `ordem_atual` — quem a executa e a celula, e
    // o painel da ordem ativa e a tela de quem esta com a ordem na mao. A
    // condicao repete isso de proposito: um caminho novo que esqueca a regra
    // erra na lista, nao na tela inteira.
    bool has_active = (app.ordem_atual >= 0 && app.ordem_atual < app.num_ordens &&
                       !app.ordens[app.ordem_atual].central &&
                       strcmp(app.ordens[app.ordem_atual].status, "Separando") == 0);

    if (has_active)
    {
        lv_obj_add_flag(dash_list_panel, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(dash_active_panel, LV_OBJ_FLAG_HIDDEN);

        OrdemExpedicao &o = app.ordens[app.ordem_atual];
        lv_label_set_text(dash_ordem_id, o.id);
        lv_label_set_text(dash_ordem_itens, o.itens);
        lv_label_set_text(dash_ordem_dest, o.destino);
    }
    else
    {
        lv_obj_clear_flag(dash_list_panel, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(dash_active_panel, LV_OBJ_FLAG_HIDDEN);

        // Renderiza so as ordens pendentes/pausadas, compactadas nas primeiras
        // linhas (sem deixar buraco no lugar de uma ordem ja concluida).
        // dash_row_ordem_idx[] guarda, por linha visivel, o indice real em
        // app.ordens[] — o botao de cada linha e fixo (criado uma vez), quem
        // muda a cada refresh e o indice pra onde ele aponta.
        int visible = 0;
        for (int i = 0; i < app.num_ordens && visible < MAX_ORDENS; i++)
        {
            const OrdemExpedicao &o = app.ordens[i];
            const bool aguardando = strcmp(o.status, "Aguardando") == 0;
            const bool pausado = strcmp(o.status, "Pausado") == 0;
            // Ordem local: como sempre — o que ainda nao comecou ou esta
            // pausado, que e o que o operador pode acionar.
            // Ordem do central: entra tambem a que esta em execucao AGORA
            // ("Separando"). Ela nao aparece no painel de ordem ativa, que e da
            // ordem local; se nao aparecesse aqui, sumiria da tela justamente
            // enquanto a celula a executa.
            const bool mostrar = o.central ? (aguardando || pausado ||
                                              strcmp(o.status, "Separando") == 0)
                                           : (aguardando || pausado);
            if (!mostrar)
                continue;

            lv_obj_clear_flag(dash_rows[visible], LV_OBJ_FLAG_HIDDEN);
            lv_label_set_text(dash_row_id[visible], o.id);
            lv_label_set_text(dash_row_itens[visible], o.itens);

            if (o.central)
            {
                lv_obj_add_flag(dash_row_btn[visible], LV_OBJ_FLAG_HIDDEN);
                lv_obj_clear_flag(dash_row_tag[visible], LV_OBJ_FLAG_HIDDEN);
                lv_label_set_text_fmt(dash_row_tag[visible], "CENTRAL\n%s", o.status);
            }
            else
            {
                lv_obj_add_flag(dash_row_tag[visible], LV_OBJ_FLAG_HIDDEN);
                lv_obj_clear_flag(dash_row_btn[visible], LV_OBJ_FLAG_HIDDEN);
                lv_label_set_text(dash_row_btn_lbl[visible], pausado ? "Retomar" : "Iniciar");
            }

            dash_row_ordem_idx[visible] = i;
            visible++;
        }
        for (int r = visible; r < MAX_ORDENS; r++)
        {
            lv_obj_add_flag(dash_rows[r], LV_OBJ_FLAG_HIDDEN);
            dash_row_ordem_idx[r] = -1;
        }
    }

    if (app.maquina_ok)
    {
        lv_label_set_text(dash_machine_icon, LV_SYMBOL_OK " OPERACIONAL");
        lv_obj_set_style_text_color(dash_machine_icon, lv_color_hex(0x166534), 0);
    }
    else
    {
        lv_label_set_text(dash_machine_icon, LV_SYMBOL_WARNING " ERRO");
        lv_obj_set_style_text_color(dash_machine_icon, lv_color_hex(0xDC2626), 0);
    }
    lv_label_set_text(dash_machine_msg, app.status_msg);

    for (int i = 0; i < NUM_DISPENSERS; i++)
    {
        lv_label_set_text(dash_disp_name[i], app.dispensers[i].nome);
        int pct = disp_pct(i);
        lv_bar_set_value(dash_disp_bar[i], pct, LV_ANIM_ON);
        set_bar_color_with_min(dash_disp_bar[i], i);
        char buf[16];
        snprintf(buf, sizeof(buf), "%d/%d", app.dispensers[i].quantidade,
                 app.dispensers[i].capacidade);
        lv_label_set_text(dash_disp_pct[i], buf);
    }
}

// ============================================================
// DISPENSERS PAGE (somente leitura)
// ============================================================
static void build_dispensers_page()
{
    page_dispensers = lv_obj_create(ui_body);
    lv_obj_remove_style_all(page_dispensers);
    lv_obj_set_size(page_dispensers, 800, 412);
    lv_obj_set_pos(page_dispensers, 0, 0);
    lv_obj_set_style_bg_opa(page_dispensers, LV_OPA_TRANSP, 0);
    lv_obj_clear_flag(page_dispensers, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *panel = lv_obj_create(page_dispensers);
    lv_obj_remove_style_all(panel);
    lv_obj_set_size(panel, 784, 400);
    lv_obj_add_style(panel, &sty_panel, 0);
    lv_obj_set_pos(panel, 8, 6);
    lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *title = lv_label_create(panel);
    lv_label_set_text(title, LV_SYMBOL_SETTINGS " Dispensers de Medicamentos");
    lv_obj_set_style_text_color(title, lv_color_hex(0x0F172A), 0);
    lv_obj_set_style_text_font(title, &lv_font_montserrat_20, 0);
    lv_obj_set_pos(title, 0, 0);

    for (int i = 0; i < NUM_DISPENSERS; i++)
    {
        int col = i % 4;
        int row = i / 4;
        int x = col * 186 + 3;
        int y = row * 173 + 30;

        disp_card[i] = lv_obj_create(panel);
        lv_obj_remove_style_all(disp_card[i]);
        lv_obj_set_size(disp_card[i], 180, 165);
        lv_obj_add_style(disp_card[i], &sty_panel, 0);
        lv_obj_set_pos(disp_card[i], x, y);
        lv_obj_clear_flag(disp_card[i], LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(disp_card[i], LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(disp_card[i], disp_card_click_cb, LV_EVENT_CLICKED,
                            (void *)(intptr_t)i);

        lv_obj_t *num_lbl = lv_label_create(disp_card[i]);
        lv_label_set_text_fmt(num_lbl, "Disp. %d", i + 1);
        lv_obj_set_style_text_color(num_lbl, lv_color_hex(0x1E40AF), 0);
        lv_obj_set_style_text_font(num_lbl, &lv_font_montserrat_14, 0);
        lv_obj_set_pos(num_lbl, 0, 0);

        disp_nome_lbl[i] = lv_label_create(disp_card[i]);
        lv_label_set_text(disp_nome_lbl[i], app.dispensers[i].nome);
        lv_obj_set_width(disp_nome_lbl[i], 152);
        lv_label_set_long_mode(disp_nome_lbl[i], LV_LABEL_LONG_WRAP);
        lv_obj_set_style_text_color(disp_nome_lbl[i], lv_color_hex(0x334155), 0);
        lv_obj_set_style_text_font(disp_nome_lbl[i], &lv_font_montserrat_14, 0);
        lv_obj_set_pos(disp_nome_lbl[i], 0, 18);

        disp_qtd_lbl[i] = lv_label_create(disp_card[i]);
        lv_label_set_text_fmt(disp_qtd_lbl[i], "%d / %d", app.dispensers[i].quantidade, app.dispensers[i].capacidade);
        lv_obj_set_style_text_color(disp_qtd_lbl[i], lv_color_hex(0x0F172A), 0);
        lv_obj_set_style_text_font(disp_qtd_lbl[i], &lv_font_montserrat_14, 0);
        lv_obj_set_pos(disp_qtd_lbl[i], 0, 52);

        int pct = disp_pct(i);
        disp_bar[i] = lv_bar_create(disp_card[i]);
        lv_obj_set_size(disp_bar[i], 152, 14);
        lv_obj_set_pos(disp_bar[i], 0, 74);
        lv_bar_set_range(disp_bar[i], 0, 100);
        style_bar(disp_bar[i]);
        lv_bar_set_value(disp_bar[i], pct, LV_ANIM_OFF);
        set_bar_color_with_min(disp_bar[i], i);

        disp_status_lbl[i] = lv_label_create(disp_card[i]);
        lv_obj_set_style_text_font(disp_status_lbl[i], &lv_font_montserrat_14, 0);
        lv_obj_set_pos(disp_status_lbl[i], 0, 96);
        set_disp_status_with_min(disp_status_lbl[i], i);

        disp_validade_lbl[i] = lv_label_create(disp_card[i]);
        lv_obj_set_width(disp_validade_lbl[i], 152);
        lv_label_set_long_mode(disp_validade_lbl[i], LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_font(disp_validade_lbl[i], &lv_font_montserrat_14, 0);
        lv_obj_set_pos(disp_validade_lbl[i], 0, 118);
        set_disp_validade_lbl(disp_validade_lbl[i], i);
    }
}

void update_dispensers_ui()
{
    for (int i = 0; i < NUM_DISPENSERS; i++)
    {
        lv_label_set_text(disp_nome_lbl[i], app.dispensers[i].nome);
        lv_label_set_text_fmt(disp_qtd_lbl[i], "%d / %d (min:%d)",
                              app.dispensers[i].quantidade, app.dispensers[i].capacidade, app.dispensers[i].minimo);
        int pct = disp_pct(i);
        lv_bar_set_value(disp_bar[i], pct, LV_ANIM_ON);
        set_bar_color_with_min(disp_bar[i], i);
        set_disp_status_with_min(disp_status_lbl[i], i);
        set_disp_validade_lbl(disp_validade_lbl[i], i);
    }
}

// ============================================================
// DISPENSER EDIT - PIN + MEDICATION SELECTION
// ============================================================
static void disp_med_select_cb(lv_event_t *e)
{
    int med_idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (med_idx < 0 || med_idx >= num_catalogo)
        return;
    if (disp_edit_slot < 0 || disp_edit_slot >= NUM_DISPENSERS)
        return;

    strncpy(app.dispensers[disp_edit_slot].nome, catalogo[med_idx],
            sizeof(app.dispensers[0].nome) - 1);
    app.dispensers[disp_edit_slot].nome[sizeof(app.dispensers[0].nome) - 1] = '\0';

    lv_obj_add_flag(disp_med_overlay, LV_OBJ_FLAG_HIDDEN);

    api_update_dispenser_med(disp_edit_slot + 1, catalogo[med_idx]);

    update_dispensers_ui();
    update_dashboard_ui();
    update_status_ui();

    Serial.printf("Dispenser %d alterado para: %s\n", disp_edit_slot + 1, catalogo[med_idx]);
    {
        const char *op = (app.operador_logado >= 0) ? operadores[app.operador_logado].nome : "---";
        char det[64];
        snprintf(det, sizeof(det), "Disp.%d -> %s", disp_edit_slot + 1, catalogo[med_idx]);
        api_log_historico(op, "Trocar Medicamento", det);
    }
    disp_edit_slot = -1;
}

static void disp_med_close_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    lv_obj_add_flag(disp_med_overlay, LV_OBJ_FLAG_HIDDEN);
    disp_edit_slot = -1;
}

static void show_med_selection()
{
    lv_obj_clean(disp_med_list);
    lv_label_set_text_fmt(disp_med_title, "Disp. %d - Selecionar Medicamento",
                          disp_edit_slot + 1);

    for (int i = 0; i < num_catalogo; i++)
    {
        lv_obj_t *btn = lv_btn_create(disp_med_list);
        lv_obj_set_size(btn, 440, 42);
        lv_obj_set_pos(btn, 0, i * 48);
        lv_obj_set_style_bg_color(btn, lv_color_hex(0xF8FAFC), 0);
        lv_obj_set_style_bg_opa(btn, LV_OPA_COVER, 0);
        lv_obj_set_style_border_color(btn, lv_color_hex(0xE2E8F0), 0);
        lv_obj_set_style_border_width(btn, 1, 0);
        lv_obj_set_style_radius(btn, 8, 0);
        lv_obj_add_event_cb(btn, disp_med_select_cb, LV_EVENT_CLICKED,
                            (void *)(intptr_t)i);

        lv_obj_t *lbl = lv_label_create(btn);
        lv_label_set_text(lbl, catalogo[i]);
        lv_obj_set_style_text_color(lbl, lv_color_hex(0x0F172A), 0);
        lv_obj_set_style_text_font(lbl, &lv_font_montserrat_16, 0);
        lv_obj_center(lbl);
    }

    lv_obj_clear_flag(disp_med_overlay, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(disp_med_overlay);
}

static void disp_pin_numpad_cb(lv_event_t *e)
{
    const char *txt = (const char *)lv_event_get_user_data(e);
    last_touch_time = millis();

    if (strcmp(txt, "OK") == 0)
    {
        const char *typed = lv_textarea_get_text(disp_pin_ta);
        if (validar_pin_backend(typed) >= 0)
        {
            lv_obj_add_flag(disp_pin_overlay, LV_OBJ_FLAG_HIDDEN);
            show_med_selection();
        }
        else
        {
            lv_label_set_text(disp_pin_msg, app.online ? "SENHA INCORRETA!"
                                                       : "BACKEND OFFLINE");
            lv_obj_set_style_text_color(disp_pin_msg, lv_color_hex(0xDC2626), 0);
        }
        lv_textarea_set_text(disp_pin_ta, "");
    }
    else if (strcmp(txt, "C") == 0)
    {
        lv_textarea_set_text(disp_pin_ta, "");
        lv_label_set_text(disp_pin_msg, "Digite a senha para alterar");
        lv_obj_set_style_text_color(disp_pin_msg, lv_color_hex(0x64748B), 0);
    }
    else if (strcmp(txt, "X") == 0)
    {
        lv_obj_add_flag(disp_pin_overlay, LV_OBJ_FLAG_HIDDEN);
        disp_edit_slot = -1;
    }
    else
    {
        lv_textarea_add_text(disp_pin_ta, txt);
    }
}

static void disp_card_click_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= NUM_DISPENSERS)
        return;
    last_touch_time = millis();
    disp_edit_slot = idx;
    lv_textarea_set_text(disp_pin_ta, "");
    lv_label_set_text_fmt(disp_pin_title, "Dispenser %d - %s", idx + 1,
                          app.dispensers[idx].nome);
    lv_label_set_text(disp_pin_msg, "Digite a senha para alterar");
    lv_obj_set_style_text_color(disp_pin_msg, lv_color_hex(0x64748B), 0);
    lv_obj_clear_flag(disp_pin_overlay, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(disp_pin_overlay);
}

static void build_disp_pin_popup()
{
    disp_pin_overlay = lv_obj_create(ui_screen);
    lv_obj_remove_style_all(disp_pin_overlay);
    lv_obj_set_size(disp_pin_overlay, 800, 480);
    lv_obj_set_pos(disp_pin_overlay, 0, 0);
    lv_obj_set_style_bg_color(disp_pin_overlay, lv_color_hex(0x000000), 0);
    lv_obj_set_style_bg_opa(disp_pin_overlay, LV_OPA_50, 0);
    lv_obj_add_flag(disp_pin_overlay, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(disp_pin_overlay, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *panel = lv_obj_create(disp_pin_overlay);
    lv_obj_remove_style_all(panel);
    lv_obj_set_size(panel, 580, 310);
    lv_obj_set_pos(panel, 110, 85);
    lv_obj_set_style_bg_color(panel, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(panel, 16, 0);
    lv_obj_set_style_border_color(panel, lv_color_hex(0x2563EB), 0);
    lv_obj_set_style_border_width(panel, 2, 0);
    lv_obj_set_style_pad_all(panel, 20, 0);
    lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);

    disp_pin_title = lv_label_create(panel);
    lv_label_set_text(disp_pin_title, "Dispenser - Senha");
    lv_obj_set_width(disp_pin_title, 280);
    lv_label_set_long_mode(disp_pin_title, LV_LABEL_LONG_DOT);
    lv_obj_set_style_text_color(disp_pin_title, lv_color_hex(0x1E40AF), 0);
    lv_obj_set_style_text_font(disp_pin_title, &lv_font_montserrat_16, 0);
    lv_obj_set_pos(disp_pin_title, 0, 0);

    disp_pin_msg = lv_label_create(panel);
    lv_label_set_text(disp_pin_msg, "Digite a senha para alterar");
    lv_obj_set_style_text_color(disp_pin_msg, lv_color_hex(0x64748B), 0);
    lv_obj_set_style_text_font(disp_pin_msg, &lv_font_montserrat_14, 0);
    lv_obj_set_pos(disp_pin_msg, 0, 30);

    disp_pin_ta = lv_textarea_create(panel);
    lv_textarea_set_placeholder_text(disp_pin_ta, "Senha...");
    lv_textarea_set_password_mode(disp_pin_ta, true);
    lv_textarea_set_one_line(disp_pin_ta, true);
    lv_textarea_set_max_length(disp_pin_ta, 12);
    lv_textarea_set_accepted_chars(disp_pin_ta, "0123456789");
    lv_obj_set_size(disp_pin_ta, 220, 48);
    lv_obj_set_pos(disp_pin_ta, 0, 55);
    lv_obj_set_style_border_color(disp_pin_ta, lv_color_hex(0x2563EB), 0);
    lv_obj_set_style_border_width(disp_pin_ta, 2, 0);
    lv_obj_set_style_radius(disp_pin_ta, 10, 0);
    lv_obj_set_style_text_font(disp_pin_ta, &lv_font_montserrat_16, 0);

    int kx = 280, ky = 0;
    int bw = 72, bh = 55, gap = 6;

    make_numpad_btn(panel, "1", kx, ky, bw, bh, disp_pin_numpad_cb, &lv_font_montserrat_16);
    make_numpad_btn(panel, "2", kx + (bw + gap), ky, bw, bh, disp_pin_numpad_cb, &lv_font_montserrat_16);
    make_numpad_btn(panel, "3", kx + 2 * (bw + gap), ky, bw, bh, disp_pin_numpad_cb, &lv_font_montserrat_16);
    make_numpad_btn(panel, "4", kx, ky + (bh + gap), bw, bh, disp_pin_numpad_cb, &lv_font_montserrat_16);
    make_numpad_btn(panel, "5", kx + (bw + gap), ky + (bh + gap), bw, bh, disp_pin_numpad_cb, &lv_font_montserrat_16);
    make_numpad_btn(panel, "6", kx + 2 * (bw + gap), ky + (bh + gap), bw, bh, disp_pin_numpad_cb, &lv_font_montserrat_16);
    make_numpad_btn(panel, "7", kx, ky + 2 * (bh + gap), bw, bh, disp_pin_numpad_cb, &lv_font_montserrat_16);
    make_numpad_btn(panel, "8", kx + (bw + gap), ky + 2 * (bh + gap), bw, bh, disp_pin_numpad_cb, &lv_font_montserrat_16);
    make_numpad_btn(panel, "9", kx + 2 * (bw + gap), ky + 2 * (bh + gap), bw, bh, disp_pin_numpad_cb, &lv_font_montserrat_16);
    make_numpad_btn(panel, "C", kx, ky + 3 * (bh + gap), bw, bh, disp_pin_numpad_cb, &lv_font_montserrat_16);
    make_numpad_btn(panel, "0", kx + (bw + gap), ky + 3 * (bh + gap), bw, bh, disp_pin_numpad_cb, &lv_font_montserrat_16);

    lv_obj_t *ok_btn = make_numpad_btn(panel, "OK", kx + 2 * (bw + gap),
                                       ky + 3 * (bh + gap), bw, bh, disp_pin_numpad_cb, &lv_font_montserrat_16);
    lv_obj_set_style_bg_color(ok_btn, lv_color_hex(0x2563EB), 0);
    lv_obj_t *ok_lbl = lv_obj_get_child(ok_btn, 0);
    lv_obj_set_style_text_color(ok_lbl, lv_color_hex(0xFFFFFF), 0);

    lv_obj_t *cancel_btn = lv_btn_create(panel);
    lv_obj_remove_style_all(cancel_btn);
    lv_obj_set_size(cancel_btn, 220, 44);
    lv_obj_set_style_bg_color(cancel_btn, lv_color_hex(0xF1F5F9), 0);
    lv_obj_set_style_bg_opa(cancel_btn, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(cancel_btn, lv_color_hex(0xCBD5E1), 0);
    lv_obj_set_style_border_width(cancel_btn, 1, 0);
    lv_obj_set_style_radius(cancel_btn, 10, 0);
    lv_obj_set_pos(cancel_btn, 0, 220);
    lv_obj_add_event_cb(cancel_btn, disp_pin_numpad_cb, LV_EVENT_CLICKED,
                        (void *)"X");

    lv_obj_t *cancel_lbl = lv_label_create(cancel_btn);
    lv_label_set_text(cancel_lbl, "Cancelar");
    lv_obj_set_style_text_color(cancel_lbl, lv_color_hex(0x475569), 0);
    lv_obj_set_style_text_font(cancel_lbl, &lv_font_montserrat_16, 0);
    lv_obj_center(cancel_lbl);
}

static void build_disp_med_popup()
{
    disp_med_overlay = lv_obj_create(ui_screen);
    lv_obj_remove_style_all(disp_med_overlay);
    lv_obj_set_size(disp_med_overlay, 800, 480);
    lv_obj_set_pos(disp_med_overlay, 0, 0);
    lv_obj_set_style_bg_color(disp_med_overlay, lv_color_hex(0x000000), 0);
    lv_obj_set_style_bg_opa(disp_med_overlay, LV_OPA_50, 0);
    lv_obj_add_flag(disp_med_overlay, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(disp_med_overlay, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *panel = lv_obj_create(disp_med_overlay);
    lv_obj_remove_style_all(panel);
    lv_obj_set_size(panel, 500, 420);
    lv_obj_set_pos(panel, 150, 30);
    lv_obj_set_style_bg_color(panel, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(panel, 16, 0);
    lv_obj_set_style_border_color(panel, lv_color_hex(0x2563EB), 0);
    lv_obj_set_style_border_width(panel, 2, 0);
    lv_obj_set_style_pad_all(panel, 16, 0);
    lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);

    disp_med_title = lv_label_create(panel);
    lv_label_set_text(disp_med_title, "Selecionar Medicamento");
    lv_obj_set_style_text_color(disp_med_title, lv_color_hex(0x1E40AF), 0);
    lv_obj_set_style_text_font(disp_med_title, &lv_font_montserrat_16, 0);
    lv_obj_set_pos(disp_med_title, 0, 0);

    lv_obj_t *close_btn = lv_btn_create(panel);
    lv_obj_remove_style_all(close_btn);
    lv_obj_set_size(close_btn, 36, 36);
    lv_obj_set_style_bg_color(close_btn, lv_color_hex(0xF1F5F9), 0);
    lv_obj_set_style_bg_opa(close_btn, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(close_btn, 18, 0);
    lv_obj_set_pos(close_btn, 430, 0);
    lv_obj_add_event_cb(close_btn, disp_med_close_cb, LV_EVENT_CLICKED, NULL);

    lv_obj_t *close_lbl = lv_label_create(close_btn);
    lv_label_set_text(close_lbl, LV_SYMBOL_CLOSE);
    lv_obj_set_style_text_color(close_lbl, lv_color_hex(0x475569), 0);
    lv_obj_set_style_text_font(close_lbl, &lv_font_montserrat_16, 0);
    lv_obj_center(close_lbl);

    disp_med_list = lv_obj_create(panel);
    lv_obj_remove_style_all(disp_med_list);
    lv_obj_set_size(disp_med_list, 460, 350);
    lv_obj_set_pos(disp_med_list, 0, 35);
    lv_obj_set_style_bg_opa(disp_med_list, LV_OPA_TRANSP, 0);
    lv_obj_set_style_pad_all(disp_med_list, 0, 0);
}

// ============================================================
// RELATORIO PAGE
// ============================================================
static char qr_url[128] = "";

static void build_relatorio_page()
{
    page_relatorio = lv_obj_create(ui_body);
    lv_obj_remove_style_all(page_relatorio);
    lv_obj_set_size(page_relatorio, 800, 412);
    lv_obj_set_pos(page_relatorio, 0, 0);
    lv_obj_set_style_bg_opa(page_relatorio, LV_OPA_TRANSP, 0);
    lv_obj_clear_flag(page_relatorio, LV_OBJ_FLAG_SCROLLABLE);

    // === Painel esquerdo: QR + contadores ===
    lv_obj_t *left = lv_obj_create(page_relatorio);
    lv_obj_remove_style_all(left);
    lv_obj_set_size(left, 250, 400);
    lv_obj_add_style(left, &sty_panel, 0);
    lv_obj_set_pos(left, 8, 6);
    lv_obj_clear_flag(left, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *title = lv_label_create(left);
    lv_label_set_text(title, LV_SYMBOL_LIST " Relatorio");
    lv_obj_set_style_text_color(title, lv_color_hex(0x0F172A), 0);
    lv_obj_set_style_text_font(title, &lv_font_montserrat_20, 0);
    lv_obj_set_pos(title, 0, 0);

    rel_total_lbl = lv_label_create(left);
    lv_label_set_text(rel_total_lbl, "Total: 0");
    lv_obj_set_style_text_color(rel_total_lbl, lv_color_hex(0x334155), 0);
    lv_obj_set_style_text_font(rel_total_lbl, &lv_font_montserrat_20, 0);
    lv_obj_set_pos(rel_total_lbl, 0, 40);

    rel_pend_lbl = lv_label_create(left);
    lv_label_set_text(rel_pend_lbl, "Pendentes: 0");
    lv_obj_set_style_text_color(rel_pend_lbl, lv_color_hex(0x92400E), 0);
    lv_obj_set_style_text_font(rel_pend_lbl, &lv_font_montserrat_16, 0);
    lv_obj_set_pos(rel_pend_lbl, 0, 75);

    rel_concl_lbl = lv_label_create(left);
    lv_label_set_text(rel_concl_lbl, "Concluidas: 0");
    lv_obj_set_style_text_color(rel_concl_lbl, lv_color_hex(0x166534), 0);
    lv_obj_set_style_text_font(rel_concl_lbl, &lv_font_montserrat_16, 0);
    lv_obj_set_pos(rel_concl_lbl, 0, 105);

    rel_qr = lv_qrcode_create(left);
    lv_qrcode_set_size(rel_qr, 120);
    lv_qrcode_set_dark_color(rel_qr, lv_color_hex(0x0F172A));
    lv_qrcode_set_light_color(rel_qr, lv_color_hex(0xFFFFFF));
    lv_obj_set_pos(rel_qr, 50, 150);

    snprintf(qr_url, sizeof(qr_url), "%s/relatorio", WEB_DASHBOARD_URL);
    lv_qrcode_update(rel_qr, qr_url, strlen(qr_url));

    lv_obj_t *qr_info = lv_label_create(left);
    lv_label_set_text(qr_info, "Escaneie p/ relatorio");
    lv_obj_set_width(qr_info, 220);
    lv_obj_set_style_text_color(qr_info, lv_color_hex(0x64748B), 0);
    lv_obj_set_style_text_font(qr_info, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_align(qr_info, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_pos(qr_info, 0, 285);

    // === Painel direito: Log de ordens ===
    lv_obj_t *right = lv_obj_create(page_relatorio);
    lv_obj_remove_style_all(right);
    lv_obj_set_size(right, 520, 400);
    lv_obj_add_style(right, &sty_panel, 0);
    lv_obj_set_pos(right, 268, 6);
    lv_obj_clear_flag(right, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *log_title = lv_label_create(right);
    lv_label_set_text(log_title, LV_SYMBOL_LIST " Ordens Realizadas");
    lv_obj_set_style_text_color(log_title, lv_color_hex(0x0F172A), 0);
    lv_obj_set_style_text_font(log_title, &lv_font_montserrat_20, 0);
    lv_obj_set_pos(log_title, 0, 0);

    // Header
    lv_obj_t *hdr = lv_obj_create(right);
    lv_obj_remove_style_all(hdr);
    lv_obj_set_size(hdr, 500, 22);
    lv_obj_set_pos(hdr, 0, 30);
    lv_obj_set_style_bg_color(hdr, lv_color_hex(0xF1F5F9), 0);
    lv_obj_set_style_bg_opa(hdr, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(hdr, 4, 0);
    lv_obj_clear_flag(hdr, LV_OBJ_FLAG_SCROLLABLE);

    const char *hdrs[] = {"OS", "Itens", "Status"};
    int hx[] = {5, 100, 400};
    for (int i = 0; i < 3; i++)
    {
        lv_obj_t *l = lv_label_create(hdr);
        lv_label_set_text(l, hdrs[i]);
        lv_obj_set_style_text_color(l, lv_color_hex(0x64748B), 0);
        lv_obj_set_style_text_font(l, &lv_font_montserrat_14, 0);
        lv_obj_set_pos(l, hx[i], 2);
    }

    // Rows
    for (int i = 0; i < REL_LOG_MAX; i++)
    {
        int ry = 55 + i * 32;
        rel_log_rows[i] = lv_obj_create(right);
        lv_obj_remove_style_all(rel_log_rows[i]);
        lv_obj_set_size(rel_log_rows[i], 500, 30);
        lv_obj_set_pos(rel_log_rows[i], 0, ry);
        lv_obj_set_style_bg_color(rel_log_rows[i], lv_color_hex(i % 2 == 0 ? 0xFFFFFF : 0xF8FAFC), 0);
        lv_obj_set_style_bg_opa(rel_log_rows[i], LV_OPA_COVER, 0);
        lv_obj_set_style_radius(rel_log_rows[i], 4, 0);
        lv_obj_clear_flag(rel_log_rows[i], LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(rel_log_rows[i], LV_OBJ_FLAG_HIDDEN);

        rel_log_id[i] = lv_label_create(rel_log_rows[i]);
        lv_label_set_text(rel_log_id[i], "");
        // O os_id do central tem ate 60 caracteres e a coluna seguinte comeca
        // em x=100: sem largura + LONG_DOT ele passaria por cima dos itens.
        lv_obj_set_width(rel_log_id[i], 92);
        lv_label_set_long_mode(rel_log_id[i], LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_color(rel_log_id[i], lv_color_hex(0x1E40AF), 0);
        lv_obj_set_style_text_font(rel_log_id[i], &lv_font_montserrat_14, 0);
        lv_obj_set_pos(rel_log_id[i], 5, 6);

        rel_log_itens[i] = lv_label_create(rel_log_rows[i]);
        lv_label_set_text(rel_log_itens[i], "");
        lv_obj_set_width(rel_log_itens[i], 280);
        lv_label_set_long_mode(rel_log_itens[i], LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_color(rel_log_itens[i], lv_color_hex(0x334155), 0);
        lv_obj_set_style_text_font(rel_log_itens[i], &lv_font_montserrat_14, 0);
        lv_obj_set_pos(rel_log_itens[i], 100, 6);

        rel_log_status[i] = lv_label_create(rel_log_rows[i]);
        lv_label_set_text(rel_log_status[i], "");
        lv_obj_set_style_text_font(rel_log_status[i], &lv_font_montserrat_14, 0);
        lv_obj_set_pos(rel_log_status[i], 400, 6);
    }
}

void update_relatorio_ui()
{
    int total = app.num_ordens;
    int pend = 0, concl = 0;
    for (int i = 0; i < app.num_ordens; i++)
    {
        if (strcmp(app.ordens[i].status, "Pronto") == 0)
            concl++;
        else if (strcmp(app.ordens[i].status, "Aguardando") == 0)
            pend++;
    }
    lv_label_set_text_fmt(rel_total_lbl, "Total: %d", total);
    lv_label_set_text_fmt(rel_pend_lbl, "Pendentes: %d", pend);
    lv_label_set_text_fmt(rel_concl_lbl, "Concluidas: %d", concl);

    for (int i = 0; i < REL_LOG_MAX; i++)
    {
        if (i < app.num_ordens)
        {
            lv_obj_clear_flag(rel_log_rows[i], LV_OBJ_FLAG_HIDDEN);
            lv_label_set_text(rel_log_id[i], app.ordens[i].id);

            char itens_fmt[80] = "";
            char buf[MAX_ITENS_RESUMO_LEN];
            strncpy(buf, app.ordens[i].itens, sizeof(buf) - 1);
            buf[sizeof(buf) - 1] = '\0';
            char *tok = strtok(buf, ";");
            bool first = true;
            while (tok)
            {
                char *pipe = strchr(tok, '|');
                if (pipe)
                {
                    *pipe = '\0';
                    char part[48];
                    snprintf(part, sizeof(part), "%s%s x%s", first ? "" : ", ", tok, pipe + 1);
                    strncat(itens_fmt, part, sizeof(itens_fmt) - strlen(itens_fmt) - 1);
                    first = false;
                }
                tok = strtok(NULL, ";");
            }
            lv_label_set_text(rel_log_itens[i], itens_fmt[0] ? itens_fmt : app.ordens[i].itens);

            const char *st = app.ordens[i].status;
            lv_label_set_text(rel_log_status[i], st);
            uint32_t c = 0x64748B;
            if (strcmp(st, "Pronto") == 0)
                c = 0x166534;
            else if (strcmp(st, "Separando") == 0)
                c = 0x1D4ED8;
            else if (strcmp(st, "Aguardando") == 0)
                c = 0x92400E;
            else if (strcmp(st, "Pausado") == 0)
                c = 0x9A3412;
            else if (strcmp(st, "Erro") == 0)
                c = 0xDC2626;
            else if (strcmp(st, "Cancelado") == 0)
                c = 0x475569;
            lv_obj_set_style_text_color(rel_log_status[i], lv_color_hex(c), 0);
        }
        else
        {
            lv_obj_add_flag(rel_log_rows[i], LV_OBJ_FLAG_HIDDEN);
        }
    }
}

// ============================================================
// STATUS PAGE
// ============================================================
static void build_status_page()
{
    page_status = lv_obj_create(ui_body);
    lv_obj_remove_style_all(page_status);
    lv_obj_set_size(page_status, 800, 412);
    lv_obj_set_pos(page_status, 0, 0);
    lv_obj_set_style_bg_opa(page_status, LV_OPA_TRANSP, 0);
    lv_obj_clear_flag(page_status, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *panel = lv_obj_create(page_status);
    lv_obj_remove_style_all(panel);
    lv_obj_set_size(panel, 400, 300);
    lv_obj_add_style(panel, &sty_panel, 0);
    lv_obj_set_pos(panel, 200, 30);
    lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *stitle = lv_label_create(panel);
    lv_label_set_text(stitle, LV_SYMBOL_SETTINGS " Status da Maquina");
    lv_obj_set_style_text_color(stitle, lv_color_hex(0x0F172A), 0);
    lv_obj_set_style_text_font(stitle, &lv_font_montserrat_20, 0);
    lv_obj_set_pos(stitle, 0, 0);

    st_icon = lv_label_create(panel);
    lv_label_set_text(st_icon, LV_SYMBOL_OK);
    lv_obj_set_style_text_color(st_icon, lv_color_hex(0x166534), 0);
    lv_obj_set_style_text_font(st_icon, &lv_font_montserrat_24, 0);
    lv_obj_set_pos(st_icon, 145, 55);

    st_title = lv_label_create(panel);
    lv_label_set_text(st_title, "OPERACIONAL");
    lv_obj_set_style_text_color(st_title, lv_color_hex(0x166534), 0);
    lv_obj_set_style_text_font(st_title, &lv_font_montserrat_24, 0);
    lv_obj_set_pos(st_title, 80, 95);

    st_msg = lv_label_create(panel);
    lv_label_set_text(st_msg, "Operacional");
    lv_obj_set_width(st_msg, 360);
    lv_label_set_long_mode(st_msg, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_color(st_msg, lv_color_hex(0x64748B), 0);
    lv_obj_set_style_text_font(st_msg, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_align(st_msg, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_pos(st_msg, 0, 145);

    lv_obj_t *net_title = lv_label_create(panel);
    lv_label_set_text(net_title, "Rede:");
    lv_obj_set_style_text_color(net_title, lv_color_hex(0x64748B), 0);
    lv_obj_set_style_text_font(net_title, &lv_font_montserrat_16, 0);
    lv_obj_set_pos(net_title, 120, 200);

    st_net = lv_label_create(panel);
    lv_label_set_text(st_net, "ONLINE");
    apply_badge(st_net, "ONLINE");
    lv_obj_set_pos(st_net, 180, 198);
}

void update_status_ui()
{
    if (app.maquina_ok)
    {
        lv_label_set_text(st_icon, LV_SYMBOL_OK);
        lv_obj_set_style_text_color(st_icon, lv_color_hex(0x166534), 0);
        lv_label_set_text(st_title, "OPERACIONAL");
        lv_obj_set_style_text_color(st_title, lv_color_hex(0x166534), 0);
    }
    else
    {
        lv_label_set_text(st_icon, LV_SYMBOL_WARNING);
        lv_obj_set_style_text_color(st_icon, lv_color_hex(0xDC2626), 0);
        lv_label_set_text(st_title, "ERRO");
        lv_obj_set_style_text_color(st_title, lv_color_hex(0xDC2626), 0);
    }
    lv_label_set_text(st_msg, app.status_msg);
    lv_label_set_text(st_net, app.online ? "ONLINE" : "OFFLINE");
    apply_badge(st_net, app.online ? "ONLINE" : "OFFLINE");
    lv_obj_set_pos(st_net, 180, 198);
}

// ============================================================
// MENU
// ============================================================
static void menu_anim_x_cb(void *var, int32_t v)
{
    lv_obj_set_x((lv_obj_t *)var, v);
}

static void close_menu()
{
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, ui_menu_panel);
    lv_anim_set_values(&a, lv_obj_get_x(ui_menu_panel), -200);
    lv_anim_set_time(&a, 180);
    lv_anim_set_exec_cb(&a, menu_anim_x_cb);
    lv_anim_start(&a);
    lv_obj_add_flag(ui_menu_overlay, LV_OBJ_FLAG_HIDDEN);
}

static void menu_overlay_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    close_menu();
}

static void menu_item_cb(lv_event_t *e)
{
    const char *key = (const char *)lv_event_get_user_data(e);
    if (strcmp(key, "dashboard") == 0)
        show_page(PAGE_DASHBOARD);
    else if (strcmp(key, "dispensers") == 0)
        show_page(PAGE_DISPENSERS);
    else if (strcmp(key, "relatorio") == 0)
        show_page(PAGE_RELATORIO);
    else if (strcmp(key, "status") == 0)
        show_page(PAGE_STATUS);
    else if (strcmp(key, "sair") == 0)
    {
        app.logado = false;
        app.operador_logado = -1;
        lv_obj_add_flag(ui_header, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(ui_body, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(page_login, LV_OBJ_FLAG_HIDDEN);
        lv_textarea_set_text(login_ta, "");
        lv_label_set_text(login_msg, "Digite a senha de acesso");
        lv_obj_set_style_text_color(login_msg, lv_color_hex(0x64748B), 0);
        currentPage = PAGE_LOGIN;
    }
    close_menu();
}

static void menu_btn_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    if (lv_obj_get_x(ui_menu_panel) >= 0)
    {
        close_menu();
        return;
    }
    lv_obj_clear_flag(ui_menu_overlay, LV_OBJ_FLAG_HIDDEN);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, ui_menu_panel);
    lv_anim_set_values(&a, lv_obj_get_x(ui_menu_panel), 0);
    lv_anim_set_time(&a, 180);
    lv_anim_set_exec_cb(&a, menu_anim_x_cb);
    lv_anim_start(&a);
}

static lv_obj_t *make_menu_item(lv_obj_t *parent, const char *text, const char *key, int y)
{
    lv_obj_t *btn = lv_btn_create(parent);
    lv_obj_set_size(btn, 168, 44);
    lv_obj_set_pos(btn, 0, y);
    lv_obj_set_style_bg_color(btn, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_bg_opa(btn, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(btn, lv_color_hex(0xDCE3EA), 0);
    lv_obj_set_style_border_width(btn, 1, 0);
    lv_obj_set_style_radius(btn, 10, 0);
    lv_obj_add_event_cb(btn, menu_item_cb, LV_EVENT_CLICKED, (void *)key);

    lv_obj_t *lbl = lv_label_create(btn);
    lv_label_set_text(lbl, text);
    lv_obj_set_style_text_color(lbl, lv_color_hex(0x0F172A), 0);
    lv_obj_set_style_text_font(lbl, &lv_font_montserrat_16, 0);
    lv_obj_center(lbl);
    return btn;
}

// ============================================================
// PAGE NAVIGATION
// ============================================================
void show_page(AppPage page)
{
    currentPage = page;
    if (page_dashboard)
        lv_obj_add_flag(page_dashboard, LV_OBJ_FLAG_HIDDEN);
    if (page_dispensers)
        lv_obj_add_flag(page_dispensers, LV_OBJ_FLAG_HIDDEN);
    if (page_relatorio)
        lv_obj_add_flag(page_relatorio, LV_OBJ_FLAG_HIDDEN);
    if (page_status)
        lv_obj_add_flag(page_status, LV_OBJ_FLAG_HIDDEN);

    switch (page)
    {
    case PAGE_DASHBOARD:
        lv_obj_clear_flag(page_dashboard, LV_OBJ_FLAG_HIDDEN);
        update_dashboard_ui();
        break;
    case PAGE_DISPENSERS:
        lv_obj_clear_flag(page_dispensers, LV_OBJ_FLAG_HIDDEN);
        update_dispensers_ui();
        break;
    case PAGE_RELATORIO:
        lv_obj_clear_flag(page_relatorio, LV_OBJ_FLAG_HIDDEN);
        update_relatorio_ui();
        break;
    case PAGE_STATUS:
        lv_obj_clear_flag(page_status, LV_OBJ_FLAG_HIDDEN);
        update_status_ui();
        break;
    default:
        break;
    }
}

// ============================================================
// MAIN UI BUILDER
// ============================================================
void build_ui()
{
    init_styles();

    ui_screen = lv_screen_active();
    lv_obj_set_style_bg_color(ui_screen, lv_color_hex(0xEEF2F6), 0);
    lv_obj_set_style_bg_opa(ui_screen, LV_OPA_COVER, 0);
    lv_obj_clear_flag(ui_screen, LV_OBJ_FLAG_SCROLLABLE);

    // Header (hidden until login)
    ui_header = lv_obj_create(ui_screen);
    lv_obj_remove_style_all(ui_header);
    lv_obj_set_size(ui_header, 800, 68);
    lv_obj_add_style(ui_header, &sty_header, 0);
    lv_obj_set_pos(ui_header, 0, 0);
    lv_obj_clear_flag(ui_header, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(ui_header, LV_OBJ_FLAG_HIDDEN);

    ui_menu_btn = lv_btn_create(ui_header);
    lv_obj_remove_style_all(ui_menu_btn);
    lv_obj_set_size(ui_menu_btn, 42, 42);
    lv_obj_add_style(ui_menu_btn, &sty_menu_btn, 0);
    lv_obj_set_pos(ui_menu_btn, 14, 13);
    lv_obj_add_event_cb(ui_menu_btn, menu_btn_cb, LV_EVENT_CLICKED, NULL);

    lv_obj_t *menu_icon = lv_label_create(ui_menu_btn);
    lv_label_set_text(menu_icon, LV_SYMBOL_LIST);
    lv_obj_set_style_text_color(menu_icon, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_text_font(menu_icon, &lv_font_montserrat_20, 0);
    lv_obj_center(menu_icon);

    ui_title_label = lv_label_create(ui_header);
    lv_label_set_text(ui_title_label, "APSEN - Dispensacao");
    lv_obj_set_style_text_color(ui_title_label, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_text_font(ui_title_label, &lv_font_montserrat_20, 0);
    lv_obj_set_pos(ui_title_label, 70, 10);

    ui_operator_label = lv_label_create(ui_header);
    lv_label_set_text(ui_operator_label, "Operador: --");
    lv_obj_set_style_text_color(ui_operator_label, lv_color_hex(0xBFDBFE), 0);
    lv_obj_set_style_text_font(ui_operator_label, &lv_font_montserrat_14, 0);
    lv_obj_set_pos(ui_operator_label, 70, 38);

    ui_net_badge = lv_label_create(ui_header);
    lv_label_set_text(ui_net_badge, "ONLINE");
    lv_obj_add_style(ui_net_badge, &sty_badge_info, 0);
    lv_obj_set_pos(ui_net_badge, 660, 8);

    ui_time_label = lv_label_create(ui_header);
    lv_label_set_text(ui_time_label, "--:--");
    lv_obj_set_style_text_color(ui_time_label, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_text_font(ui_time_label, &lv_font_montserrat_16, 0);
    lv_obj_set_pos(ui_time_label, 720, 36);

    // Body (hidden until login)
    ui_body = lv_obj_create(ui_screen);
    lv_obj_remove_style_all(ui_body);
    lv_obj_set_size(ui_body, 800, 412);
    lv_obj_set_pos(ui_body, 0, 68);
    lv_obj_set_style_bg_opa(ui_body, LV_OPA_TRANSP, 0);
    lv_obj_clear_flag(ui_body, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(ui_body, LV_OBJ_FLAG_HIDDEN);

    // Build all pages
    build_dashboard_page();
    build_dispensers_page();
    build_relatorio_page();
    build_status_page();

    show_page(PAGE_DASHBOARD);

    // Menu overlay
    ui_menu_overlay = lv_obj_create(ui_screen);
    lv_obj_remove_style_all(ui_menu_overlay);
    lv_obj_set_size(ui_menu_overlay, 800, 480);
    lv_obj_set_pos(ui_menu_overlay, 0, 0);
    lv_obj_set_style_bg_color(ui_menu_overlay, lv_color_hex(0x000000), 0);
    lv_obj_set_style_bg_opa(ui_menu_overlay, LV_OPA_30, 0);
    lv_obj_add_flag(ui_menu_overlay, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_event_cb(ui_menu_overlay, menu_overlay_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_clear_flag(ui_menu_overlay, LV_OBJ_FLAG_SCROLLABLE);

    // Menu panel
    ui_menu_panel = lv_obj_create(ui_screen);
    lv_obj_remove_style_all(ui_menu_panel);
    lv_obj_set_size(ui_menu_panel, 200, 480);
    lv_obj_set_pos(ui_menu_panel, -200, 0);
    lv_obj_add_style(ui_menu_panel, &sty_menu_panel, 0);
    lv_obj_set_style_pad_all(ui_menu_panel, 14, 0);
    lv_obj_clear_flag(ui_menu_panel, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *menu_title = lv_label_create(ui_menu_panel);
    lv_label_set_text(menu_title, "Menu");
    lv_obj_set_style_text_color(menu_title, lv_color_hex(0x0F172A), 0);
    lv_obj_set_style_text_font(menu_title, &lv_font_montserrat_20, 0);
    lv_obj_set_pos(menu_title, 0, 8);

    make_menu_item(ui_menu_panel, LV_SYMBOL_HOME " Dashboard", "dashboard", 48);
    make_menu_item(ui_menu_panel, LV_SYMBOL_SETTINGS " Dispensers", "dispensers", 100);
    make_menu_item(ui_menu_panel, LV_SYMBOL_LIST " Relatorio", "relatorio", 152);
    make_menu_item(ui_menu_panel, LV_SYMBOL_EYE_OPEN " Status", "status", 204);

    lv_obj_t *sair_btn = make_menu_item(ui_menu_panel, LV_SYMBOL_POWER " Sair", "sair", 290);
    lv_obj_set_style_bg_color(sair_btn, lv_color_hex(0xFEE2E2), 0);

    // Login page (on top, full screen)
    build_login_page();

    // Alert popup
    build_alert_popup();

    // Dispenser edit popups
    build_disp_pin_popup();
    build_disp_med_popup();

    // Screensaver (top-most layer, shown at boot)
    build_screensaver_page();
    lv_obj_add_flag(page_login, LV_OBJ_FLAG_HIDDEN);
}

// ============================================================
// UPDATE ALL
// ============================================================
void update_all_ui()
{
    lv_label_set_text(ui_time_label, app.hora);
    lv_label_set_text(ui_net_badge, app.online ? "ONLINE" : "OFFLINE");

    update_dashboard_ui();
    update_dispensers_ui();
    update_relatorio_ui();
    update_status_ui();
}

// ============================================================
// TIMER UPDATE (called every second)
// ============================================================
static void timer_update_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    if (!app.logado)
        return;

    if (app.ordem_atual >= 0 && app.ordem_atual < app.num_ordens &&
        strcmp(app.ordens[app.ordem_atual].status, "Separando") == 0)
    {
        unsigned long elapsed = millis() - app.ordens[app.ordem_atual].tempo_inicio;
        char buf[8];
        format_elapsed(elapsed, buf, sizeof(buf));
        if (currentPage == PAGE_DASHBOARD)
            lv_label_set_text(dash_timer, buf);
    }
}

// ============================================================
// SERIAL USB — COMUNICAÇÃO COM O BACKEND (JSON via cabo USB)
// Protocolo: uma linha JSON por mensagem, terminada com '\n'.
// Único canal de comunicação com o backend (substitui WiFi/MQTT).
//
// ESP32 -> Backend:
//   {"cmd":"ping"}                                  -> {"resp":"pong","epoch":...}
//   {"cmd":"get_ordens"}                             -> {"resp":"ordens","data":[...]}
//   {"cmd":"get_catalogo"}                           -> {"resp":"catalogo","data":[...]}
//   {"cmd":"get_operadores"}                         -> {"resp":"operadores","data":[...]}
//   {"cmd":"get_dispensers"}                         -> {"resp":"dispensers","data":[...]}
//   {"cmd":"validar_operador","nome":..,"pin":..}    -> {"resp":"operador","ok":bool,..}
//   {"cmd":"set_status","numero_os":..,"status":..}  -> {"resp":"ok","ok":bool}
//   {"cmd":"sync_dispensers","itens":[...]}          -> {"resp":"ok","ok":bool}
//   {"cmd":"set_dispenser_med","slot":..,"nome":..}  -> {"resp":"ok","ok":bool}
//   {"event":"historico", ...} / {"event":"desvio", ...} / {"event":"ordem_concluida", ...}
//
// `validar_operador` espera ate 5 s, e nao os 800 ms dos demais: quem confere o
// PIN e o BACKEND, e conferir hash custa ~300 ms POR OPERADOR ativo de proposito
// (ver CLAUDE.md). Encurtar o hash para caber no timeout trocaria seguranca por
// latencia que ninguem percebe.
//
// Backend -> ESP32 (nao solicitado):
//   {"push":"ordem_status","numero_os":"OS1","status":"Em Processo"}
//   {"push":"dispensers","data":[...]}   (estoque de um slot mudou na web)
//
// Comandos de debug aceitos do simulador manual (simulador_serial.py):
//   {"cmd":"nova_ordem","id":"OS001","itens":"Med1|5;Med2|3","destino":"UTI","lote":"LOT001"}
//   {"cmd":"dispenser","slot":1,"nome":"Paracetamol","quantidade":45,"capacidade":60,"minimo":10}
//   {"cmd":"status"}
//
// Linhas de debug (Serial.printf/println) NÃO começam com '{' —
// o backend/script Python as exibe separadamente sem confundir com JSON.
// ============================================================
// 4096: precisa caber a lista de dispensers (8 slots, ~1.1KB) e o catalogo
// completo (ate MAX_CATALOGO itens), sem truncar a linha JSON no meio.
static char s2_buf[4096];
static int s2_len = 0;
static JsonDocument s2_last_resp;
static bool s2_have_resp = false;
static const char *s2_awaited_resp_type = nullptr;

static void serial2_send(const char *json)
{
    Serial.println(json);
}

static void serial2_send_evento_ordem_concluida(const OrdemExpedicao &o)
{
    const char *op = (app.operador_logado >= 0) ? operadores[app.operador_logado].nome : "";
    unsigned long tempo_seg = (o.tempo_inicio > 0) ? (millis() - o.tempo_inicio) / 1000 : 0;
    char buf[256];
    snprintf(buf, sizeof(buf),
             "{\"event\":\"ordem_concluida\",\"id\":\"%s\",\"operador\":\"%s\",\"tempo_seg\":%lu}",
             o.id, op, tempo_seg);
    serial2_send(buf);
}

// Trata mudanca de status empurrada pelo backend (dashboard web alterou uma ordem).
// Espelha exatamente a regra que ja existia no antigo callback MQTT.
static void handle_push_ordem_status(JsonObject doc)
{
    const char *numero_os = doc["numero_os"] | "";
    const char *status = doc["status"] | "";

    Serial.printf("Serial: status recebido do backend: %s -> %s\n", numero_os, status);

    int idx = -1;
    for (int i = 0; i < app.num_ordens; i++)
    {
        if (strcmp(app.ordens[i].id, numero_os) == 0)
        {
            idx = i;
            break;
        }
    }

    if (idx < 0)
    {
        force_fetch_ordens = true;
        return;
    }

    // Ordem espelhada so ESPELHA: o status entra na linha, mas ela nunca vira
    // `ordem_atual` — o painel de ordem ativa oferece Pausar/Concluir, e a
    // celula nao aceita nenhum dos dois.
    if (app.ordens[idx].central)
    {
        copy_trunc(app.ordens[idx].status, sizeof(app.ordens[idx].status),
                   status_backend_para_painel(status));
        if (app.ordem_atual == idx)
            app.ordem_atual = -1;
    }
    else if (strcmp(status, "Em Processo") == 0)
    {
        if (strcmp(app.ordens[idx].status, "Pausado") == 0)
        {
            strcpy(app.ordens[idx].status, "Separando");
            app.ordens[idx].tempo_inicio = millis() - app.ordens[idx].tempo_acumulado;
            app.ordem_atual = idx;
        }
        else if (strcmp(app.ordens[idx].status, "Aguardando") == 0)
        {
            strcpy(app.ordens[idx].status, "Separando");
            app.ordens[idx].tempo_inicio = millis();
            app.ordens[idx].tempo_acumulado = 0;
            get_datetime(app.ordens[idx].hora_inicio, sizeof(app.ordens[idx].hora_inicio));
            app.ordem_atual = idx;
        }
    }
    else if (strcmp(status, "Pausado") == 0)
    {
        if (strcmp(app.ordens[idx].status, "Separando") == 0)
        {
            app.ordens[idx].tempo_acumulado = millis() - app.ordens[idx].tempo_inicio;
            strcpy(app.ordens[idx].status, "Pausado");
            if (app.ordem_atual == idx)
                app.ordem_atual = -1;
        }
    }
    else if (strcmp(status, "Concluido") == 0)
    {
        strcpy(app.ordens[idx].status, "Pronto");
        if (app.ordem_atual == idx)
            app.ordem_atual = -1;
    }
    else if (strcmp(status, "Erro") == 0 || strcmp(status, "Cancelado") == 0)
    {
        // Estados terminais novos no vocabulario do painel. Sem este ramo a
        // ordem ficaria parada em "Aguardando"/"Separando" para sempre, com o
        // operador esperando uma execucao que ja acabou.
        copy_trunc(app.ordens[idx].status, sizeof(app.ordens[idx].status), status);
        if (app.ordem_atual == idx)
            app.ordem_atual = -1;
    }

    update_dashboard_ui();
    update_relatorio_ui();
    if (currentPage == PAGE_DISPENSERS)
        update_dispensers_ui();
    if (currentPage == PAGE_STATUS)
        update_status_ui();
}

static void handle_debug_cmd(JsonDocument &doc)
{
    const char *cmd = doc["cmd"] | "";

    if (strcmp(cmd, "nova_ordem") == 0)
    {
        if (app.num_ordens >= MAX_ORDENS)
        {
            serial2_send("{\"ack\":\"erro\",\"msg\":\"lista cheia\"}");
            return;
        }
        OrdemExpedicao &o = app.ordens[app.num_ordens];
        // Ausente = "local": o simulador serial e qualquer backend anterior a
        // integracao nao mandam o campo, e nao e por isso que a ordem deles
        // deve virar so-leitura.
        const char *origem = doc["origem"] | "local";
        o.central = (strcmp(origem, "central") == 0);
        copy_trunc(o.id, sizeof(o.id), doc["id"] | "---");
        copy_trunc(o.itens, sizeof(o.itens), doc["itens"] | "");
        copy_trunc(o.destino, sizeof(o.destino), doc["destino"] | "");
        const char *lote_in = doc["lote"] | "";
        if (lote_in[0])
            copy_trunc(o.lote, sizeof(o.lote), lote_in);
        else
            snprintf(o.lote, sizeof(o.lote), "LOT-%s", o.id);
        copy_trunc(o.status, sizeof(o.status),
                   o.central ? status_backend_para_painel(doc["status"] | "Pendente")
                             : "Aguardando");
        o.tempo_inicio = 0;
        o.tempo_acumulado = 0;
        o.hora_inicio[0] = '\0';
        app.num_ordens++;
        Serial.printf("Serial2: nova ordem %s [%s]\n", o.id, o.central ? "central" : "local");
        serial2_send("{\"ack\":\"ok\",\"cmd\":\"nova_ordem\"}");
        update_relatorio_ui();
        if (currentPage == PAGE_DASHBOARD)
            update_dashboard_ui();
    }
    else if (strcmp(cmd, "dispenser") == 0)
    {
        int slot = (doc["slot"] | 1) - 1;
        if (slot < 0 || slot >= NUM_DISPENSERS)
        {
            serial2_send("{\"ack\":\"erro\",\"msg\":\"slot invalido\"}");
            return;
        }
        if (!doc["nome"].isNull())
            strncpy(app.dispensers[slot].nome, doc["nome"] | "", sizeof(app.dispensers[0].nome) - 1);
        if (!doc["quantidade"].isNull())
            app.dispensers[slot].quantidade = doc["quantidade"];
        if (!doc["capacidade"].isNull())
            app.dispensers[slot].capacidade = doc["capacidade"];
        if (!doc["minimo"].isNull())
            app.dispensers[slot].minimo = doc["minimo"];
        app.dispensers[slot].nome[sizeof(app.dispensers[0].nome) - 1] = '\0';
        Serial.printf("Serial2: dispenser %d atualizado\n", slot + 1);
        serial2_send("{\"ack\":\"ok\",\"cmd\":\"dispenser\"}");
        update_dispensers_ui();
        update_dashboard_ui();
    }
    else if (strcmp(cmd, "status") == 0)
    {
        const char *op = (app.operador_logado >= 0) ? operadores[app.operador_logado].nome : "";
        char resp[256];
        snprintf(resp, sizeof(resp),
                 "{\"ack\":\"status\",\"logado\":%s,\"operador\":\"%s\","
                 "\"ordens\":%d,\"online\":%s,\"maquina_ok\":%s}",
                 app.logado ? "true" : "false", op,
                 app.num_ordens,
                 app.online ? "true" : "false",
                 app.maquina_ok ? "true" : "false");
        serial2_send(resp);
    }
    else
    {
        Serial.printf("Serial2: cmd desconhecido: %s\n", cmd);
        serial2_send("{\"ack\":\"erro\",\"msg\":\"cmd desconhecido\"}");
    }
}

// Despacha uma linha JSON completa vinda do backend (ou do simulador manual),
// distinguindo pela chave presente: "resp" (resposta a um serial_request em
// andamento), "push" (evento nao solicitado, ex: status de ordem mudou no
// dashboard web) ou "cmd" (comando de debug do simulador manual).
static void handle_serial_line(const char *raw, int len)
{
    JsonDocument doc;
    if (deserializeJson(doc, raw, len))
    {
        Serial.printf("Serial2: JSON invalido: %.40s\n", raw);
        return;
    }

    if (!doc["resp"].isNull())
    {
        const char *resp_type = doc["resp"];
        if (s2_awaited_resp_type && strcmp(resp_type, s2_awaited_resp_type) == 0)
        {
            s2_last_resp = doc;
            s2_have_resp = true;
        }
        return;
    }

    if (!doc["push"].isNull())
    {
        const char *push_type = doc["push"];
        if (strcmp(push_type, "ordem_status") == 0)
            handle_push_ordem_status(doc.as<JsonObject>());
        else if (strcmp(push_type, "dispensers") == 0)
            handle_push_dispensers(doc.as<JsonObject>());
        return;
    }

    if (!doc["cmd"].isNull())
    {
        handle_debug_cmd(doc);
        return;
    }

    Serial.printf("Serial2: linha nao reconhecida: %.40s\n", raw);
}

static void check_serial2()
{
    while (Serial.available())
    {
        char c = Serial.read();
        if (c == '\n' || c == '\r')
        {
            if (s2_len > 0 && s2_buf[0] == '{')
            {
                s2_buf[s2_len] = '\0';
                handle_serial_line(s2_buf, s2_len);
                s2_len = 0;
            }
            else
            {
                s2_len = 0;
            }
        }
        else if (s2_len < (int)sizeof(s2_buf) - 1)
        {
            s2_buf[s2_len++] = c;
        }
    }
}

// Envia um comando e aguarda (com timeout) a resposta correspondente,
// drenando o buffer serial via check_serial2 — mensagens "push"/"cmd" que
// chegarem nesse meio-tempo continuam sendo despachadas normalmente.
static bool serial_request(const char *cmd_json, const char *expect_resp, JsonDocument &out, unsigned long timeout_ms)
{
    s2_have_resp = false;
    s2_awaited_resp_type = expect_resp;
    serial2_send(cmd_json);

    unsigned long start = millis();
    while (millis() - start < timeout_ms)
    {
        check_serial2();
        if (s2_have_resp)
        {
            out = s2_last_resp;
            s2_awaited_resp_type = nullptr;
            return true;
        }
    }
    s2_awaited_resp_type = nullptr;
    return false;
}

// ============================================================
// DATE/TIME HELPER
// ============================================================
static void get_datetime(char *buf, size_t len)
{
    struct tm ti;
    if (getLocalTime(&ti, 2000))
    {
        strftime(buf, len, "%d/%m/%Y %H:%M:%S", &ti);
    }
    else
    {
        unsigned long s = millis() / 1000;
        snprintf(buf, len, "T+%lum%02lus", s / 60, s % 60);
    }
}

static void update_clock()
{
    struct tm ti;
    if (getLocalTime(&ti, 200))
    {
        snprintf(app.hora, sizeof(app.hora), "%02d:%02d", ti.tm_hour, ti.tm_min);
        lv_label_set_text(ui_time_label, app.hora);
    }
}

// ============================================================
// SINCRONIZACAO DE HORA E HEARTBEAT (via Serial, substitui NTP/WiFi)
// ============================================================
// Manda um ping ao backend; se responder, ajusta o relogio interno (RTC do
// ESP-IDF) via settimeofday() com o epoch recebido e marca app.online=true.
// Chamada no setup() (com retry) e periodicamente no loop() — dobra como
// heartbeat e como correcao de deriva do relogio.
static void sync_time_with_backend()
{
    JsonDocument resp;
    if (serial_request("{\"cmd\":\"ping\"}", "pong", resp, 800))
    {
        long epoch = resp["epoch"] | 0;
        if (epoch > 0)
        {
            struct timeval tv = {.tv_sec = epoch, .tv_usec = 0};
            settimeofday(&tv, nullptr);
        }
        app.online = true;
    }
    else
    {
        app.online = false;
    }
}

static void do_login(int op_idx)
{
    app.operador_logado = op_idx;
    app.logado = true;
    lv_label_set_text_fmt(ui_operator_label, "Operador: %s", operadores[op_idx].nome);
    lv_obj_add_flag(page_login, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(ui_header, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(ui_body, LV_OBJ_FLAG_HIDDEN);
    show_page(PAGE_DASHBOARD);
    update_all_ui();
    api_log_historico(operadores[op_idx].nome, "Login", "Login via painel");
}

// ============================================================
// CATALOGO (via Serial)
// ============================================================
static void fetch_catalogo_api()
{
    if (!app.online)
        return;

    JsonDocument doc;
    if (!serial_request("{\"cmd\":\"get_catalogo\"}", "catalogo", doc))
    {
        Serial.println("Serial: catalogo falhou (timeout)");
        return;
    }

    JsonArray arr = doc["data"].as<JsonArray>();
    num_catalogo = 0;
    for (JsonVariant v : arr)
    {
        if (num_catalogo >= MAX_CATALOGO)
            break;
        const char *nome = nullptr;
        if (v.is<JsonObject>())
            nome = v["nome"] | (const char *)nullptr;
        else
            nome = v.as<const char *>();
        if (!nome)
            continue;
        strncpy(catalogo[num_catalogo], nome, 31);
        catalogo[num_catalogo][31] = '\0';
        num_catalogo++;
    }
    Serial.printf("Serial: %d medicamentos no catalogo\n", num_catalogo);
}

// ============================================================
// VALIDACAO DE PIN (via Serial)
// ============================================================
// O PIN digitado vai para o BACKEND e de la volta o veredito. O display nao
// compara nada: ele nao guarda mais PIN, nem em claro nem em hash.
//
// Devolve o indice do operador em `operadores[]`, ou -1 — PIN errado, backend
// fora do ar ou timeout. Quem distingue os casos na tela e `app.online`.
static int validar_pin_backend(const char *pin)
{
    if (!app.online || !pin || pin[0] == '\0')
        return -1;

    // O PIN entra num JSON montado a mao. O numpad so produz digitos, mas
    // confiar nisso e confiar numa TELA para proteger um PROTOCOLO: uma aspa
    // que chegasse aqui viraria uma linha JSON quebrada do outro lado do cabo.
    size_t n = strlen(pin);
    if (n == 0 || n > 12)
        return -1;
    for (size_t i = 0; i < n; i++)
        if (pin[i] < '0' || pin[i] > '9')
            return -1;

    char payload[64];
    snprintf(payload, sizeof(payload),
             "{\"cmd\":\"validar_operador\",\"pin\":\"%s\"}", pin);

    // 5 s, e nao os 800 ms do resto: conferir PIN e a UNICA resposta do backend
    // que demora de proposito. O hash e deliberadamente caro (~300 ms por
    // operador ativo, e um PIN errado percorre todos), porque e esse custo que
    // separa um arquivo .db levado no bolso de todos os PINs da bancada.
    // "O timeout de quem pergunta manda no de quem responde" vale aqui ao
    // contrario: com 800 ms o display desistiria de uma resposta correta e
    // diria BACKEND OFFLINE para quem digitou o PIN certo.
    JsonDocument resp;
    if (!serial_request(payload, "operador", resp, 5000))
    {
        Serial.println("Serial: validar_operador falhou (timeout)");
        return -1;
    }
    if (!(resp["ok"] | false))
        return -1;

    const char *nome = resp["nome"] | "";
    for (int i = 0; i < num_operadores; i++)
        if (operadores[i].ativo && strcmp(operadores[i].nome, nome) == 0)
            return i;

    // O backend aprovou alguem que a lista local nao tem: ela envelheceu (o
    // operador foi cadastrado depois do ultimo fetch). Recarrega e procura de
    // novo — negar PIN correto por lista velha manda o operador achar que errou
    // a senha, e ele vai repetir o acerto ate desistir.
    fetch_operadores_api();
    for (int i = 0; i < num_operadores; i++)
        if (operadores[i].ativo && strcmp(operadores[i].nome, nome) == 0)
            return i;
    return -1;
}

static void fetch_operadores_api()
{
    if (!app.online)
        return;

    JsonDocument doc;
    if (!serial_request("{\"cmd\":\"get_operadores\"}", "operadores", doc))
    {
        Serial.println("Serial: operadores falhou (timeout)");
        return;
    }

    JsonArray arr = doc["data"].as<JsonArray>();
    num_operadores = 0;
    for (JsonObject obj : arr)
    {
        if (num_operadores >= MAX_OPERADORES)
            break;
        // So o nome. A resposta nao traz mais `pin`, e campo que o backend
        // deixou de enviar nao pode continuar sendo lido "por compatibilidade":
        // seria guardar de novo o que esta mudanca tirou daqui.
        const char *nome = obj["nome"] | "";
        strncpy(operadores[num_operadores].nome, nome, sizeof(operadores[0].nome) - 1);
        operadores[num_operadores].nome[sizeof(operadores[0].nome) - 1] = '\0';
        operadores[num_operadores].ativo = true;
        num_operadores++;
    }
    Serial.printf("Serial: %d operadores carregados\n", num_operadores);
    save_operadores_to_sd();
}

static void api_update_dispenser_med(int slot, const char *nome)
{
    if (!app.online)
        return;

    JsonDocument doc;
    doc["cmd"] = "set_dispenser_med";
    doc["slot"] = slot;
    doc["nome"] = nome;
    char payload[128];
    serializeJson(doc, payload, sizeof(payload));

    JsonDocument resp;
    if (serial_request(payload, "ok", resp))
        Serial.printf("Serial: dispenser %d -> %s\n", slot, nome);
    else
        Serial.printf("Serial: atualizar medicamento falhou (timeout, slot %d)\n", slot);
}

// ============================================================
// FETCH ORDENS/DISPENSERS DO BACKEND (via Serial)
// ============================================================
// Copia um array de dispensers do backend para app.dispensers. Compartilhado
// entre o polling (resposta de get_dispensers) e o push nao solicitado, que o
// backend dispara assim que a visao altera o estoque — sem isso o operador
// esperaria ate o proximo ciclo de 5 s para ver o numero mudar.
static int aplicar_dispensers(JsonArray arr)
{
    int i = 0;
    for (JsonObject obj : arr)
    {
        if (i >= NUM_DISPENSERS)
            break;
        const char *nome = obj["nome"] | "";
        strncpy(app.dispensers[i].nome, nome, sizeof(app.dispensers[i].nome) - 1);
        app.dispensers[i].nome[sizeof(app.dispensers[i].nome) - 1] = '\0';
        app.dispensers[i].quantidade = obj["quantidade"] | 0;
        app.dispensers[i].capacidade = obj["capacidade"] | 60;
        app.dispensers[i].minimo = obj["minimo"] | 10;
        const char *lote = obj["lote"] | "";
        strncpy(app.dispensers[i].lote, lote, sizeof(app.dispensers[i].lote) - 1);
        app.dispensers[i].lote[sizeof(app.dispensers[i].lote) - 1] = '\0';
        const char *validade = obj["validade"] | "";
        strncpy(app.dispensers[i].validade, validade, sizeof(app.dispensers[i].validade) - 1);
        app.dispensers[i].validade[sizeof(app.dispensers[i].validade) - 1] = '\0';
        i++;
    }
    return i;
}

static void handle_push_dispensers(JsonObject doc)
{
    int n = aplicar_dispensers(doc["data"].as<JsonArray>());
    Serial.printf("Serial: push de %d dispensers (estoque mudou)\n", n);

    // Redesenha so a pagina que esta na frente do operador; as outras pegam o
    // valor novo quando forem abertas, porque leem de app.dispensers.
    switch (currentPage)
    {
    case PAGE_DASHBOARD:
        update_dashboard_ui();
        break;
    case PAGE_DISPENSERS:
        update_dispensers_ui();
        break;
    default:
        break;
    }
}

static void fetch_dispensers_api()
{
    if (!app.online)
        return;

    JsonDocument doc;
    if (!serial_request("{\"cmd\":\"get_dispensers\"}", "dispensers", doc))
    {
        Serial.println("Serial: dispensers falhou (timeout)");
        return;
    }

    int i = aplicar_dispensers(doc["data"].as<JsonArray>());
    Serial.printf("Serial: %d dispensers carregados\n", i);
}

static unsigned long last_api_fetch = 0;

static void fetch_ordens_api()
{
    if (!app.online)
        return;

    JsonDocument doc;
    if (!serial_request("{\"cmd\":\"get_ordens\"}", "ordens", doc))
    {
        Serial.println("Serial: fetch ordens falhou (timeout)");
        return;
    }

    JsonArray arr = doc["data"].as<JsonArray>();
    int count = 0;
    bool mudou = false; // status de ordem espelhada que ja estava na lista

    for (JsonObject obj : arr)
    {
        if (count >= MAX_ORDENS)
            break;

        const char *num_os = obj["numero_os"] | "---";
        const char *dest = obj["destino"] | "";
        const char *status = obj["status"] | "Pendente";
        // Ausente = "local": o simulador serial e qualquer backend anterior a
        // integracao nao mandam o campo, e nao e por isso que a ordem deles
        // deve virar so-leitura.
        const char *origem = obj["origem"] | "local";
        const bool e_central = (strcmp(origem, "central") == 0);

        int existente = -1;
        for (int i = 0; i < app.num_ordens; i++)
        {
            if (strcmp(app.ordens[i].id, num_os) == 0)
            {
                existente = i;
                break;
            }
        }
        if (existente >= 0)
        {
            // Ordem LOCAL conhecida nao se toca: quem manda no estado dela e o
            // display, e o backend so a conhece pelo que o display contou.
            //
            // Ordem do CENTRAL conhecida tem o status refrescado aqui. O push
            // `ordem_status` ja faz isso, e este caminho e a rede de seguranca
            // dele: push e uma linha serial que pode se perder num reset do
            // ESP32 ou numa reconexao da porta, e o preco de perde-la seria a
            // ordem congelar na tela no status em que foi vista pela primeira
            // vez — o operador esperando uma execucao que ja mudou de fase.
            if (app.ordens[existente].central)
            {
                const char *novo = status_backend_para_painel(status);
                if (strcmp(app.ordens[existente].status, novo) != 0)
                {
                    copy_trunc(app.ordens[existente].status,
                               sizeof(app.ordens[existente].status), novo);
                    Serial.printf("API: ordem %s (central) -> %s\n", num_os, novo);
                    mudou = true;
                }
            }
            continue;
        }

        // Buffer cheio: recicla o slot de uma ordem que ja terminou — de
        // qualquer maneira, inclusive abortada pela celula — em vez de travar
        // o recebimento de novas ordens pra sempre.
        int slot = -1;
        if (app.num_ordens < MAX_ORDENS)
        {
            slot = app.num_ordens;
            app.num_ordens++;
        }
        else
        {
            for (int i = 0; i < app.num_ordens; i++)
            {
                if (ordem_terminal(app.ordens[i].status))
                {
                    slot = i;
                    break;
                }
            }
        }
        if (slot < 0)
            continue; // nenhum slot livre nem reciclavel

        OrdemExpedicao &o = app.ordens[slot];
        o.central = e_central;
        if (!copy_trunc(o.id, sizeof(o.id), num_os))
            Serial.printf("API: os_id maior que %d bytes, truncado: %s\n",
                          (int)sizeof(o.id) - 1, o.id);

        // Monta o resumo item a item e para no ITEM que nao couber, marcando o
        // corte com reticencias. Cortar no meio de um item deixaria um par
        // "nome|qtd" pela metade, que descontar_itens_ordem leria como outra
        // quantidade — truncamento que vira erro de estoque, e nao texto
        // cortado na tela.
        o.itens[0] = '\0';
        JsonArray lista = obj["itens_lista"];
        bool first = true;
        for (JsonObject item : lista)
        {
            const char *med_name = item["med"] | "";
            int qtd = item["qtd"] | 0;
            char part[64];
            snprintf(part, sizeof(part), "%s%s|%d", first ? "" : ";", med_name, qtd);
            size_t usado = strlen(o.itens);
            // +4: o espaco das reticencias, para o caso de o proximo item
            // tambem nao caber.
            if (usado + strlen(part) + 4 >= sizeof(o.itens))
            {
                strncat(o.itens, first ? "..." : ";...", sizeof(o.itens) - usado - 1);
                Serial.printf("API: resumo de itens da ordem %s truncado\n", num_os);
                break;
            }
            strncat(o.itens, part, sizeof(o.itens) - usado - 1);
            first = false;
        }

        copy_trunc(o.destino, sizeof(o.destino), dest);
        snprintf(o.lote, sizeof(o.lote), "LOT-%s", num_os);
        // Ordem local mantem o comportamento de sempre: entra como "Aguardando"
        // e quem a move e o operador. Ordem do central entra com o status que a
        // celula reporta — e a unica forma de o display mostrar o que ela faz
        // agora, ja que o painel nunca vai comandar essa ordem.
        copy_trunc(o.status, sizeof(o.status),
                   e_central ? status_backend_para_painel(status) : "Aguardando");
        o.tempo_inicio = 0;
        o.tempo_acumulado = 0;
        o.hora_inicio[0] = '\0';
        count++;

        Serial.printf("API: nova ordem %s [%s] - itens: %s\n",
                      num_os, e_central ? "central" : "local", o.itens);
    }

    if (count > 0 || mudou)
    {
        update_relatorio_ui();
        if (currentPage == PAGE_DASHBOARD)
            update_dashboard_ui();
    }
}

static void api_update_status(const char *numero_os, const char *novo_status)
{
    if (!app.online)
        return;

    JsonDocument doc;
    doc["cmd"] = "set_status";
    doc["numero_os"] = numero_os;
    doc["status"] = novo_status;
    // 60 (os_id) + 11 (status) + as chaves: 160 ficava a poucos bytes de
    // truncar o JSON no meio, e serializeJson trunca em silencio.
    char payload[256];
    serializeJson(doc, payload, sizeof(payload));

    JsonDocument resp;
    if (!serial_request(payload, "ok", resp))
    {
        Serial.printf("Serial: falha atualizar status (timeout, %s)\n", numero_os);
        return;
    }

    if (resp["ok"] | false)
        Serial.printf("Serial: status %s -> %s\n", numero_os, novo_status);
    else
        Serial.printf("Serial: status %s rejeitado: %s\n", numero_os, (const char *)(resp["msg"] | ""));
}

// ============================================================
// HISTORICO (log acoes do painel no backend, via Serial)
// ============================================================
static void api_log_historico(const char *operador, const char *acao, const char *detalhes)
{
    if (!app.online)
        return;

    JsonDocument doc;
    doc["event"] = "historico";
    doc["operador"] = operador;
    doc["acao"] = acao;
    doc["detalhes"] = detalhes;
    char payload[256];
    serializeJson(doc, payload, sizeof(payload));
    serial2_send(payload);
}

static void api_post_desvio(const char *numero_os, const char *tipo, const char *descricao)
{
    if (!app.online)
        return;

    JsonDocument doc;
    doc["event"] = "desvio";
    doc["numero_os"] = numero_os;
    doc["tipo"] = tipo;
    doc["descricao"] = descricao;
    doc["operador"] = (app.operador_logado >= 0) ? operadores[app.operador_logado].nome : "---";
    // descricao chega do verificar_estoque com ate 256 bytes; somados o os_id
    // (60) e o operador, 384 truncava o JSON.
    char payload[640];
    serializeJson(doc, payload, sizeof(payload));
    serial2_send(payload);
}

// ============================================================
// OFFLINE CACHE (SD card)
// ============================================================
#define MAX_PENDING_ACTIONS 20
struct PendingAction
{
    char type[16];              // "status" ou "sync"
    char param1[MAX_OS_ID_LEN]; // numero_os ou "" — mesmo campo, mesmo limite
    char param2[MAX_STATUS_LEN]; // status ou ""
    bool used;
};
static PendingAction pending_actions[MAX_PENDING_ACTIONS] = {};
static int num_pending = 0;

static void save_operadores_to_sd()
{
    if (!sd_ok || num_operadores == 0)
        return;
    File f = SD.open("/operadores.json", FILE_WRITE);
    if (!f)
        return;
    // Sem `pin`. Este arquivo fica num cartao SD que sai da bancada no bolso de
    // qualquer um; ele guardava o PIN de todos os operadores em texto puro.
    // Hash tambem nao resolveria: 4 digitos sao 10 mil tentativas offline.
    JsonDocument doc;
    JsonArray arr = doc.to<JsonArray>();
    for (int i = 0; i < num_operadores; i++)
    {
        JsonObject o = arr.add<JsonObject>();
        o["nome"] = operadores[i].nome;
    }
    serializeJson(doc, f);
    f.close();
    Serial.printf("SD: %d operadores salvos\n", num_operadores);
}

static void load_operadores_from_sd()
{
    if (!sd_ok || !SD.exists("/operadores.json"))
        return;
    File f = SD.open("/operadores.json", FILE_READ);
    if (!f)
        return;
    String content = f.readString();
    f.close();

    JsonDocument doc;
    if (deserializeJson(doc, content))
        return;

    JsonArray arr = doc.as<JsonArray>();
    num_operadores = 0;
    for (JsonObject obj : arr)
    {
        if (num_operadores >= MAX_OPERADORES)
            break;
        const char *nome = obj["nome"] | "";
        strncpy(operadores[num_operadores].nome, nome, sizeof(operadores[0].nome) - 1);
        operadores[num_operadores].nome[sizeof(operadores[0].nome) - 1] = '\0';
        operadores[num_operadores].ativo = true;
        num_operadores++;
    }
    Serial.printf("SD: %d operadores carregados do cache\n", num_operadores);
}

// Uma ordem espelhada do computador central e so-leitura no painel: quem a
// executa e a celula, e o backend recusa qualquer escrita sobre ela.
static bool ordem_e_central(const char *numero_os)
{
    if (!numero_os || !numero_os[0])
        return false;
    for (int i = 0; i < app.num_ordens; i++)
        if (strcmp(app.ordens[i].id, numero_os) == 0)
            return app.ordens[i].central;
    return false;
}

static void queue_pending_action(const char *type, const char *p1, const char *p2)
{
    if (num_pending >= MAX_PENDING_ACTIONS)
        return;
    // Acao enfileirada e acao que vai ser TENTADA DE NOVO quando o backend
    // voltar. Para ordem do central, "depois" nunca e a hora certa: a recusa
    // nao e do momento, e da ordem. Enfileirar so adiaria a mesma recusa e
    // ainda gastaria um dos MAX_PENDING_ACTIONS slots.
    if (strcmp(type, "status") == 0 && ordem_e_central(p1))
    {
        Serial.printf("OFFLINE: ordem %s e do computador central, acao nao enfileirada\n", p1);
        return;
    }
    PendingAction &a = pending_actions[num_pending];
    copy_trunc(a.type, sizeof(a.type), type);
    copy_trunc(a.param1, sizeof(a.param1), p1);
    copy_trunc(a.param2, sizeof(a.param2), p2);
    a.used = true;
    num_pending++;
    Serial.printf("OFFLINE: acao enfileirada [%s] %s %s\n", type, p1, p2);
}

static void process_pending_actions()
{
    if (num_pending == 0 || !app.online)
        return;
    Serial.printf("ONLINE: processando %d acoes pendentes\n", num_pending);
    for (int i = 0; i < num_pending; i++)
    {
        PendingAction &a = pending_actions[i];
        if (!a.used)
            continue;
        if (strcmp(a.type, "status") == 0)
        {
            api_update_status(a.param1, a.param2);
        }
        else if (strcmp(a.type, "sync") == 0)
        {
            sync_dispensers_to_api();
        }
        a.used = false;
    }
    num_pending = 0;
    Serial.println("ONLINE: todas acoes pendentes processadas");
}

// ============================================================
// SD CARD
// ============================================================
static void setup_sd()
{
    SPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
    if (SD.begin(SD_CS, SPI, 4000000))
    {
        sd_ok = true;
        Serial.printf("SD card OK. Size: %lluMB\n", SD.cardSize() / (1024 * 1024));
    }
    else
    {
        sd_ok = false;
        Serial.println("SD card FALHOU - continuando sem SD");
    }
}

// ============================================================
// SETUP & LOOP
// ============================================================
void setup()
{
    setup_display();
    setup_sd();
    load_logos_from_sd();
    build_ui();
    lv_timer_create(timer_update_cb, 1000, NULL);
    last_touch_time = millis();

    load_operadores_from_sd();

    setenv("TZ", TZ_STRING, 1);
    tzset();

    Serial.print("Conectando ao backend via Serial...");
    Serial.flush();
    int tries = 0;
    do
    {
        sync_time_with_backend();
        if (!app.online)
        {
            delay(200);
            Serial.print(".");
            Serial.flush();
            tries++;
        }
    } while (!app.online && tries < 3);
    Serial.println(app.online ? " OK!" : " falhou (vai tentar de novo depois)");
    Serial.flush();

    fetch_operadores_api();
    fetch_dispensers_api();
    fetch_catalogo_api();

    Serial.println("=== APSEN Dispensacao Iniciado ===");
}

static unsigned long last_sync = 0;
static unsigned long last_catalogo_sync = 0;
#define SYNC_INTERVAL_MS 5000
#define CATALOGO_INTERVAL_MS 60000

void loop()
{
    loop_display();

    if (currentPage != PAGE_SCREENSAVER &&
        millis() - last_touch_time > SCREENSAVER_TIMEOUT_MS)
    {
        activate_screensaver();
    }

    if (currentPage == PAGE_SCREENSAVER)
        return;

    check_serial2();

    if (force_fetch_ordens && app.online && app.logado)
    {
        force_fetch_ordens = false;
        fetch_ordens_api();
        update_dashboard_ui();
        update_relatorio_ui();
    }

    if (millis() - last_sync > SYNC_INTERVAL_MS)
    {
        last_sync = millis();

        sync_time_with_backend();

        lv_label_set_text(ui_net_badge, app.online ? "ONLINE" : "OFFLINE");
        update_clock();

        if (app.online)
        {
            process_pending_actions();

            if (app.logado)
            {
                fetch_ordens_api();
                fetch_dispensers_api();
                switch (currentPage)
                {
                case PAGE_DASHBOARD:
                    update_dashboard_ui();
                    break;
                case PAGE_DISPENSERS:
                    update_dispensers_ui();
                    break;
                case PAGE_RELATORIO:
                    update_relatorio_ui();
                    break;
                case PAGE_STATUS:
                    update_status_ui();
                    break;
                default:
                    break;
                }
            }

            if (millis() - last_catalogo_sync > CATALOGO_INTERVAL_MS)
            {
                last_catalogo_sync = millis();
                fetch_catalogo_api();
                fetch_operadores_api();
            }
        }
    }
}
