#ifndef DISPLAY_H
#define DISPLAY_H

#include <lvgl.h> /* 9.2.2 */
#include <Arduino_GFX_Library.h> /* 1.5.0 */
#include "TAMC_GT911.h" /* 1.0.2 */
#include <esp_heap_caps.h>

// Configuration for Display and Touch
// A board esp32s3box ja define TFT_BL/GFX_BL em pins_arduino.h com o valor do
// devkit oficial, que nesta placa nao corresponde a pino nenhum. Sem o #undef
// a definicao da board prevalece e o backlight vai parar no GPIO 227
// (inexistente): o painel inicializa, mas a luz de fundo nunca acende e a tela
// fica preta. O sintoma no boot e "gpio_set_level(227): GPIO output error".
#ifdef TFT_BL
#undef TFT_BL
#endif
#define TFT_BL 2
#define TOUCH_GT911_SCL 20
#define TOUCH_GT911_SDA 19
#define TOUCH_GT911_INT -1
#define TOUCH_GT911_RST 38
#define TOUCH_MAP_X1 800
#define TOUCH_MAP_X2 0
#define TOUCH_MAP_Y1 480
#define TOUCH_MAP_Y2 0

// Initialize touchscreen object
TAMC_GT911 ts(TOUCH_GT911_SDA, TOUCH_GT911_SCL, TOUCH_GT911_INT, TOUCH_GT911_RST, max(TOUCH_MAP_X1, TOUCH_MAP_X2), max(TOUCH_MAP_Y1, TOUCH_MAP_Y2));

#ifdef GFX_BL
#undef GFX_BL
#endif
#define GFX_BL 2

Arduino_ESP32RGBPanel rgbpanel(
    41 /* DE */, 40 /* VSYNC */, 39 /* HSYNC */, 42 /* PCLK */,
    14 /* R0 */, 21 /* R1 */, 47 /* R2 */, 48 /* R3 */, 45 /* R4 */,
    9 /* G0 */, 46 /* G1 */, 3 /* G2 */, 8 /* G3 */, 16 /* G4 */, 1 /* G5 */,
    15 /* B0 */, 7 /* B1 */, 6 /* B2 */, 5 /* B3 */, 4 /* B4 */,
    0 /* hsync_polarity */, 20 /* hsync_front_porch */, 30 /* hsync_pulse_width */, 16 /* hsync_back_porch */,
    0 /* vsync_polarity */, 22 /* vsync_front_porch */, 13 /* vsync_pulse_width */, 10 /* vsync_back_porch */,
    true /* pclk_active_neg */);

Arduino_RGB_Display gfx(800, 480, &rgbpanel, 0, true);

uint32_t screenWidth;
uint32_t screenHeight;
uint32_t bufSize;
lv_display_t *disp;
lv_color_t *disp_draw_buf;

uint32_t millis_cb(void)
{
  return millis();
}

void my_disp_flush(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map)
{
#ifndef DIRECT_MODE
  uint32_t w = lv_area_get_width(area);
  uint32_t h = lv_area_get_height(area);
  gfx.draw16bitRGBBitmap(area->x1, area->y1, (uint16_t *)px_map, w, h);
#endif
  lv_disp_flush_ready(disp);
}

extern unsigned long last_touch_time;

void my_touchpad_read(lv_indev_t *indev, lv_indev_data_t *data)
{
  ts.read();
  if (ts.isTouched)
  {
    for (int i = 0; i < ts.touches; i++)
    {
      if (i == 0)
      {
        data->state = LV_INDEV_STATE_PRESSED;
        data->point.x = ts.points[i].x;
        data->point.y = ts.points[i].y;
        last_touch_time = millis();
      }
    }
  }
  else
  {
    data->state = LV_INDEV_STATE_RELEASED;
  }
}

void setup_display()
{
  Serial.begin(115200);
  Serial.println("Initializing display...");

  gfx.begin();


  gfx.fillScreen(0x000000);

#ifdef GFX_BL
  pinMode(GFX_BL, OUTPUT);
  digitalWrite(GFX_BL, HIGH);
#endif

  ts.begin(); // Initialize the touchscreen
  ts.setRotation(1);

  lv_init();
  lv_tick_set_cb(millis_cb);

  screenWidth = gfx.width();
  screenHeight = gfx.height();
#ifdef DIRECT_MODE
  bufSize = screenWidth * screenHeight;
#else
  bufSize = screenWidth * 40;
#endif

  disp_draw_buf = (lv_color_t *)heap_caps_malloc(bufSize * 2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!disp_draw_buf) {
    disp_draw_buf = (lv_color_t *)malloc(bufSize * 2);
  }
  if (!disp_draw_buf)
  {
    Serial.println("Failed to allocate display buffer!");
    return;
  }

  disp = lv_display_create(screenWidth, screenHeight);
  lv_display_set_flush_cb(disp, my_disp_flush);
  lv_display_set_buffers(disp, disp_draw_buf, NULL, bufSize * 2, LV_DISPLAY_RENDER_MODE_PARTIAL);

  lv_indev_t *indev = lv_indev_create();
  lv_indev_set_type(indev, LV_INDEV_TYPE_POINTER);
  lv_indev_set_read_cb(indev, my_touchpad_read);

  Serial.printf("Display setup complete. BL pin = %d\n", GFX_BL);
}

void loop_display()
{
  lv_task_handler();
  delay(5);
}


#endif // DISPLAY_H
