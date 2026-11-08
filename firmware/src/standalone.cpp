// ======================================================================
//  Автономный тест (без ПК), как в старом CardTester: нажал кнопку —
//  увидел результат на дисплее. Кнопка A1 — тест карты, A2 — тест чипа.
//  Критерии те же, что в программе ПК (pc/operations.py).
// ======================================================================

#include "common.h"
#include "standalone.h"
#include "display.h"
#include "card.h"
#include "chip.h"
#include <util/delay.h>

#define BTN_MASK (_BV(BTN_CARD_BIT) | _BV(BTN_CHIP_BIT))
#define BTN_PRESSED() (~PINC & BTN_MASK)            // биты нажатых кнопок

#define CHARGE_MS 500

void standalone_init() {
  DDRC &= ~BTN_MASK;
  PORTC |= BTN_MASK;
}

void standalone_idle_screen() {
  disp_screen("TESTER", "B1 card", "B2 chip");
}

static uint8_t crc8(const uint8_t *d, uint8_t n) {
  uint8_t crc = 0;
  while (n--) {
    uint8_t b = *d++;
    for (uint8_t i = 0; i < 8; i++) {
      uint8_t mix = (crc ^ b) & 1;
      crc >>= 1;
      if (mix) crc ^= 0x8C;
      b >>= 1;
    }
  }
  return crc;
}

// Память нового (не привязанного) чипа
static uint8_t chip_expected(uint8_t a) {
  if (a < 0x48) return 0xFF;
  if (a == 0x60) return 0x0F;                      // длительность 15
  if (a == 0x61) return 0x3C;                      // лимит 60
  if (a >= 0x68 && a <= 0x6E) return 0xFF;         // 60 минут
  if (a == 0x6F) return 0x0F;
  if (a >= 0x78 && a <= 0x7F) return 0xFF;         // не привязан
  if (a >= 0x80 && a <= 0x83) return 0xAA;
  if (a == 0x84 || a == 0x85) return 0x55;
  if (a == 0x86 || a == 0x87) return 0x44;
  return 0x00;
}

static uint8_t rom[8], mem[CHIP_MEM];

static void hex2(char *p, uint8_t v) {
  static const char hx[] = "0123456789ABCDEF";
  p[0] = hx[v >> 4]; p[1] = hx[v & 15];
}

static void run_chip_test() {
  disp_screen("CHIP TEST", "...");
  uint8_t st = chip_test(CHARGE_MS, rom, mem);
  // ROM из одних 00 или FF — это состояние линии (нет подтяжки, линия
  // в воздухе), а не чип.
  uint8_t all0 = 1, allF = 1;
  for (uint8_t i = 0; i < 8; i++) { all0 &= rom[i] == 0x00; allF &= rom[i] == 0xFF; }
  const char *why = nullptr;
  if (st == ST_OW_NOPRESENCE) why = "no chip";
  else if (st == ST_OW_LINE_LOW) why = "line low";
  else if (all0 || allF) why = "no chip";
  else if (st == ST_OW_DIFF) why = "unstable";
  else if (rom[0] != 0xAD || rom[5] != 0x20 || rom[6] != 0x00) why = "bad ROM";
  else if (crc8(rom, 7) != rom[7]) why = "bad CRC";
  else {
    for (uint8_t a = 0; a < CHIP_MEM; a++)
      if (mem[a] != chip_expected(a)) { why = "bad MEM"; break; }
  }
  disp_screen("ZOOM CHIP", why ? "FAILED" : "PASSED", why ? why : "60 min");
}

static void run_card_test() {
  disp_screen("CARD TEST", "...");
  uint8_t d[2];
  uint8_t st = card_test(d);
  if (st == ST_OK && d[0] == 0x01 && d[1] == 0x14) {
    disp_screen("FLASH CARD", "PASSED", "120 min");
  } else {
    char got[6] = "?? ??";
    hex2(got, d[0]); hex2(got + 3, d[1]);
    disp_screen("FLASH CARD", "FAILED", st == ST_OK ? got : "no answer");
  }
}

void standalone_poll() {
  uint8_t b = BTN_PRESSED();
  if (!b) return;
  while (BTN_PRESSED()) ;                          // ждём отпускания, как раньше
  _delay_ms(200);
  if (b & _BV(BTN_CARD_BIT)) run_card_test();
  else run_chip_test();
}
