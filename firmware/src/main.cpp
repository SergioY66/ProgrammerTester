// ======================================================================
//  Программатор-тестер карт Flash и чипов Zoom (ATtiny816 / ATtiny1616)
//  и головы ZoomLamp (AT89S8253, ISP)
//
//  При включении — автономный режим: кнопка A1 — тест карты, A2 — тест чипа,
//  результат на OLED. Первый пакет от ПК переводит в управление с ПК:
//  программаторы UPDI и ISP, тест карты, тест чипа — каждая команда сама настраивает
//  нужные выводы. ПК шлёт PING раз в 2 с; если пакетов нет PC_TIMEOUT_MS —
//  программа ПК закрыта, возврат в автономный режим.
//
//  Обмен с ПК, 250000 бод, двоичные пакеты:
//    запрос:  A5 cmd len payload[len] xor
//    ответ:   5A status len payload[len] xor
//  xor — XOR всех байтов после A5/5A.
// ======================================================================

#include "common.h"
#include "updi.h"
#include "isp89.h"
#include "card.h"
#include "chip.h"
#include "sniff.h"
#include "display.h"
#include "standalone.h"

#define FW_VERSION 9
#define PC_TIMEOUT_MS 5000

static bool pc_mode;
static uint32_t pc_last_ms;

// Что сейчас показано на OLED в режиме ПК. Перерисовка (I2C, десятки мс) —
// только когда функция сменилась, а не на каждую команду (страницы Flash и т. п.).
static const char *shown_l2, *shown_l3;

static bool same(const char *a, const char *b) {
  return a == b || (a && b && !strcmp(a, b));
}

static void show(const char *l2, const char *l3 = nullptr) {
  if (same(l2, shown_l2) && same(l3, shown_l3)) return;
  shown_l2 = l2;
  shown_l3 = l3;
  disp_screen("PC", l2, l3);
}

enum : uint8_t {
  CMD_PING        = 0x01,
  CMD_UPDI_OPEN   = 0x10,   // → statusA
  CMD_UPDI_ERASE  = 0x11,   // стирание ключом + программирование
  CMD_UPDI_ENTER  = 0x12,   // программирование без стирания
  CMD_UPDI_READ   = 0x13,   // addr16, n → data[n]
  CMD_UPDI_FLASH  = 0x14,   // offset16, data — страница Flash
  CMD_UPDI_EEPROM = 0x15,   // offset16, data — страница EEPROM
  CMD_UPDI_FUSE   = 0x16,   // addr16, value
  CMD_UPDI_STATUS = 0x17,   // → ASI_SYS_STATUS
  CMD_UPDI_CLOSE  = 0x18,
  CMD_CARD_TEST   = 0x20,   // → 2 байта
  CMD_CHIP_TEST   = 0x30,   // charge_ms16 → rom[8] + mem[0x90]
  CMD_ISP_OPEN    = 0x40,   // force → режим SPI, сигнатура[3], тип, lock
  CMD_ISP_READ    = 0x41,   // space, addr16, n → data[n]
  CMD_ISP_WRITE   = 0x42,   // space, addr16, data — страница Flash / EEPROM (до 128 Б)
  CMD_ISP_ERASE   = 0x43,   // стирание Flash + EEPROM + lock
  CMD_ISP_CLOSE   = 0x44,
  CMD_ISP_READ_BLOCK = 0x45, // space, addr16, len16 → len/page ответов подряд, по странице
  CMD_ISP_WRITE_PART = 0x46, // space, addr16, часть 0/1, data — страница S52 (256 Б) двумя пакетами
  CMD_ISP_LOCK    = 0x47,   // уровень 2/3 — запись lock-битов
  CMD_ISP_WRITE_BYTE = 0x48, // space, addr16, data — запись одного байта (Byte Mode)
  CMD_SNIFF       = 0x50,   // сниффер 1-Wire: ответ ST_OK, затем поток записей (sniff.h) до любого байта от ПК
};

void pins_release() {
  isp_settle();                                    // не обрывать запись страницы AT89
  // все линии изделия — входы без подтяжки
  DDRD  &= ~(_BV(UPDI_BIT) | _BV(SDA_BIT) | _BV(DQ_BIT) | _BV(PULL_BIT));
  PORTD &= ~(_BV(UPDI_BIT) | _BV(SDA_BIT) | _BV(DQ_BIT) | _BV(PULL_BIT));
  SPCR = 0;
  DDRB  &= ~(_BV(SCL_BIT) | _BV(RST89_BIT) | _BV(MOSI_BIT) | _BV(MISO_BIT) | _BV(SCK_BIT));
  PORTB &= ~(_BV(SCL_BIT) | _BV(RST89_BIT) | _BV(MOSI_BIT) | _BV(MISO_BIT) | _BV(SCK_BIT));
}

