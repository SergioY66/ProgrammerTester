#pragma once
#include <Arduino.h>

// Программатор ISP для AT89S8253 и AT89S52: SPI D11/D12/D13, RST — D10.
// 8253 — голова ZoomLamp, питается сама, у неё должен работать кварц.
// S52 — микросхема в панельке: питание 5 В и кварц обязательны.
// Возврат — код ST_* из common.h.

enum : uint8_t {
  ISP_CODE    = 0,   // Flash: 8253 — 12 КБ, страница 64 Б; S52 — 8 КБ, страница 256 Б
  ISP_DATA    = 1,   // EEPROM 8253 — 2 КБ, страница 32 Б (у S52 нет)
  ISP_ATMEL   = 2,   // сигнатура: 8253 — 1E 73 по 0x30; S52 — 1E 52 06 по 0x00
  ISP_USER    = 3,   // сигнатура пользователя 8253, 64 Б (у S52 нет)
  ISP_LOCK    = 4,   // биты защиты, 1 Б (читаются и у закрытого чипа)
  ISP_FUSES   = 5,   // фьюзы 8253, 1 Б (у S52 нет)
};

enum : uint8_t {
  ISP_DEV_8253 = 1,
  ISP_DEV_S52  = 2,
};

// force: 0 — определить по сигнатуре, ISP_DEV_* — тип задан вручную (у закрытого
// чипа сигнатура не читается). out: режим SPI, сигнатура[3], тип чипа, байт lock,
// ответ на Programming Enable (0x69 — чип вошёл в программирование).
uint8_t  isp_open(uint8_t force, uint8_t *out);
void     isp_close();
uint8_t  isp_read(uint8_t space, uint16_t addr, uint8_t *buf, uint16_t n);
uint8_t  isp_write_page(uint8_t space, uint16_t addr, const uint8_t *data, uint16_t n);
uint8_t  isp_write_byte(uint8_t space, uint16_t addr, uint8_t data);   // Byte Mode S52: 0x40
uint8_t  isp_write_part(uint8_t space, uint16_t addr, uint8_t part, const uint8_t *data, uint16_t n);
uint8_t  isp_lock(uint8_t level);              // 8253 — 2 (LB1+LB2), S52 — 3 (LB1+LB2)
uint8_t  isp_erase();
uint16_t isp_page_size(uint8_t space);         // страница текущего чипа, 0 — области нет
void     isp_settle();                         // дождаться конца записи (если идёт)
