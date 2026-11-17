// ======================================================================
//  ISP для AT89S8253 (MCS-51, голова ZoomLamp) — аппаратный SPI Nano.
//  Команды по 4 байта при RST = 1; набор команд — как в at89s825x-arduino
//  (mtirado), который проверен на AT89S8253.
//
//  SCK = 16 МГц / 128 = 125 кГц: должно быть меньше f/16 кварца головы
//  (3,58 МГц → 223 кГц). Режим SPI (0 или 1) подбирается при входе по
//  сигнатуре. Время записи страницы и стирания — с запасом, результат
//  проверяет программа ПК сверкой.
// ======================================================================

#include "common.h"
#include "isp89.h"
#include <util/delay.h>

#define PAGE_CODE 64
#define PAGE_DATA 32
#define WRITE_MS  12
#define ERASE_MS  600

// Запись страницы идёт в чипе ~WRITE_MS после передачи. Nano не ждёт её
// сразу, а отвечает ПК: ожидание совпадает с пересылкой следующей страницы.
// Перед любым следующим обращением к чипу (и перед снятием RST) — isp_settle().
static bool     write_pending;
static uint32_t write_start_us;

void isp_settle() {
  if (!write_pending) return;
  // Остаток считаем один раз и ждём задержкой: pins_release() зовут и при
  // запрещённых прерываниях (UPDI), а тогда micros() не идёт.
  uint32_t done = micros() - write_start_us;
  if (done < WRITE_MS * 1000UL)
    for (uint16_t n = (WRITE_MS * 1000UL - done) / 100 + 1; n; n--) _delay_us(100);
  write_pending = false;
}

// Режим программирования открыт, пока включён SPI: любая другая команда
// вызывает pins_release(), а та выключает SPI.
#define ISP_ON() (SPCR & _BV(SPE))

static uint8_t xfer(uint8_t b) {
  SPDR = b;
  while (!(SPSR & _BV(SPIF))) ;
  return SPDR;
}

static uint8_t cmd4(uint8_t a, uint8_t b, uint8_t c, uint8_t d) {
  xfer(a); xfer(b); xfer(c);
  return xfer(d);
}

static uint8_t read_byte(uint8_t space, uint16_t addr) {
  uint8_t ah = addr >> 8, al = addr;
  switch (space) {
  case ISP_CODE:  return cmd4(0x20, ah, al, 0);
  case ISP_DATA:  return cmd4(0xA0, ah, al, 0);
  case ISP_ATMEL: return cmd4(0x28, 0, al, 0);
  case ISP_USER:  return cmd4(0x22, 0, al, 0);
  case ISP_LOCK:  return cmd4(0x24, 0, 0, 0);
  default:        return cmd4(0x21, 0, 0, 0);    // ISP_FUSES
  }
}

static void rst_low()  { PORTB &= ~_BV(RST89_BIT); DDRB |= _BV(RST89_BIT); }
static void rst_high() { PORTB |= _BV(RST89_BIT);  DDRB |= _BV(RST89_BIT); }

uint8_t isp_open(uint8_t *out) {
  pins_release();
  PORTB &= ~(_BV(SCK_BIT) | _BV(MOSI_BIT));
  DDRB  |= _BV(SCK_BIT) | _BV(MOSI_BIT);           // SCK = 0 до подъёма RST
  for (uint8_t mode = 0; mode < 2; mode++) {
    // SS (D10 = RST) — выходом ДО включения SPI: иначе при SS-входе, прижатом
    // подтяжкой головы к нулю, срабатывает Mode Fault, MSTR сбрасывается,
    // SPI становится ведомым и xfer() ждёт SPIF вечно (Nano «замирает»).
    rst_low();
    SPCR = _BV(SPE) | _BV(MSTR) | _BV(SPR1) | _BV(SPR0) | (mode ? _BV(CPHA) : 0);
    SPSR = 0;
    _delay_ms(10);
    rst_high();
    _delay_ms(50);
    cmd4(0xAC, 0x53, 0x00, 0x00);                  // Programming Enable
    // Сигнатура 1E 73 — по 0x30 (AT89S8253; третий байт не определён, читается FF);
    // на всякий случай и по 0x00
    for (uint8_t base = 0x30;; base = 0x00) {
      if (read_byte(ISP_ATMEL, base) == 0x1E) {
        out[0] = mode;
        for (uint8_t i = 0; i < 3; i++) out[1 + i] = read_byte(ISP_ATMEL, base + i);
        return ST_OK;
      }
      if (base == 0x00) break;
    }
  }
  isp_close();
  return ST_ISP_NOSYNC;
}

void isp_close() {
  isp_settle();
  if (DDRB & _BV(RST89_BIT)) {                     // выйти из программирования
    rst_low();
    _delay_ms(1);
  }
  pins_release();                                  // голова стартует от своей цепи сброса
}

uint8_t isp_read(uint8_t space, uint16_t addr, uint8_t *buf, uint8_t n) {
  if (!ISP_ON()) return ST_ISP_NOTOPEN;
  if (space > ISP_FUSES || n == 0) return ST_BAD_ARGS;
  isp_settle();
  uint8_t page = space == ISP_CODE ? PAGE_CODE : space == ISP_DATA ? PAGE_DATA : 0;
  if (page && n == page && !(addr & (page - 1))) {
    xfer(space == ISP_CODE ? 0x30 : 0xB0);         // чтение страницы
    xfer(addr >> 8);
    xfer(addr);
    for (uint8_t i = 0; i < n; i++) buf[i] = xfer(0);
    return ST_OK;
  }
  for (uint8_t i = 0; i < n; i++) buf[i] = read_byte(space, addr + i);
  return ST_OK;
}

uint8_t isp_write_page(uint8_t space, uint16_t addr, const uint8_t *data, uint8_t n) {
  if (!ISP_ON()) return ST_ISP_NOTOPEN;
  uint8_t page = space == ISP_CODE ? PAGE_CODE : space == ISP_DATA ? PAGE_DATA : 0;
  if (!page || n != page || (addr & (page - 1))) return ST_BAD_ARGS;
  isp_settle();
  xfer(space == ISP_CODE ? 0x50 : 0xD0);
  xfer(addr >> 8);
  xfer(addr);
  for (uint8_t i = 0; i < n; i++) xfer(data[i]);
  write_start_us = micros();                       // ждём не здесь, а в isp_settle()
  write_pending = true;
  return ST_OK;
}

uint8_t isp_erase() {
  if (!ISP_ON()) return ST_ISP_NOTOPEN;
  isp_settle();
  cmd4(0xAC, 0x80, 0x00, 0x00);                    // Chip Erase: Flash + EEPROM + lock
  _delay_ms(ERASE_MS);
  return ST_OK;
}