static uint8_t rx_buf[256];
static uint8_t tx_buf[256];

static void reply(uint8_t status, const uint8_t *data, uint8_t len, bool flush = true) {
  uint8_t x = status ^ len;
  Serial.write(0x5A);
  Serial.write(status);
  Serial.write(len);
  for (uint8_t i = 0; i < len; i++) { Serial.write(data[i]); x ^= data[i]; }
  Serial.write(x);
  if (flush) Serial.flush();     // без flush передача идёт фоном, пока читается следующая страница
}

static inline uint16_t get16(const uint8_t *p) { return p[0] | ((uint16_t)p[1] << 8); }

static void handle(uint8_t cmd, const uint8_t *p, uint8_t len) {
  uint8_t st = ST_OK, n = 0;

  // Экран — до cli() (OLED по I2C на прерываниях); PING не меняет экран
  switch (cmd) {
  case CMD_UPDI_OPEN:   show("UPDI", "CONNECT"); break;
  case CMD_UPDI_ERASE:  show("UPDI", "ERASE"); break;
  case CMD_UPDI_ENTER:  show("UPDI", "ENTER PROG"); break;
  case CMD_UPDI_READ:   show("UPDI", "READ"); break;
  case CMD_UPDI_FLASH:  show("UPDI", "WRITE FLASH"); break;
  case CMD_UPDI_EEPROM: show("UPDI", "WRITE EE"); break;
  case CMD_UPDI_FUSE:   show("UPDI", "FUSES"); break;
  case CMD_UPDI_CLOSE:  show("UPDI", "DONE"); break;
  case CMD_CARD_TEST:   show("CARD TEST"); break;
  case CMD_CHIP_TEST:   show("CHIP TEST"); break;
  case CMD_ISP_OPEN:    show("HEAD AT89", "CONNECT"); break;
  case CMD_ISP_READ:
  case CMD_ISP_READ_BLOCK: show("HEAD AT89", "READ"); break;
  case CMD_ISP_WRITE:
  case CMD_ISP_WRITE_PART:
  case CMD_ISP_WRITE_BYTE: show("HEAD AT89", "WRITE"); break;
  case CMD_ISP_ERASE:   show("HEAD AT89", "ERASE"); break;
  case CMD_ISP_LOCK:    show("HEAD AT89", "LOCK"); break;
  case CMD_ISP_CLOSE:   show("HEAD AT89", "DONE"); break;
  case CMD_SNIFF:       show("SNIFFER", "1-WIRE"); break;
  }

  // UPDI — с запрещёнными прерываниями (программный UART)
  if (cmd >= CMD_UPDI_OPEN && cmd <= CMD_UPDI_CLOSE) {
    cli();
    switch (cmd) {
    case CMD_UPDI_OPEN:
      st = updi_open(tx_buf); n = 1;
      break;
    case CMD_UPDI_ERASE:
      st = updi_erase_and_enter();
      break;
    case CMD_UPDI_ENTER:
      st = updi_enter_prog();
      break;
    case CMD_UPDI_READ:
      if (len != 3 || p[2] == 0) { st = ST_BAD_ARGS; break; }
      st = updi_read(get16(p), tx_buf, p[2]); n = p[2];
      break;
    case CMD_UPDI_FLASH:
      if (len < 3) { st = ST_BAD_ARGS; break; }
      st = updi_write_flash_page(get16(p), p + 2, len - 2);
      break;
    case CMD_UPDI_EEPROM:
      if (len < 3) { st = ST_BAD_ARGS; break; }
      st = updi_write_eeprom(get16(p), p + 2, len - 2);
      break;
    case CMD_UPDI_FUSE:
      if (len != 3) { st = ST_BAD_ARGS; break; }
      st = updi_write_fuse(get16(p), p[2]);
      break;
    case CMD_UPDI_STATUS:
      st = updi_sys_status(tx_buf); n = 1;
      break;
    case CMD_UPDI_CLOSE:
      updi_close();
      break;
    }
    sei();
    if (st != ST_OK) n = 0;
    reply(st, tx_buf, n);
    return;
  }

  switch (cmd) {
  case CMD_PING:
    tx_buf[0] = 'P'; tx_buf[1] = 'T'; tx_buf[2] = FW_VERSION;
    reply(ST_OK, tx_buf, 3);
    break;

  case CMD_CARD_TEST:
    st = card_test(tx_buf);
    reply(st, tx_buf, 2);
    break;

  case CMD_CHIP_TEST: {
    uint16_t charge = len >= 2 ? get16(p) : 500;
    st = chip_test(charge, tx_buf, tx_buf + 8);
    reply(st, tx_buf, (st == ST_OK || st == ST_OW_DIFF) ? 8 + CHIP_MEM : 0);
    break;
  }

  case CMD_ISP_OPEN:
    st = isp_open(len >= 1 ? p[0] : 0, tx_buf);
    // При ST_ISP_NOSIG тоже отдаём прочитанное: ПК покажет оператору обе
    // сигнатуры и байт lock, чтобы тот выбрал тип чипа.
    reply(st, tx_buf, (st == ST_OK || st == ST_ISP_NOSIG) ? 7 : 0);
    break;

  case CMD_ISP_READ:
    if (len != 4 || p[3] == 0) { reply(ST_BAD_ARGS, 0, 0); break; }
    st = isp_read(p[0], get16(p + 1), tx_buf, p[3]);
    reply(st, tx_buf, st == ST_OK ? p[3] : 0);
    break;

  case CMD_ISP_READ_BLOCK: {
    // Flash/EEPROM блоком: одна команда — много ответов, без ожидания ПК между страницами.
    // Страница целиком читается в tx_buf (до 256 Б = его размер), а отдаётся
    // ответами не длиннее 128 Б: длина ответа — один байт.
    if (len != 5 || p[0] > ISP_DATA) { reply(ST_BAD_ARGS, 0, 0); break; }
    uint16_t page = isp_page_size(p[0]);
    uint16_t addr = get16(p + 1), total = get16(p + 3);
    if (!page || !total || (addr | total) & (page - 1)) { reply(ST_BAD_ARGS, 0, 0); break; }
    for (; total; total -= page, addr += page) {
      st = isp_read(p[0], addr, tx_buf, page);
      if (st != ST_OK) { reply(st, 0, 0); break; }
      for (uint16_t off = 0; off < page; off += 128) {
        uint8_t n = page - off > 128 ? 128 : page - off;
        reply(ST_OK, tx_buf + off, n, total == page && off + n == page);
      }
    }
    break;
  }

  case CMD_ISP_WRITE:
    if (len < 4) { reply(ST_BAD_ARGS, 0, 0); break; }
    reply(isp_write_page(p[0], get16(p + 1), p + 3, len - 3), 0, 0);
    break;

  case CMD_ISP_WRITE_PART:
    if (len < 5) { reply(ST_BAD_ARGS, 0, 0); break; }
    reply(isp_write_part(p[0], get16(p + 1), p[3], p + 4, len - 4), 0, 0);
    break;

  case CMD_ISP_LOCK:
    if (len != 1) { reply(ST_BAD_ARGS, 0, 0); break; }
    reply(isp_lock(p[0]), 0, 0);
    break;

  case CMD_ISP_WRITE_BYTE:
    if (len != 4) { reply(ST_BAD_ARGS, 0, 0); break; }
    reply(isp_write_byte(p[0], get16(p + 1), p[3]), 0, 0);
    break;

  case CMD_ISP_ERASE:
    reply(isp_erase(), 0, 0);
    break;

  case CMD_ISP_CLOSE:
    isp_close();
    reply(ST_OK, 0, 0);
    break;

  case CMD_SNIFF:
    pins_release();                                // D2 — вход, D3 — Hi-Z (линию подтягивает голова)
    reply(ST_OK, 0, 0);                            // старт принят, дальше — поток
    sniff_run(rx_buf, tx_buf);                     // свои буферы не нужны: команда уже разобрана
    reply(ST_OK, 0, 0);
    show("SNIFFER", "STOPPED");
    break;

  default:
    reply(ST_UNKNOWN_CMD, 0, 0);
  }
}

