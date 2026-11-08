// ======================================================================
//  Тест чипа Zoom — мастер 1-Wire. Перенесён из сниффера
//  (ZoomLamp/.../Сниффер 1-Wire/onewire_sniffer.ino, режим мастера).
//
//  Тайминги — копия головы ZoomLamp: сброс 606 мкс, слот ~145 мкс,
//  «1» — ноль 13 мкс, «0» — ноль 73 мкс; чтение — ноль 12 мкс, опрос линии
//  с 15-й по 40-ю мкс. С учебными таймингами эмулятор отвечает FF.
//
//  Питание чипа только от линии: подтяжка D3 → 2 кОм → DQ (как 2 кОм
//  в голове), плюс «сильная подтяжка» 250 мкс перед каждой транзакцией.
//  Чип питается только от линии: VCC на разъём X2 не выводится.
//  Только чтение — команд записи здесь нет.
// ======================================================================

#include "common.h"
#include "chip.h"
#include <util/delay.h>

#define OW_LOW()  (DDRD |= _BV(DQ_BIT))           // PORTD.2 = 0, выход — прижать
#define OW_REL()  (DDRD &= ~_BV(DQ_BIT))          // вход — отпустить
#define OW_IN()   (PIND & _BV(DQ_BIT))

#define T_STRONG_US 250

static void ow_strong_pullup() {
  PORTD |= _BV(DQ_BIT);
  DDRD  |= _BV(DQ_BIT);                            // выход, +5 В
  delayMicroseconds(T_STRONG_US);
  DDRD  &= ~_BV(DQ_BIT);                           // сначала вход,
  PORTD &= ~_BV(DQ_BIT);                           // потом снять внутреннюю подтяжку
}

// 0 — нет присутствия, 1 — чип ответил, 2 — линия не поднимается.
static uint8_t ow_reset() {
  ow_strong_pullup();
  delayMicroseconds(20);
  if (!OW_IN()) return 2;
  uint8_t sreg = SREG;
  cli();
  OW_LOW(); delayMicroseconds(606);
  OW_REL(); delayMicroseconds(70);
  uint8_t pres = OW_IN() ? 0 : 1;
  SREG = sreg;
  delayMicroseconds(410);
  return pres;
}

static void ow_write_bit(uint8_t b) {
  cli();
  OW_LOW();
  if (b) { delayMicroseconds(13); OW_REL(); sei(); delayMicroseconds(130); }
  else   { delayMicroseconds(73); OW_REL(); sei(); delayMicroseconds(70); }
}

static uint8_t ow_read_bit() {
  uint8_t r = 1;
  cli();
  uint16_t t0 = TCNT1;
  OW_LOW(); delayMicroseconds(12);
  OW_REL();
  while ((uint16_t)(TCNT1 - t0) < US(15)) ;
  while ((uint16_t)(TCNT1 - t0) < US(40)) {
    if (!OW_IN()) r = 0;
  }
  sei();
  delayMicroseconds(105);
  return r;
}

static void ow_write(uint8_t v) {
  for (uint8_t i = 0; i < 8; i++) { ow_write_bit(v & 1); v >>= 1; }
}

static uint8_t ow_read() {
  uint8_t v = 0;
  for (uint8_t i = 0; i < 8; i++) v |= ow_read_bit() << i;
  return v;
}

// verify=0 — в буферы, verify=1 — сверка с буферами.
// 0 — успех, 1 — нет присутствия, 2 — расхождение, 3 — линия не поднимается.
static uint8_t chip_read(uint8_t *rom, uint8_t *mem, uint8_t verify) {
  uint8_t diff = 0;
  uint8_t p = ow_reset();
  if (p == 2) return 3;
  if (!p) return 1;
  ow_write(0x33);                                  // READ ROM
  for (uint8_t i = 0; i < 8; i++) {
    uint8_t v = ow_read();
    if (verify) diff |= v != rom[i]; else rom[i] = v;
  }
  p = ow_reset();
  if (p == 2) return 3;
  if (!p) return 1;
  ow_write(0xCC);                                  // SKIP ROM
  ow_write(0xF0);                                  // READ MEMORY
  ow_write(0x00); ow_write(0x00);                  // с адреса 0x0000
  for (uint8_t i = 0; i < CHIP_MEM; i++) {
    uint8_t v = ow_read();
    if (verify) diff |= v != mem[i]; else mem[i] = v;
  }
  ow_reset();
  return diff ? 2 : 0;
}

static uint8_t map_err(uint8_t e) {
  switch (e) {
  case 0:  return ST_OK;
  case 1:  return ST_OW_NOPRESENCE;
  case 2:  return ST_OW_DIFF;
  default: return ST_OW_LINE_LOW;
  }
}

uint8_t chip_test(uint16_t charge_ms, uint8_t *rom, uint8_t *mem) {
  pins_release();
  timer1_free();
  PORTD |= _BV(PULL_BIT); DDRD |= _BV(PULL_BIT);   // подтяжка 2 кОм включена
  while (charge_ms--) _delay_ms(1);

  uint8_t e = chip_read(rom, mem, 0);
  if (e == 0) e = chip_read(rom, mem, 1);          // второе чтение — сверка
  pins_release();
  return map_err(e);
}