void setup() {
  pins_release();
  standalone_init();
  Serial.begin(250000);
  Serial.setTimeout(100);
  disp_init();
  standalone_idle_screen();
}

static void pc_packet() {
  if (Serial.read() != 0xA5) return;
  uint8_t hdr[2];
  if (Serial.readBytes(hdr, 2) != 2) return;
  uint8_t len = hdr[1];
  if (Serial.readBytes(rx_buf, len + 1) != (size_t)len + 1) { reply(ST_BAD_PACKET, 0, 0); return; }
  uint8_t x = hdr[0] ^ hdr[1];
  for (uint8_t i = 0; i < len; i++) x ^= rx_buf[i];
  if (x != rx_buf[len]) { reply(ST_BAD_PACKET, 0, 0); return; }
  if (!pc_mode) {
    pc_mode = true;
    pins_release();
    shown_l2 = shown_l3 = nullptr;
    disp_screen("PC", "CONTROL");
  }
  handle(hdr[0], rx_buf, len);
  pc_last_ms = millis();                           // после команды: она могла идти долго
}

void loop() {
  if (Serial.available()) pc_packet();
  if (pc_mode) {
    if (millis() - pc_last_ms > PC_TIMEOUT_MS) {
      pc_mode = false;
      pins_release();
      standalone_idle_screen();
    }
  } else {
    standalone_poll();
  }
}
